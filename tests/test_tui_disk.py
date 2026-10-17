"""Disk helpers and their descendants remain owned through output and shutdown."""

import io
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
from threading import Event
import time
import unittest
from unittest import mock

from agent_builds import tui_disk
from agent_builds.store import Store


CHILD = textwrap.dedent("""\
    import json, os, signal, socket, sys
    from pathlib import Path
    def identity(pid):
        raw = Path('/proc/{}/stat'.format(pid)).read_text()
        return dict(pid=pid, start=raw[raw.rfind(')') + 2:].split()[19])
    record = dict(leader=identity(os.getppid()), child=identity(os.getpid()))
    Path(sys.argv[2]).write_text(json.dumps(record))
    ready = socket.socket(socket.AF_UNIX)
    ready.connect(sys.argv[1])
    ready.sendall(b'R')
    sys.stdout.write('R')
    sys.stdout.flush()
    signal.pause()
""")

LEADER = textwrap.dedent("""\
    import json, signal, subprocess, sys
    child = subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2], sys.argv[3]],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert child.stdout.read(1) == b'R'
    child.stdout.close()
    if sys.argv[4] == 'report':
        print(json.dumps(dict(schema='buildr-tui-disk-v1', result=dict(paths=[]), error=None)), flush=True)
    else:
        signal.pause()
""")


def live_identity(record):
    try:
        raw = Path("/proc/{}/stat".format(record["pid"])).read_text()
    except FileNotFoundError:
        return False
    fields = raw[raw.rfind(")") + 2 :].split()
    return fields[19] == record["start"] and fields[0] not in ("Z", "X")


@unittest.skipUnless(hasattr(os, "killpg"), "requires POSIX process groups")
class DiskReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repository = self.root / "repo"
        self.repository.mkdir()

    def scan(self):
        reader = tui_disk.DiskRead(self.root / "state")
        self.addCleanup(reader.close)
        return reader

    def fixture_group(self, mode):
        listener = socket.socket(socket.AF_UNIX)
        self.addCleanup(listener.close)
        listener.settimeout(5)
        address, record_path = self.root / "ready.sock", self.root / "pids.json"
        listener.bind(str(address))
        listener.listen(1)
        reader = self.scan()
        command = [
            sys.executable,
            "-c",
            LEADER,
            CHILD,
            str(address),
            str(record_path),
            mode,
        ]
        actual_popen = subprocess.Popen
        opened = []

        def open_process(*args, **kwargs):
            process = actual_popen(*args, **kwargs)
            process.poll = mock.Mock(side_effect=AssertionError("reader polled leader"))
            process.wait = mock.Mock(wraps=process.wait)
            opened.append(process)
            return process

        with mock.patch.object(
            tui_disk, "worker_command", return_value=command
        ), mock.patch(
            "agent_builds.tui_disk.subprocess.Popen", side_effect=open_process
        ):
            future = reader.start(self.repository)
        connection, unused = listener.accept()
        self.addCleanup(connection.close)
        connection.settimeout(5)
        self.assertEqual(connection.recv(1), b"R")
        record = json.loads(record_path.read_text())
        self.assertEqual(opened[0].pid, record["leader"]["pid"])
        self.assertEqual(os.getpgid(record["child"]["pid"]), opened[0].pid)
        return reader, future, opened[0], connection, record

    def assert_group_closed(self, reader, process, connection, record):
        self.assertTrue(reader.close())
        self.assertEqual(connection.recv(1), b"")
        self.assertFalse(Path("/proc/{}".format(record["leader"]["pid"])).exists())
        deadline = time.monotonic() + 2
        pending = Event()
        while live_identity(record["child"]) and time.monotonic() < deadline:
            pending.wait(0.01)
        self.assertFalse(live_identity(record["child"]))
        process.poll.assert_not_called()
        process.wait.assert_called_once_with(timeout=1)

    def test_finished_report_keeps_leader_unreaped_until_owned_group_cleanup(self):
        reader, future, process, connection, record = self.fixture_group("report")
        self.assertEqual(future.result(timeout=5), dict(paths=[]))
        process.poll.assert_not_called()
        process.wait.assert_not_called()
        # Even an exited leader still reserves its identity until close().
        raw = Path("/proc/{}/stat".format(process.pid)).read_text()
        self.assertEqual(
            raw[raw.rfind(")") + 2 :].split()[19], record["leader"]["start"]
        )
        self.assertTrue(live_identity(record["child"]))
        with self.assertRaisesRegex(RuntimeError, "previous disk inspector"):
            reader.start(self.repository)
        self.assert_group_closed(reader, process, connection, record)
        self.assertTrue(reader.close())

    def test_stalled_helper_and_same_group_descendant_are_killed_on_close(self):
        reader, future, process, connection, record = self.fixture_group("stall")
        self.assertFalse(future.done())
        self.assertTrue(live_identity(record["leader"]))
        self.assertTrue(live_identity(record["child"]))
        self.assert_group_closed(reader, process, connection, record)
        with self.assertRaisesRegex(ValueError, "Invalid disk inventory output"):
            future.result(timeout=5)

    def test_real_worker_inspects_missing_state_without_initializing_it_and_can_restart(
        self,
    ):
        reader = self.scan()
        for unused in range(2):
            result = reader.start(self.repository).result(timeout=5)
            self.assertEqual(result["paths"], [])
            self.assertEqual(result["active_scratch_reservations"], [])
            self.assertTrue(reader.close())
            self.assertFalse((self.root / "state").exists())

    def test_worker_reads_use_read_only_store_bounded_timeout_and_close_after_errors(
        self,
    ):
        writer = Store(self.root / "state")
        self.addCleanup(writer.close)
        opened = []

        def create(*args, **kwargs):
            store = Store(*args, **kwargs)
            store.close = mock.Mock(wraps=store.close)
            opened.append(store)
            return store

        def fail(store, repository):
            self.assertEqual(Path(repository), self.repository)
            self.assertTrue(store.read_only)
            self.assertEqual(
                store.db.execute("PRAGMA busy_timeout").fetchone()[0], 1000
            )
            with self.assertRaises(sqlite3.OperationalError):
                store.db.execute("DELETE FROM jobs")
            raise ValueError("fixture scan failed")

        with mock.patch("agent_builds.tui_disk.Store", side_effect=create), mock.patch(
            "agent_builds.disk.inventory", side_effect=fail
        ), mock.patch.object(Store, "recover", side_effect=AssertionError("recovery")):
            with self.assertRaisesRegex(ValueError, "fixture scan failed"):
                tui_disk._inventory(writer.root, self.repository)
        self.assertEqual(len(opened), 1)
        opened[0].close.assert_called_once_with()
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].db.execute("SELECT 1")

    def test_real_worker_reports_unreadable_state_without_changing_its_database(self):
        state = self.root / "state"
        state.mkdir()
        database = state / "queue.sqlite3"
        database.write_bytes(b"not a database")
        reader = self.scan()
        with self.assertRaisesRegex(ValueError, "Disk inventory failed: DatabaseError"):
            reader.start(self.repository).result(timeout=5)
        self.assertTrue(reader.close())
        self.assertEqual(database.read_bytes(), b"not a database")

    def test_invalid_and_oversized_worker_output_remains_bounded(self):
        for item in (
            1,
            {},
            {"path": "x"},
            {"path": 1, "bytes": 0},
            {"path": "x", "bytes": -1},
            {"path": "x", "bytes": True},
            {"path": "x", "bytes": float("nan")},
        ):
            with self.subTest(item=item), self.assertRaisesRegex(
                ValueError, "Invalid disk inventory path"
            ):
                tui_disk._result(
                    json.dumps(
                        dict(
                            schema=tui_disk.SCHEMA,
                            result=dict(paths=[item]),
                            error=None,
                        )
                    ).encode()
                )
        for code, message in (
            ("print('not json')", "Invalid disk inventory output"),
            ("print('{}')", "Invalid disk inventory schema"),
            (
                'print(\'{"schema":"buildr-tui-disk-v1","result":[]}\')',
                "Invalid disk inventory result",
            ),
            (
                "import os, signal; os.write(1, b'x' * (1024 * 1024 + 1)); signal.pause()",
                "exceeded 1 MiB",
            ),
        ):
            with self.subTest(message=message):
                reader = self.scan()
                with mock.patch.object(
                    tui_disk,
                    "worker_command",
                    return_value=[sys.executable, "-c", code],
                ):
                    future = reader.start(self.repository)
                with self.assertRaisesRegex(ValueError, message):
                    future.result(timeout=5)
                self.assertTrue(reader.close())

    def test_cleanup_signals_before_reaping_and_retains_ownership_after_timeout(self):
        process = mock.Mock(pid=123456789)
        process.stdout = io.BytesIO(
            json.dumps(
                dict(schema=tui_disk.SCHEMA, result=dict(paths=[]), error=None)
            ).encode()
        )
        calls = []

        def wait(timeout):
            calls.append(("wait", timeout))
            if len(calls) == 2:
                raise subprocess.TimeoutExpired("fixture", timeout)
            return 0

        process.wait.side_effect = wait
        reader = self.scan()
        with mock.patch(
            "agent_builds.tui_disk.subprocess.Popen", return_value=process
        ) as spawn, mock.patch(
            "agent_builds.tui_disk.os.killpg",
            side_effect=lambda pid, sig: calls.append(("kill", pid, sig)),
        ):
            self.assertEqual(
                reader.start(self.repository).result(timeout=5), dict(paths=[])
            )
            options = spawn.call_args.kwargs
            self.assertTrue(options["start_new_session"])
            self.assertEqual(options["stdin"], subprocess.DEVNULL)
            self.assertEqual(options["stderr"], subprocess.DEVNULL)
            self.assertEqual(
                options["env"]["PYTHONPATH"].split(os.pathsep)[0],
                str(Path(tui_disk.__file__).resolve().parent.parent),
            )
            process.poll.assert_not_called()
            process.wait.assert_not_called()
            self.assertFalse(reader.close())
            self.assertIs(reader._process, process)
            with self.assertRaises(RuntimeError):
                reader.start(self.repository)
            spawn.assert_called_once()
            self.assertTrue(reader.close())
            self.assertTrue(reader.close())
        self.assertEqual(
            calls,
            [
                ("kill", process.pid, signal.SIGKILL),
                ("wait", 1),
                ("kill", process.pid, signal.SIGKILL),
                ("wait", 1),
            ],
        )
        self.assertIsNone(reader._process)

    def test_external_child_reaping_handler_prevents_spawning(self):
        for handler in (signal.SIG_IGN, lambda *args: None):
            with self.subTest(handler=handler), mock.patch(
                "agent_builds.tui_disk.signal.getsignal", return_value=handler
            ), mock.patch("agent_builds.tui_disk.subprocess.Popen") as spawn:
                reader = self.scan()
                with self.assertRaisesRegex(ValueError, "SIGCHLD"):
                    reader.start(self.repository)
                self.assertIsNone(reader._process)
                spawn.assert_not_called()

    def test_already_missing_process_group_is_still_reaped(self):
        process = mock.Mock(pid=123456789)
        reader = self.scan()
        reader._process = process
        with mock.patch(
            "agent_builds.tui_disk.os.killpg", side_effect=ProcessLookupError
        ):
            self.assertTrue(reader.close())
        process.wait.assert_called_once_with(timeout=1)


if __name__ == "__main__":
    unittest.main()
