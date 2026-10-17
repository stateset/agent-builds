"""Dashboard attempts confirm durable creation independently of CLI/native output."""

from concurrent.futures import Future
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from agent_builds import tui_submissions as submissions
from agent_builds.completion import wait
from agent_builds.runner import cancel_job
from agent_builds.store import Store


SOURCE = str(Path(__file__).resolve().parent.parent)
KEY = "a" * 32
OTHER = "b" * 32
HANDLE = "c" * 12
ARGV = ["run", "--", "true"]


class Pending:
    def __init__(self):
        self.calls = []

    def __call__(self, loader, keys):
        future = Future()
        self.calls.append((loader, keys, future))
        return future


class SubmissionTrackerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "absent-state"
        self.pending = Pending()
        self.process = mock.Mock()
        self.process.poll.return_value = None
        self.popen = mock.patch.object(
            submissions.subprocess, "Popen", return_value=self.process
        ).start()
        self.addCleanup(mock.patch.stopall)
        self.tracker = submissions.SubmissionTracker(self.root, submit=self.pending)

    def start(self, now=0, argv=ARGV):
        with mock.patch.object(
            submissions.uuid, "uuid4", return_value=mock.Mock(hex=KEY)
        ):
            return self.tracker.start(argv, "fixture\nrequest", now)

    def test_launch_is_starting_with_no_foreground_file_or_database_reads(self):
        with mock.patch.object(
            Path, "stat", side_effect=AssertionError("foreground stat")
        ), mock.patch.object(
            Path, "resolve", side_effect=AssertionError("foreground resolve")
        ), mock.patch.object(
            submissions.os, "open", side_effect=AssertionError("foreground open")
        ), mock.patch.object(
            submissions, "Store", side_effect=AssertionError("foreground database")
        ):
            key = self.start()
            self.assertEqual(self.tracker.get(key).phase, "starting")
            self.assertEqual(len(self.tracker.poll(0)), 1)
            self.tracker.poll(1)
        command = self.popen.call_args[0][0]
        options = self.popen.call_args[1]
        self.assertEqual(
            command[:5],
            [sys.executable, "-m", "agent_builds.tui_submissions", str(self.root), KEY],
        )
        self.assertEqual(json.loads(command[5]), ARGV)
        self.assertTrue(options["start_new_session"])
        for name in ("stdin", "stdout", "stderr"):
            self.assertEqual(options[name], subprocess.DEVNULL)
        self.assertEqual(options["env"]["AGENT_BUILDS_STATE"], str(self.root))
        self.assertEqual(options["env"]["PYTHONPATH"].split(os.pathsep)[0], SOURCE)
        self.assertEqual(self.tracker.get(key).label, "fixture request")
        self.assertEqual(
            self.tracker.get(key).receipt_path,
            str(self.root / ("receipt-tui-" + key + ".json")),
        )
        self.assertFalse(self.root.exists())

    def test_one_pending_batch_timeout_and_late_receipt_without_retry(self):
        key = self.start()
        self.tracker.poll(0)
        future = self.tracker.future
        self.tracker.poll(20)
        self.tracker.poll(100)
        self.assertEqual(len(self.pending.calls), 1)
        self.assertIs(self.tracker.future, future)
        self.assertEqual(self.tracker.get(key).phase, "unconfirmed")
        future.set_result((submissions.SubmissionInspection(key, "job", HANDLE),))
        changed = self.tracker.poll(101)
        self.assertEqual(changed[0].phase, "submitted")
        self.assertEqual(changed[0].handle, HANDLE)
        self.assertIn("Execution outcome is not verified", changed[0].detail)
        self.assertEqual(self.popen.call_count, 1)
        for method in ("wait", "kill", "terminate"):
            getattr(self.process, method).assert_not_called()

    def test_helper_exit_zero_without_receipt_never_confirms_and_is_reaped(self):
        key = self.start()
        self.process.poll.return_value = 0
        self.tracker.poll(0)
        self.assertEqual(self.tracker.get(key).phase, "unconfirmed")
        self.assertIn(
            "exited 0 without a confirmed receipt", self.tracker.get(key).detail
        )
        self.assertEqual(self.tracker.processes, ())
        self.tracker.future.set_result(
            (submissions.SubmissionInspection(key, "job", HANDLE),)
        )
        self.tracker.poll(1)
        self.assertEqual(self.tracker.get(key).phase, "submitted")
        self.assertEqual(self.popen.call_count, 1)

    def test_creation_survives_helper_failure_but_never_implies_execution_success(self):
        key = self.start(argv=["task", "run", "fixture"])
        self.tracker.poll(0)
        self.tracker.future.set_result(
            (submissions.SubmissionInspection(key, "plan", HANDLE),)
        )
        self.tracker.poll(1)
        self.assertEqual(self.tracker.get(key).phase, "submitted")
        self.process.poll.return_value = 7
        self.tracker.poll(2)
        status = self.tracker.get(key)
        self.assertEqual(
            (status.phase, status.kind, status.handle), ("submitted", "plan", HANDLE)
        )
        self.assertIn("helper exited 7", status.detail)
        self.assertIn("Control startup is unconfirmed", status.detail)

    def test_foreign_keys_invalid_handles_and_changed_receipts_are_ignored(self):
        key = self.start()
        self.tracker.poll(0)
        self.tracker.future.set_result(
            (submissions.SubmissionInspection(OTHER, "job", HANDLE),)
        )
        self.tracker.poll(1)
        self.assertIsNone(self.tracker.get(key).handle)
        self.tracker.poll(2)
        self.tracker.future.set_result(
            (submissions.SubmissionInspection(key, "job", "foreign"),)
        )
        self.tracker.poll(3)
        self.assertIsNone(self.tracker.get(key).handle)
        self.tracker.poll(4)
        self.tracker.future.set_result(
            (submissions.SubmissionInspection(key, "job", HANDLE),)
        )
        self.tracker.poll(5)
        self.tracker.poll(6)
        self.tracker.future.set_result(
            (submissions.SubmissionInspection(key, "plan", "d" * 12),)
        )
        self.tracker.poll(7)
        self.assertEqual(
            (self.tracker.get(key).kind, self.tracker.get(key).handle), ("job", HANDLE)
        )
        self.assertIn("changed receipt was ignored", self.tracker.get(key).detail)

    def test_errors_are_visible_and_inspection_is_throttled(self):
        key = self.start()
        self.tracker.poll(0)
        self.tracker.future.set_exception(OSError("fixture unavailable"))
        self.tracker.poll(1)
        self.assertIn("fixture unavailable", self.tracker.get(key).detail)
        self.assertEqual(self.tracker.get(key).phase, "unconfirmed")
        self.tracker.poll(1.2)
        self.assertEqual(len(self.pending.calls), 1)
        self.tracker.poll(1.5)
        self.assertEqual(len(self.pending.calls), 2)

    def test_failed_spawn_stays_visible_without_an_automatic_retry(self):
        self.popen.side_effect = OSError("fixture launch failure")
        with self.assertRaises(OSError):
            self.start()
        self.assertEqual(self.tracker.get(KEY).phase, "unconfirmed")
        self.assertIn("did not launch", self.tracker.get(KEY).detail)
        self.tracker.poll(30)
        self.assertEqual(self.popen.call_count, 1)
        self.assertEqual(self.pending.calls, [])

    def test_unresolved_capacity_and_completed_history_are_bounded(self):
        self.tracker = submissions.SubmissionTracker(
            self.root, submit=self.pending, limit=2
        )
        for key in (KEY, OTHER):
            with mock.patch.object(
                submissions.uuid, "uuid4", return_value=mock.Mock(hex=key)
            ):
                self.tracker.start(ARGV, key, 0)
        with self.assertRaisesRegex(ValueError, "Too many unresolved"):
            self.tracker.start(ARGV, "third", 0)
        self.process.poll.return_value = 0
        self.tracker.poll(0)
        self.tracker.future.set_result(
            tuple(
                submissions.SubmissionInspection(key, "job", HANDLE)
                for key in (KEY, OTHER)
            )
        )
        self.tracker.poll(1)
        for value in range(3):
            key = "{:032x}".format(value)
            with mock.patch.object(
                submissions.uuid, "uuid4", return_value=mock.Mock(hex=key)
            ):
                self.tracker.start(ARGV, key, 2 + value * 2)
            self.tracker.poll(2 + value * 2)
            self.tracker.future.set_result(
                (submissions.SubmissionInspection(key, "job", HANDLE),)
            )
            self.tracker.poll(3 + value * 2)
        self.assertEqual(len(self.tracker.statuses), 2)
        self.assertIsNone(self.tracker.get(KEY))

    def test_invalid_arguments_and_bounds_fail_before_launch(self):
        for argv in (
            ["run", "echo", "x"],
            ["run", "--"],
            ["cancel", HANDLE],
            ["run", "--receipt-file=foreign", "--", "true"],
            ["run", "--", "x" * submissions.MAX_JSON_BYTES],
            ["run", "--", "é" * submissions.MAX_JSON_BYTES],
            ["run", "--", '"' * (submissions.MAX_JSON_BYTES // 2)],
        ):
            with self.subTest(argv=repr(argv)[:60]), self.assertRaises(ValueError):
                self.tracker.start(argv, "invalid", 0)
        self.popen.assert_not_called()
        for options in (
            dict(interval=float("nan")),
            dict(timeout=float("inf")),
            dict(limit=17),
            dict(limit=True),
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                submissions.SubmissionTracker(self.root, **options)


class SubmissionFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "state"
        self.repo = self.base / "repo"
        self.repo.mkdir()

    def store(self):
        store = Store(self.root)
        self.addCleanup(store.close)
        store.update_config(
            dict(
                cpu_slots=2,
                memory_gb=4,
                min_free_gb=0,
                min_available_gb=0,
                quick_reserve_slots=0,
                quick_reserve_jobs=0,
                quick_reserve_memory_gb=0,
                nice=0,
            )
        )
        return store

    def journal(self, key=KEY, argv=ARGV, started=0, **changes):
        self.root.mkdir(exist_ok=True)
        value = dict(
            schema=submissions.JOURNAL_SCHEMA,
            key=key,
            argv=argv,
            state="starting",
            started_at=started,
        )
        value.update(changes)
        submissions._paths(self.root, key)[2].write_text(json.dumps(value))
        return value

    def receipt(self, handle=HANDLE, key=KEY, **changes):
        value = dict(
            job_id=handle,
            cwd=str(self.repo),
            log_path=str(self.root / "logs" / (handle + ".log")),
        )
        value.update(changes)
        submissions._paths(self.root, key)[0].write_text(json.dumps(value))
        return value


class SubmissionEvidenceTests(SubmissionFixture):
    def test_missing_state_stays_absent_and_invalid_batch_is_bounded(self):
        with mock.patch.object(
            Store, "_ensure_schema", side_effect=AssertionError("migration")
        ), mock.patch.object(Store, "recover", side_effect=AssertionError("recovery")):
            self.assertEqual(
                submissions.inspect_submissions(self.root, (KEY,)),
                (submissions.SubmissionInspection(KEY),),
            )
            self.assertEqual(
                submissions.inspect_submissions(self.root, ([], "foreign")), ()
            )
        self.assertFalse(self.root.exists())
        with self.assertRaises(ValueError):
            submissions.inspect_submissions(self.root, (KEY,) * 17)

    def test_receipt_requires_matching_journal_database_cwd_and_creation_time(self):
        store = self.store()
        handle = store.submit(
            self.repo, ["true"], "local", "quick", 1, 0.25, str(self.repo), "", 1
        )
        self.receipt(handle)
        self.assertIn(
            "no matching attempt journal",
            submissions.inspect_submissions(self.root, (KEY,))[0].detail,
        )
        self.journal(started=time.time() + 1)
        self.assertIn(
            "before this submission attempt",
            submissions.inspect_submissions(self.root, (KEY,))[0].detail,
        )
        self.journal()
        self.receipt(handle, cwd=str(self.base))
        self.assertIsNone(submissions.inspect_submissions(self.root, (KEY,))[0].kind)
        self.receipt(handle)
        before = store.db.total_changes
        with mock.patch.object(
            Store, "_ensure_schema", side_effect=AssertionError("migration")
        ), mock.patch.object(
            Store, "recover", side_effect=AssertionError("recovery")
        ), mock.patch.object(
            Store, "describe", side_effect=AssertionError("bulky job expansion")
        ):
            result = submissions.inspect_submissions(self.root, (KEY,))[0]
        self.assertEqual((result.kind, result.handle), ("job", handle))
        self.assertEqual(store.db.total_changes, before)
        self.receipt(HANDLE)
        self.assertIsNone(submissions.inspect_submissions(self.root, (KEY,))[0].kind)

    def test_reviewed_plan_identity_and_nonzero_outcome_are_independent(self):
        store = self.store()
        store.db.execute(
            "INSERT INTO plans(id,name,cwd,definition,created,state) VALUES(?,?,?,?,?,?)",
            (HANDLE, "fixture", str(self.repo), "{}", time.time(), "queued"),
        )
        self.journal(argv=["task", "run", "fixture"], state="finished", returncode=7)
        path = submissions._paths(self.root, KEY)[0]
        path.write_text(
            json.dumps(
                dict(
                    plan_id=HANDLE,
                    cwd=str(self.repo),
                    state="created",
                    reviewed_task=True,
                )
            )
        )
        result = submissions.inspect_submissions(self.root, (KEY,))[0]
        self.assertEqual(
            (result.kind, result.handle, result.returncode), ("plan", HANDLE, 7)
        )
        self.journal(argv=ARGV)
        self.assertIn(
            "kind does not match",
            submissions.inspect_submissions(self.root, (KEY,))[0].detail,
        )

    def test_nonregular_symlink_oversize_and_malformed_files_fail_without_opening_database(
        self,
    ):
        self.journal()
        receipt = submissions._paths(self.root, KEY)[0]
        for kind in ("fifo", "directory", "symlink", "oversize", "invalid"):
            with self.subTest(kind=kind):
                if kind == "fifo":
                    os.mkfifo(str(receipt))
                elif kind == "directory":
                    receipt.mkdir()
                elif kind == "symlink":
                    receipt.symlink_to(self.root / "outside")
                elif kind == "oversize":
                    receipt.write_bytes(b" " * (submissions.MAX_JSON_BYTES + 1))
                else:
                    receipt.write_bytes(b"invalid json")
                with mock.patch.object(
                    submissions,
                    "Store",
                    side_effect=AssertionError("invalid receipt reached database"),
                ):
                    result = submissions.inspect_submissions(self.root, (KEY,))[0]
                self.assertIsNone(result.kind)
                self.assertIn("Inspection unavailable", result.detail)
                receipt.rmdir() if kind == "directory" else receipt.unlink()

    def test_evidence_descriptors_close_when_fdopen_fails(self):
        self.journal()
        opened = []
        real_open = os.open

        def opening(*args, **kwargs):
            descriptor = real_open(*args, **kwargs)
            opened.append(descriptor)
            return descriptor

        with mock.patch.object(
            submissions.os, "open", side_effect=opening
        ), mock.patch.object(
            submissions.os, "fdopen", side_effect=OSError("fixture fdopen failure")
        ):
            self.assertIn(
                "fdopen failure",
                submissions.inspect_submissions(self.root, (KEY,))[0].detail,
            )
        for descriptor in opened:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_worker_owns_readonly_connection_and_closes_on_database_error(self):
        store = self.store()
        handle = store.submit(
            self.repo, ["true"], "local", "quick", 1, 0.25, str(self.repo), "", 1
        )
        self.journal()
        self.receipt(handle)
        opened, threads = [], []

        def reader(root, read_only=False):
            self.assertTrue(read_only)
            connection = Store(root, read_only=True)
            connection.close = mock.Mock(wraps=connection.close)
            original_control = connection.get_control

            def control(handle):
                self.assertEqual(
                    connection.db.execute("PRAGMA busy_timeout").fetchone()[0], 1000
                )
                return original_control(handle)

            connection.get_control = control
            opened.append(connection)
            threads.append(threading.get_ident())
            return connection

        with mock.patch.object(submissions, "Store", side_effect=reader):
            future = submissions.submit_read(
                lambda keys: submissions.inspect_submissions(self.root, keys), (KEY,)
            )
            result = future.result(timeout=5)[0]
        self.assertEqual(result.handle, handle)
        self.assertNotEqual(threads[0], threading.get_ident())
        opened[0].close.assert_called_once_with()
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].db.execute("SELECT 1")
        store.db.execute("DROP TABLE jobs")
        with mock.patch.object(submissions, "Store", side_effect=reader):
            result = submissions.inspect_submissions(self.root, (KEY,))[0]
        self.assertIn("no such table", result.detail)
        opened[-1].close.assert_called_once_with()
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[-1].db.execute("SELECT 1")


class SubmissionHelperTests(SubmissionFixture):
    def test_journal_precedes_cli_and_transcript_is_private_bounded_and_command_safe(
        self,
    ):
        self.root.mkdir(mode=0o750)
        mode = stat.S_IMODE(self.root.stat().st_mode)
        argv = [
            "run",
            "--cwd",
            str(self.repo),
            "--",
            "printf",
            "--receipt-file",
            "literal",
        ]
        seen = []

        def cli():
            receipt, transcript, journal = submissions._paths(self.root, KEY)
            value = json.loads(journal.read_text())
            self.assertEqual(
                (value["key"], value["state"], value["argv"]), (KEY, "starting", argv)
            )
            self.assertEqual(stat.S_IMODE(journal.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(transcript.stat().st_mode), 0o600)
            self.assertEqual(os.environ["AGENT_BUILDS_STATE"], str(self.root))
            seen.extend(sys.argv[1:])
            print("detached CLI status", file=sys.stderr)
            print("x" * (submissions.MAX_TRANSCRIPT_BYTES * 2))
            raise SystemExit(7)

        previous = sys.argv
        with mock.patch.dict(os.environ, AGENT_BUILDS_STATE=str(self.base / "ambient")):
            with mock.patch("agent_builds.__main__.main", side_effect=cli):
                self.assertEqual(submissions.run_helper(self.root, KEY, argv), 7)
            self.assertEqual(
                os.environ["AGENT_BUILDS_STATE"], str(self.base / "ambient")
            )
        self.assertIs(sys.argv, previous)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), mode)
        boundary = seen.index("--")
        self.assertIn("--detach", seen[:boundary])
        self.assertEqual(seen[boundary + 1 :], ["printf", "--receipt-file", "literal"])
        receipt, transcript, journal = submissions._paths(self.root, KEY)
        self.assertEqual(seen[seen.index("--receipt-file") + 1], str(receipt))
        self.assertLessEqual(
            transcript.stat().st_size, submissions.MAX_TRANSCRIPT_BYTES
        )
        self.assertIn("transcript clipped", transcript.read_text())
        self.assertEqual(json.loads(journal.read_text())["returncode"], 7)
        with mock.patch("agent_builds.__main__.main") as cli:
            with self.assertRaises(FileExistsError):
                submissions.run_helper(self.root, KEY, argv)
            cli.assert_not_called()

    def test_invalid_key_or_unbounded_or_ambiguous_command_does_not_create_state(self):
        for key, argv in (
            ("../foreign", ARGV),
            (KEY, ["run", "echo", "unsafe"]),
            (KEY, ["cancel", HANDLE]),
        ):
            with self.subTest(key=key, argv=argv), self.assertRaises(ValueError):
                submissions.run_helper(self.root, key, argv)
            self.assertFalse(self.root.exists())

    def test_atomic_journal_descriptor_closes_if_fdopen_fails(self):
        self.root.mkdir()
        opened = []
        real_open = os.open

        def opening(*args, **kwargs):
            descriptor = real_open(*args, **kwargs)
            opened.append(descriptor)
            return descriptor

        with mock.patch.object(
            submissions.os, "open", side_effect=opening
        ), mock.patch.object(
            submissions.os, "fdopen", side_effect=OSError("fixture fdopen failure")
        ):
            with self.assertRaises(OSError):
                submissions._atomic_json(self.root / "attempt.json", {}, initial=True)
        self.assertEqual(list(self.root.iterdir()), [])
        for descriptor in opened:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_explicit_helper_root_overrides_ambient_queue_and_preserves_nested_guard(
        self,
    ):
        ambient = self.base / "ambient"

        def cli():
            writer = Store()
            try:
                writer.update_config(
                    dict(
                        cpu_slots=2,
                        memory_gb=4,
                        quick_reserve_slots=0,
                        quick_reserve_jobs=0,
                        quick_reserve_memory_gb=0,
                    )
                )
                handle = writer.submit(
                    self.repo,
                    ["true"],
                    "local",
                    "quick",
                    1,
                    0.25,
                    str(self.repo),
                    "",
                    1,
                )
                submissions._paths(self.root, KEY)[0].write_text(
                    json.dumps(
                        dict(
                            job_id=handle,
                            cwd=str(self.repo),
                            log_path=str(writer.log_path(handle)),
                        )
                    )
                )
                self.assertEqual(
                    os.environ.get("AGENT_BUILDS_JOB_ID"), "parent-fixture"
                )
            finally:
                writer.close()
            return 0

        with mock.patch.dict(
            os.environ,
            AGENT_BUILDS_STATE=str(ambient),
            AGENT_BUILDS_JOB_ID="parent-fixture",
        ), mock.patch("agent_builds.__main__.main", side_effect=cli):
            code = submissions.run_helper(self.root, KEY, ARGV)
            transcript = submissions._paths(self.root, KEY)[1]
            self.assertEqual(code, 0, transcript.read_text())
            self.assertEqual(os.environ["AGENT_BUILDS_STATE"], str(ambient))
        self.assertFalse(ambient.exists())
        result = submissions.inspect_submissions(self.root, (KEY,))[0]
        self.assertEqual(result.kind, "job")

    def test_pending_daemon_inspection_does_not_prevent_process_exit_or_wait_for_helper(
        self,
    ):
        script = """
from pathlib import Path
import sys, threading
from unittest import mock
from agent_builds import tui_submissions as s
entered = threading.Event()
blocked = threading.Event()
def inspect(root, keys):
    entered.set()
    blocked.wait()
process = mock.Mock()
process.poll.return_value = None
with mock.patch.object(s.subprocess, "Popen", return_value=process), mock.patch.object(s, "inspect_submissions", side_effect=inspect):
    tracker = s.SubmissionTracker(Path(sys.argv[1]))
    tracker.start(["run", "--", "true"], "fixture", 0)
    tracker.poll(0)
    assert entered.wait(2)
    assert tracker.future.running()
    process.wait.assert_not_called()
    process.kill.assert_not_called()
    print("pending; exit without joining", flush=True)
"""
        result = subprocess.run(
            [sys.executable, "-c", script, str(self.root)],
            env=dict(os.environ, PYTHONPATH=SOURCE),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"exit without joining", result.stdout)
        self.assertFalse(self.root.exists())

    def test_real_detached_helper_confirms_exact_job_but_not_its_native_failure(self):
        store = self.store()
        env = dict(os.environ, AGENT_BUILDS_STATE=str(self.root), PYTHONPATH=SOURCE)
        env.pop("AGENT_BUILDS_JOB_ID", None)
        argv = [
            "run",
            "--cwd",
            str(self.repo),
            "--jobs",
            "1",
            "--memory-gb",
            ".25",
            "--timeout",
            "8",
            "--",
            sys.executable,
            "-c",
            'print("native-only-output"); raise SystemExit(7)',
        ]
        tracker = submissions.SubmissionTracker(self.root)
        key = None
        try:
            with mock.patch.dict(os.environ, env, clear=True):
                key = tracker.start(argv, "native fixture", time.monotonic())
            helper = tracker.processes[0]
            self.assertEqual(helper.wait(timeout=20), 0)
            receipt, transcript, journal = submissions._paths(self.root, key)
            for path in (receipt, transcript, journal):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            value = json.loads(receipt.read_text())
            self.assertEqual(json.loads(journal.read_text())["state"], "finished")
            tracker.poll(time.monotonic())
            if tracker.future is not None:
                tracker.future.result(timeout=5)
            tracker.poll(time.monotonic())
            status = tracker.get(key)
            self.assertEqual(
                (status.phase, status.kind, status.handle),
                ("submitted", "job", value["job_id"]),
            )
            self.assertIn("Execution outcome is not verified", status.detail)
            reader = Store(self.root, read_only=True)
            try:
                result = wait(reader, [value["job_id"]], timeout=12, tail_lines=5)
            finally:
                reader.close()
            self.assertTrue(result["complete"], result)
            row = result["jobs"][0]
            self.assertEqual((row["state"], row["exit_code"]), ("failed", 7))
            self.assertIn("native-only-output", "\n".join(row["log_tail"]))
            self.assertNotIn("native-only-output", transcript.read_text())
            self.assertEqual(len(store.rows()), 1)
            self.assertEqual(json.loads(receipt.read_text())["job_id"], status.handle)
            self.assertEqual(tracker.processes, ())
        finally:
            # Only this isolated queue belongs to this integration fixture.
            for row in store.rows(active=True):
                cancel_job(store, row["id"])
            if store.rows(active=True):
                reader = Store(self.root, read_only=True)
                try:
                    result = wait(
                        reader,
                        [row["id"] for row in store.rows(active=True)],
                        timeout=10,
                    )
                    self.assertTrue(result["complete"], result)
                finally:
                    reader.close()


if __name__ == "__main__":
    unittest.main()
