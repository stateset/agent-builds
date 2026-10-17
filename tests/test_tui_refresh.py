"""Dashboard snapshots keep keyboard input independent of slow inspection."""

from concurrent.futures import Future
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import textwrap
import unittest
from unittest import mock

import test_tui_browser as fixtures

from agent_builds import tui_data
from agent_builds.store import Store


def completed(loader, key):
    future = Future()
    try:
        future.set_result(loader(key))
    except Exception as error:
        future.set_exception(error)
    return future


def frame_text(frame):
    return "\n".join(frame.values())


class TuiRefreshTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TuiBrowserTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root
        self.store = self.fixture.store
        self.jobs = self.fixture.jobs

    def test_each_pending_baseline_read_preserves_paint_help_and_quit(self):
        for dimensions in ((20, 120), (12, 40)):
            for target, label in (
                ("queue", "Loading queue snapshot"),
                ("host", "Loading host metrics"),
                ("summary", "Loading queue summary"),
            ):
                pending, calls = Future(), []

                def submit(loader, key):
                    if key == target:
                        calls.append(key)
                        return pending
                    return completed(loader, key)

                with self.subTest(dimensions=dimensions, target=target):
                    frames = self.fixture.dashboard(
                        [ord("?"), -1, ord("?"), ord("q")],
                        dimensions,
                        background_submit=submit,
                    )
                    self.assertEqual(len(frames), 4)
                    self.assertTrue(all(frames))
                    self.assertIn("BUILDR HELP", frame_text(frames[1]))
                    self.assertEqual(calls, [target])
                    self.assertFalse(pending.done())
                    if target != "host" or dimensions[1] >= 65:
                        self.assertIn(label, frame_text(frames[-1]))
                    else:
                        self.assertIn(self.jobs[0], frame_text(frames[-1]))
                    self.assertEqual(self.fixture.prompt_calls, [])

    def test_pending_summary_never_uses_filtered_jobs_as_global_reservations(self):
        for dimensions in ((20, 160), (12, 40)):
            pending = Future()

            def submit(loader, key):
                return pending if key == "summary" else completed(loader, key)

            with self.subTest(dimensions=dimensions):
                frames = self.fixture.dashboard(
                    [ord("f"), ord("f"), ord("q")],
                    dimensions,
                    background_submit=submit,
                )
                self.assertIn("waiting", frame_text(frames[-1]))
                for frame in frames:
                    text = frame_text(frame)
                    if dimensions[1] >= 65:
                        self.assertIn("Managed reservations: unknown", text)
                        self.assertNotIn("Managed reservations: 0/", text)
                        self.assertNotIn("Managed reservations: 1/", text)
                    else:
                        self.assertIn("CPU reservations ?", text)
                        self.assertIn("RAM reservations ?", text)
                        self.assertNotIn("CPU 0/", text)
                        self.assertNotIn("CPU 1/", text)
                self.assertFalse(pending.done())

    def test_uninitialized_summary_cannot_report_zero_after_browser_sees_queue(self):
        uninitialized = tui_data.summary(self.root / "not-created-yet")
        self.assertFalse(uninitialized["initialized"])
        for dimensions in ((20, 160), (12, 40)):

            def submit(loader, key):
                if key == "summary":
                    future = Future()
                    future.set_result(uninitialized)
                    return future
                return completed(loader, key)

            with self.subTest(dimensions=dimensions):
                frames = self.fixture.dashboard(
                    [ord("q")], dimensions, background_submit=submit
                )
                text = frame_text(frames[-1])
                self.assertIn(self.jobs[0], text)
                self.assertIn("Queue not initialized", text)
                if dimensions[1] >= 65:
                    self.assertIn("Managed reservations: unknown", text)
                    self.assertNotIn("Managed reservations: 0/", text)
                else:
                    self.assertIn("CPU reservations ?", text)
                    self.assertIn("RAM reservations ?", text)
                    self.assertNotIn("CPU 0/", text)

    def test_initial_read_failures_are_visible_and_retry_only_when_due(self):
        for target, label, interval in (
            ("queue", "Queue snapshot unavailable", 1),
            ("host", "Host metrics unavailable", 1),
            ("summary", "Queue summary unavailable", 2),
        ):
            now, calls = [100], []
            updates = iter((100.25, 100.9, 100 + interval))

            def submit(loader, key):
                if key == target:
                    calls.append(now[0])
                    if len(calls) == 1:
                        failed = Future()
                        failed.set_exception(
                            sqlite3.OperationalError("fixture read failed")
                        )
                        return failed
                return completed(loader, key)

            def key():
                try:
                    now[0] = next(updates)
                except StopIteration:
                    return ord("q")
                return -1

            with self.subTest(target=target):
                frames = self.fixture.dashboard(
                    key,
                    (20, 160),
                    clock=lambda: now[0],
                    background_submit=submit,
                )
                self.assertEqual(calls, [100, 100 + interval])
                for frame in frames[:-1]:
                    self.assertIn(label, frame_text(frame))
                    self.assertIn("fixture read failed", frame_text(frame))
                self.assertNotIn("fixture read failed", frame_text(frames[-1]))
                self.assertIn(self.jobs[0], frame_text(frames[-1]))

    def test_browser_refresh_failure_retains_snapshot_and_selected_job(self):
        self.store.log_path(self.jobs[0]).write_text("wrong job output\n")
        self.store.log_path(self.jobs[1]).write_text("selected job retained\n")
        for dimensions in ((20, 160), (12, 40)):
            now, reads = [100], []
            keys = iter((ord("j"), -1, ord("l"), ord("q")))

            def submit(loader, key):
                if key == "queue":
                    reads.append(now[0])
                    if len(reads) > 1:
                        failed = Future()
                        failed.set_exception(OSError("fixture queue unavailable"))
                        return failed
                return completed(loader, key)

            def key():
                value = next(keys)
                if value == -1:
                    now[0] = 101
                return value

            with self.subTest(dimensions=dimensions):
                frames = self.fixture.dashboard(
                    key,
                    dimensions,
                    clock=lambda: now[0],
                    background_submit=submit,
                )
                stale = frame_text(frames[2])
                self.assertIn(
                    "STALE queue" if dimensions[1] >= 65 else "BUILDR STALE jobs",
                    stale,
                )
                self.assertIn(self.jobs[1], stale)
                self.assertIn("selected job retained", frame_text(frames[-1]))
                self.assertNotIn("wrong job output", frame_text(frames[-1]))
                self.assertIn(self.jobs[1], frame_text(frames[-1]))
                self.assertEqual(reads, [100, 101])

    def test_slow_refresh_labels_old_global_snapshots_and_keeps_selected_job(self):
        self.store.log_path(self.jobs[1]).write_text("selected through slow refresh\n")
        for dimensions in ((24, 200), (14, 50)):
            now = [100]
            calls = dict(queue=0, summary=0)
            pending = dict(queue=Future(), summary=Future())
            keys = iter((ord("j"), -1, ord("l"), ord("q")))

            def submit(loader, key):
                if key in calls:
                    calls[key] += 1
                    if calls[key] > 1:
                        return pending[key]
                return completed(loader, key)

            def key():
                value = next(keys)
                if value == -1:
                    now[0] = 105
                return value

            with self.subTest(dimensions=dimensions):
                frames = self.fixture.dashboard(
                    key,
                    dimensions,
                    clock=lambda: now[0],
                    background_submit=submit,
                )
                text = frame_text(frames[2])
                if dimensions[1] >= 65:
                    self.assertGreaterEqual(
                        text.count("Snapshot 5s ago; refreshing"), 2
                    )
                    self.assertIn("Managed reservations: 1/", text)
                    self.assertNotIn("Managed reservations: 0/", text)
                else:
                    self.assertIn("BUILDR STALE jobs", text)
                    self.assertIn("STALE CPU 1/", text)
                    self.assertNotIn("CPU 0/", text)
                self.assertIn(self.jobs[1], text)
                self.assertIn("selected through slow refresh", frame_text(frames[-1]))
                self.assertIn(self.jobs[1], frame_text(frames[-1]))
                self.assertEqual(calls, dict(queue=2, summary=2))
                self.assertTrue(all(not future.done() for future in pending.values()))

    def test_missing_queue_is_not_created_and_monitor_never_opens_a_writer(self):
        missing = self.root / "absent-state"
        opened, attributes = [], []
        original_init = Store.__init__
        original_attribute = tui_data.LazyStore.__getattr__

        def initialize(store, root=None, read_only=False):
            opened.append((root, read_only))
            self.assertTrue(read_only, "monitor opened a writable Store")
            original_init(store, root, read_only=read_only)

        def attribute(store, name):
            attributes.append(name)
            self.assertNotEqual(name, "recover_if_due", "monitor attempted recovery")
            return original_attribute(store, name)

        with mock.patch.object(Store, "__init__", initialize), mock.patch.object(
            tui_data.LazyStore, "__getattr__", attribute
        ), mock.patch.object(
            tui_data.LazyStore,
            "writable",
            side_effect=AssertionError("monitor requested writable access"),
        ):
            frames = self.fixture.dashboard(
                [ord("?"), ord("?"), ord("q")],
                (20, 160),
                state_path=missing,
            )
        self.assertTrue(opened)
        self.assertTrue(all(read_only for _, read_only in opened))
        self.assertNotIn("recover_if_due", attributes)
        self.assertIn("Queue not initialized", frame_text(frames[-1]))
        self.assertIn("Managed reservations: unknown", frame_text(frames[-1]))
        self.assertFalse(missing.exists())

    def test_pending_details_discard_late_evidence_after_job_switch(self):
        self.store.db.execute(
            "UPDATE jobs SET source=? WHERE id=?",
            (json.dumps({"submission": {"head": "OLD_JOB_EVIDENCE"}}), self.jobs[0]),
        )
        self.store.db.execute(
            "UPDATE jobs SET source=? WHERE id=?",
            (
                json.dumps({"submission": {"head": "CURRENT_JOB_EVIDENCE"}}),
                self.jobs[1],
            ),
        )
        details_mode, pending = [False], []
        keys = iter((ord("i"), ord("j"), -1, -1, ord("q")))

        def submit(loader, key):
            if details_mode[0] and key in self.jobs:
                future = Future()
                pending.append((key, future))
                return future
            return completed(loader, key)

        def key():
            value = next(keys)
            if value == ord("i"):
                details_mode[0] = True
            elif value == -1:
                job, future = pending[-1]
                future.set_result(self.store.get(job))
            return value

        frames = self.fixture.dashboard(key, (46, 160), background_submit=submit)
        self.assertEqual([job for job, _ in pending], self.jobs[:2])
        self.assertIn("Loading job details", frame_text(frames[1]))
        self.assertTrue(
            all("OLD_JOB_EVIDENCE" not in frame_text(frame) for frame in frames)
        )
        self.assertIn("CURRENT_JOB_EVIDENCE", frame_text(frames[-1]))
        self.assertIn(self.jobs[1], frame_text(frames[-1]))

    def test_pending_wait_context_discards_late_observation_after_job_switch(self):
        pending = []
        keys = iter((ord("j"), -1, -1, ord("q")))

        def submit(loader, key):
            if key in self.jobs:
                future = Future()
                pending.append((key, future))
                return future
            return completed(loader, key)

        def key():
            value = next(keys)
            if value == -1:
                job, future = pending[-1]
                future.set_result(
                    dict(
                        disk_free_gb=111 if job == self.jobs[0] else 222,
                        required_free_gb=0,
                        measured_at=123,
                    )
                )
            return value

        frames = self.fixture.dashboard(key, (20, 160), background_submit=submit)
        self.assertEqual([job for job, _ in pending], self.jobs[:2])
        self.assertTrue(all("disk 111.0" not in frame_text(frame) for frame in frames))
        self.assertIn("disk 222.0/0.0", frame_text(frames[-1]))

    def test_pause_toggles_current_config_instead_of_display_snapshot(self):
        changes, queue_reads = [], []
        original_update = Store.update_config
        first = [True]

        def submit(loader, key):
            if key == "queue":
                queue_reads.append(key)
            return completed(loader, key)

        def key():
            if first[0]:
                first[0] = False
                current = self.store.config()
                current["paused"] = True
                self.store.config_path.write_text(json.dumps(current))
                return ord("p")
            return ord("q")

        def update(store, requested):
            self.assertFalse(store.read_only)
            self.assertTrue(self.store.config()["paused"])
            changes.append(dict(requested))
            return original_update(store, requested)

        frames = self.fixture.dashboard(
            key,
            (20, 160),
            background_submit=submit,
            on_update=update,
        )
        self.assertEqual(queue_reads, ["queue"])
        self.assertNotIn("PAUSED", frame_text(frames[0]))
        self.assertEqual(changes, [{"paused": False}])
        self.assertFalse(self.store.config()["paused"])
        self.assertIn("Queue resumed.", frame_text(frames[-1]))

    def test_pending_tool_reads_preserve_help_hiding_and_quit(self):
        for view, kind in (
            (1, "processes"),
            (2, "announcements"),
            (5, "workers"),
            (7, "plans"),
            (8, "advisor"),
            (9, "groups"),
            (10, "watches"),
        ):
            target = "processes" if view == 1 else (kind, None)
            pending, submitted, now = Future(), [], [100]
            keys = iter(
                (
                    ord("V"),
                    ord("?"),
                    ord("p"),
                    ord("q"),
                    ord("V"),
                    ord("?"),
                    ord("q"),
                    ord("q"),
                )
            )

            def submit(loader, key):
                if key == target:
                    submitted.append(key)
                    return pending
                return completed(loader, key)

            def key():
                now[0] += 10
                return next(keys)

            with self.subTest(view=view), mock.patch(
                "agent_builds.tui.choose_view", side_effect=(view, 0)
            ):
                frames = self.fixture.dashboard(
                    key,
                    (24, 160),
                    clock=lambda: now[0],
                    background_submit=submit,
                )
                self.assertEqual(len(frames), 8)
                self.assertIn("Loading " + kind, frame_text(frames[1]))
                self.assertIn("Loading " + kind, frame_text(frames[4]))
                self.assertIn("BUILDR HELP", frame_text(frames[2]))
                self.assertIn("BUILDR HELP", frame_text(frames[3]))
                self.assertIn("BUILDR HELP", frame_text(frames[6]))
                self.assertEqual(submitted, [target])
                self.assertFalse(pending.done())
                self.assertEqual(self.fixture.prompt_calls, [])
                self.assertFalse(self.store.config()["paused"])

    def test_failed_tool_loads_show_errors_and_still_accept_quit(self):
        for view, kind in (
            (2, "announcements"),
            (5, "workers"),
            (7, "plans"),
            (8, "advisor"),
            (9, "groups"),
            (10, "watches"),
        ):
            with self.subTest(view=view), mock.patch(
                "agent_builds.tui.choose_view", return_value=view
            ), mock.patch(
                "agent_builds.tui.read_tool",
                side_effect=sqlite3.OperationalError("no such table: fixture_legacy"),
            ) as load:
                frames = self.fixture.dashboard([ord("V"), -1, ord("q")], (24, 160))
                self.assertEqual(load.call_count, 1)
                self.assertEqual(load.call_args.args[1], (kind, None))
                text = frame_text(frames[-1])
                self.assertIn(kind.capitalize() + " unavailable", text)
                self.assertIn("no such table: fixture_legacy", text)
                self.assertNotIn("Traceback", text)

    def test_empty_successful_tool_snapshot_remains_stale_after_refresh_error(self):
        for view, kind in (
            (1, "processes"),
            (2, "announcements"),
            (7, "plans"),
            (9, "groups"),
            (10, "watches"),
        ):
            sizes = ((24, 160), (14, 50)) if view in (7, 10) else ((24, 160),)
            for dimensions in sizes:
                target = "processes" if view == 1 else (kind, None)
                now, submissions = [100], []
                keys = iter((ord("V"), -1, ord("q")))

                def submit(loader, key):
                    if key == target:
                        submissions.append(now[0])
                        future = Future()
                        if len(submissions) == 1:
                            future.set_result([])
                        else:
                            future.set_exception(OSError("fixture refresh failed"))
                        return future
                    return completed(loader, key)

                def key():
                    value = next(keys)
                    if value == -1:
                        now[0] = 101
                    return value

                with self.subTest(view=view, dimensions=dimensions), mock.patch(
                    "agent_builds.tui.choose_view", return_value=view
                ):
                    frames = self.fixture.dashboard(
                        key,
                        dimensions,
                        clock=lambda: now[0],
                        background_submit=submit,
                    )
                    self.assertEqual(submissions, [100, 101])
                    self.assertNotIn("Loading " + kind, frame_text(frames[1]))
                    self.assertIn(
                        "Stale " + kind + ": fixture refresh failed",
                        frame_text(frames[-1]),
                    )

    def test_unverified_controller_liveness_never_claims_stopped_or_suggests_resume(
        self,
    ):
        plan = dict(
            id="fixture-plan",
            state="running",
            name="Active plan",
            stages=[],
            elapsed_seconds=1,
            controller_alive=None,
            reason="",
        )
        watch = dict(
            id="fixture-watch",
            state="waiting",
            definition=dict(name="Active watch", external_ref="fixture-external"),
            probe_count=0,
            external_idle_seconds=1,
            accounting=dict(local_reserved_seconds=0),
            probes=[],
            reason="",
            complete=False,
            controller_alive=None,
        )
        for view, report in ((7, plan), (10, watch)):
            for dimensions in ((40, 160), (14, 50)):
                with self.subTest(view=view, dimensions=dimensions), mock.patch(
                    "agent_builds.tui.choose_view", return_value=view
                ), mock.patch("agent_builds.tui.read_tool", return_value=[report]):
                    frames = self.fixture.dashboard([ord("V"), ord("q")], dimensions)
                    text = frame_text(frames[-1])
                    self.assertIn(report["id"], text)
                    self.assertNotIn("Controller stopped", text)
                    self.assertNotIn("Controller is not running", text)
                    self.assertNotIn("r resumes", text)
                    if dimensions[1] >= 65:
                        self.assertIn(
                            "Controller liveness is not verified by read-only inspection.",
                            text,
                        )

    def test_disk_scan_errors_remain_visible_and_do_not_prevent_quit(self):
        for error in (
            sqlite3.OperationalError("fixture database unavailable"),
            RuntimeError("fixture inventory failure"),
        ):
            failed = Future()
            failed.set_exception(error)
            with self.subTest(error=type(error).__name__), mock.patch(
                "agent_builds.tui.DiskRead", autospec=True
            ) as disk_read:
                disk_read.return_value.start.return_value = failed
                frames = self.fixture.dashboard([ord("d"), ord("q")], (24, 160))
                disk_read.return_value.start.assert_called_once_with(str(self.root))
                self.assertTrue(disk_read.return_value.close.called)
                self.assertIn("Disk scan failed: " + str(error), frame_text(frames[-1]))
                self.assertNotIn("Traceback", frame_text(frames[-1]))
                self.assertEqual(self.fixture.prompt_calls, [])

    def test_interpreter_exits_with_a_stalled_dashboard_disk_scan(self):
        metadata_path = self.root / "disk-worker.json"
        worker_script = textwrap.dedent(
            """
            import json
            import os
            from pathlib import Path
            import socket
            import subprocess
            import sys
            from threading import Event

            def identity(pid):
                stat = Path('/proc/{}/stat'.format(pid)).read_text()
                return dict(pid=pid, start=stat[stat.rfind(')') + 2:].split()[19])

            descendant = subprocess.Popen(
                [sys.executable, '-c', 'import time; time.sleep(300)'],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            metadata = dict(leader=identity(os.getpid()), descendant=identity(descendant.pid))
            Path(sys.argv[1]).write_text(json.dumps(metadata))
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as ready:
                ready.connect(sys.argv[2])
                ready.sendall(b'ready')
            Event().wait()
            """
        )
        script = textwrap.dedent(
            """
            import json
            from pathlib import Path
            import socket
            import sys
            from unittest import mock

            sys.path.insert(0, str(Path.cwd() / 'tests'))
            import test_tui_browser as fixtures

            fixture = fixtures.TuiBrowserTests()
            fixture.setUp()
            metadata_path = Path(sys.argv[1])
            from threading import Event
            import time
            ready_path = fixture.root / 'disk-ready.sock'
            ready = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            ready.bind(str(ready_path))
            ready.listen(1)
            ready.settimeout(3)
            keys = iter((ord('d'), ord('q')))
            before = [tuple(row) for row in fixture.store.db.execute('SELECT * FROM jobs ORDER BY id')]
            config_before = fixture.store.config_path.read_bytes()

            def alive(identity):
                try:
                    stat = Path('/proc/{}/stat'.format(identity['pid'])).read_text()
                except FileNotFoundError:
                    return False
                fields = stat[stat.rfind(')') + 2:].split()
                return fields[19] == identity['start'] and fields[0] not in ('Z', 'X')

            def key():
                value = next(keys)
                if value == ord('q'):
                    connection, _ = ready.accept()
                    with connection:
                        assert connection.recv(5) == b'ready'
                    metadata = json.loads(metadata_path.read_text())
                    assert all(alive(identity) for identity in metadata.values())
                return value

            try:
                command = [sys.executable, '-c', sys.argv[2], str(metadata_path), str(ready_path)]
                with mock.patch('agent_builds.tui_disk.worker_command', return_value=command) as worker_command:
                    fixture.dashboard(key)
                assert worker_command.call_count == 1
                metadata = json.loads(metadata_path.read_text())
                deadline=time.monotonic()+1
                while any(alive(identity) for identity in metadata.values()) and time.monotonic()<deadline:
                    Event().wait(.01)
                assert not alive(metadata['leader']), 'disk worker still running after q'
                assert not alive(metadata['descendant']), 'disk descendant still running after q'
                after = [tuple(row) for row in fixture.store.db.execute('SELECT * FROM jobs ORDER BY id')]
                assert before == after, 'disk inspection changed queue jobs'
                assert config_before == fixture.store.config_path.read_bytes()
                print('dashboard quit; owned disk processes stopped', flush=True)
            finally:
                ready.close()
                fixture.tearDown()
            """
        )

        def alive(identity):
            try:
                stat = Path("/proc/{}/stat".format(identity["pid"])).read_text()
            except FileNotFoundError:
                return False
            fields = stat[stat.rfind(")") + 2 :].split()
            return fields[19] == identity["start"] and fields[0] not in ("Z", "X")

        try:
            result = subprocess.run(
                [sys.executable, "-c", script, str(metadata_path), worker_script],
                cwd=str(Path(__file__).resolve().parent.parent),
                env=dict(os.environ),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertIn(
                b"dashboard quit; owned disk processes stopped", result.stdout
            )
            metadata = json.loads(metadata_path.read_text())
            self.assertTrue(all(not alive(identity) for identity in metadata.values()))
        finally:
            # Clean up only fixture identities if the regression itself fails.
            if metadata_path.exists():
                import signal

                metadata = json.loads(metadata_path.read_text())
                group = metadata["leader"]["pid"]
                for identity in metadata.values():
                    if alive(identity):
                        try:
                            if os.getpgid(identity["pid"]) == group:
                                os.killpg(group, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
