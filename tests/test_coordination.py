import io
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import tarfile
import time
import unittest
from unittest import mock

from agent_builds import execution_policy, remote
from agent_builds.polling import AdmissionPoll, ExecutionPoll
from agent_builds.runner import spawn_gated
from agent_builds.store import SCHEMA_VERSION, Store, identity


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state")
        self.store.update_config(
            dict(
                cpu_slots=2,
                memory_gb=4,
                max_running=2,
                max_heavy=1,
                quick_reserve_slots=0,
                quick_reserve_jobs=0,
                quick_reserve_memory_gb=0,
                min_available_gb=0,
                min_free_gb=0,
                nice=0,
            )
        )

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def job(self):
        return self.store.submit(
            self.root, ["true"], "local", "quick", 1, 0.25, str(self.root), "", 5
        )

    def test_current_writable_opens_do_not_take_migration_write_transactions(self):
        trace = []
        connect = sqlite3.connect

        def observed(*args, **kwargs):
            db = connect(*args, **kwargs)
            db.set_trace_callback(trace.append)
            return db

        with mock.patch("agent_builds.store.sqlite3.connect", side_effect=observed):
            for _ in range(10):
                other = Store(self.store.root)
                self.assertEqual(
                    other.db.execute("PRAGMA user_version").fetchone()[0],
                    SCHEMA_VERSION,
                )
                other.close()
        self.assertFalse(
            any(
                sql.startswith(("BEGIN IMMEDIATE", "CREATE ", "ALTER ", "DROP "))
                for sql in trace
            ),
            trace,
        )
        self.assertNotIn("PRAGMA journal_mode=WAL", trace)

    def test_output_bursts_do_not_increase_control_read_frequency(self):
        job, now = self.job(), [0.0]
        poll = ExecutionPoll(self.store, job, clock=lambda: now[0])
        original = self.store.get_execution_control
        with mock.patch.object(
            self.store, "get_execution_control", wraps=original
        ) as read:
            first = poll.read()
            for _ in range(10000):
                self.assertIs(poll.read(), first)
            self.assertEqual(read.call_count, 1)
            self.store.cancel(job)
            now[0] = 0.19
            self.assertFalse(poll.read()["cancel_requested"])
            self.assertAlmostEqual(poll.wait_timeout(), 0.01)
            now[0] = 0.2
            self.assertTrue(poll.read()["cancel_requested"])
            self.assertEqual(read.call_count, 2)

    def test_control_wait_budget_tracks_the_refresh_deadline(self):
        now = [10.0]
        poll = ExecutionPoll(self.store, self.job(), clock=lambda: now[0])
        self.assertEqual(poll.wait_timeout(), 0)
        poll.read()
        self.assertAlmostEqual(poll.wait_timeout(), 0.2)
        now[0] = 10.15
        self.assertAlmostEqual(poll.wait_timeout(), 0.05)
        now[0] = 10.3
        self.assertEqual(poll.wait_timeout(), 0)
        poll.read()
        self.assertAlmostEqual(poll.wait_timeout(), 0.2)

    def test_stop_boundary_refreshes_identity_and_read_failures_propagate(self):
        job = self.job()
        poll = ExecutionPoll(self.store, job, clock=lambda: 0)
        self.store.db.execute(
            "UPDATE jobs SET child_pid=123,child_start='old' WHERE id=?", (job,)
        )
        self.assertEqual(poll.read()["child_start"], "old")
        self.store.db.execute(
            "UPDATE jobs SET child_pid=456,child_start='fresh' WHERE id=?", (job,)
        )
        self.assertEqual(poll.read()["child_pid"], 123)
        self.assertEqual(poll.read(force=True)["child_start"], "fresh")
        with mock.patch.object(
            self.store,
            "get_execution_control",
            side_effect=sqlite3.OperationalError("fixture failure"),
        ) as read:
            with self.assertRaisesRegex(sqlite3.OperationalError, "fixture failure"):
                poll.read(force=True)
            self.assertEqual(read.call_count, 1)

    def test_real_burst_output_is_complete_without_per_chunk_control_reads(self):
        self.verify_output_polling(False)

    def test_real_chatty_job_cancellation_retains_exit_130_and_fresh_stop_identity(
        self,
    ):
        self.verify_output_polling(True)

    def test_stop_control_read_cannot_hide_native_deadline_expiry(self):
        self.verify_output_polling(True, wait_for_deadline=True)

    def verify_output_polling(self, cancel, wait_for_deadline=False):
        from agent_builds import runner
        from agent_builds.__main__ import parser

        reads, stopped, guards, blocked = [], [], [], []
        requested, ready, case = [False], self.root / "output-started", self

        class Deadline(runner.LocalDeadline):
            def __init__(instance, *args, **kwargs):
                super().__init__(*args, **kwargs)
                guards.append(instance)

        class ObservedStore(Store):
            def get_execution_control(instance, job):
                reads.append((job, time.monotonic()))
                previous_request = requested[0]
                if cancel and not requested[0] and len(reads) >= 2 and ready.exists():
                    requested[0] = True
                    instance.cancel(job)
                if previous_request and wait_for_deadline and not blocked:
                    blocked.append(True)
                    case.assertTrue(guards[0].expired.wait(timeout=5))
                return super().get_execution_control(job)

        source = (
            "import os,time; from pathlib import Path; block=b'X'*4096; os.write(1,block); Path({!r}).touch(); [(os.write(1,block),time.sleep(.001)) for _ in range(10000)]".format(
                str(ready)
            )
            if cancel
            else "import os; block=b'X'*4096; [os.write(1,block) for _ in range(1024)]; os.write(2,b'END-ERR\\n'); raise SystemExit(7)"
        )
        args = parser().parse_args(
            [
                "run",
                "--cwd",
                str(self.root),
                "--lane",
                "quick",
                "--jobs",
                "1",
                "--memory-gb",
                ".25",
                "--timeout",
                "2" if wait_for_deadline else "10",
                "--",
                sys.executable,
                "-c",
                source,
            ]
        )
        args.quiet_output = True
        env = dict(os.environ, AGENT_BUILDS_STATE=str(self.store.root))
        env.pop("AGENT_BUILDS_JOB_ID", None)  # explicitly isolated native fixture only
        terminate = runner.terminate

        def stop(row):
            stopped.append(dict(row))
            return terminate(row)

        with mock.patch.dict(os.environ, env, clear=True), mock.patch(
            "agent_builds.runner.Store", ObservedStore
        ), mock.patch("sys.stdout", io.StringIO()), mock.patch(
            "agent_builds.runner.terminate", side_effect=stop
        ), mock.patch("agent_builds.runner.LocalDeadline", Deadline):
            expected = 124 if wait_for_deadline else 130 if cancel else 7
            self.assertEqual(runner.execute(args), expected)
        self.assertTrue(reads)
        row = self.store.get(reads[0][0])
        self.assertEqual(row["exit_code"], expected)
        data = self.store.log_path(row["id"]).read_bytes().partition(b"\n")[2]
        if cancel:
            self.assertTrue(data)
            self.assertEqual(row["state"], "cancelled")
            self.assertEqual(len(stopped), 1)
            self.assertTrue(requested[0])
            if wait_for_deadline:
                self.assertTrue(blocked)
                self.assertTrue(guards[0].expired.is_set())
                self.assertEqual(row["reason"], "timeout")
            self.assertGreaterEqual(
                len(reads), 3
            )  # periodic read then forced stop read
            self.assertEqual(
                (stopped[0]["child_pid"], stopped[0]["child_start"]),
                (row["child_pid"], row["child_start"]),
            )
            self.assertGreaterEqual(reads[1][1] - reads[0][1], 0.19)
        else:
            self.assertEqual(row["state"], "failed")
            self.assertEqual(data, b"X" * (4096 * 1024) + b"END-ERR\n")
            self.assertFalse(stopped)
            self.assertTrue(
                all(
                    later[1] - earlier[1] >= 0.19
                    for earlier, later in zip(reads, reads[1:])
                ),
                reads,
            )

    def test_wal_activation_holds_the_bootstrap_lock(self):
        state = self.root / "wal-bootstrap"
        observed = []
        case = self
        connect = sqlite3.connect

        class Connection(sqlite3.Connection):
            def execute(self, sql, *args, **kwargs):
                if sql == "PRAGMA journal_mode=WAL":
                    with (state / "schema.lock").open("a+b") as descriptor:
                        with case.assertRaises(BlockingIOError):
                            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    observed.append(sql)
                return super().execute(sql, *args, **kwargs)

        def instrumented(*args, **kwargs):
            return connect(*args, factory=Connection, **kwargs)

        with mock.patch("agent_builds.store.sqlite3.connect", side_effect=instrumented):
            store = Store(state)
            try:
                self.assertEqual(
                    store.db.execute("PRAGMA journal_mode").fetchone()[0], "wal"
                )
            finally:
                store.close()
        self.assertEqual(observed, ["PRAGMA journal_mode=WAL"])

    def test_concurrent_first_opens_serialize_complete_migrations(self):
        state = self.root / "concurrent-state"
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent))
        code = "from agent_builds.store import Store; import sys; s=Store(sys.argv[1]); print(s.db.execute('PRAGMA user_version').fetchone()[0]); s.close()"
        children = [
            subprocess.Popen(
                [sys.executable, "-c", code, str(state)],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(8)
        ]
        try:
            for child in children:
                output, errors = child.communicate(timeout=20)
                self.assertEqual(child.returncode, 0, errors)
                self.assertEqual(int(output.strip()), SCHEMA_VERSION)
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.communicate()
        migrated = Store(state)
        try:
            self.assertEqual(
                migrated.db.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )
        finally:
            migrated.close()

    def test_actual_previous_release_runs_against_versioned_queue_and_wakes_new_waiters(
        self,
    ):
        self.verify_previous_release("0.22.1")

    def test_actual_v0222_runner_shares_current_queue_without_schema_upgrade(self):
        self.verify_previous_release("0.22.2")

    def verify_previous_release(self, version):
        old = self.root / "previous-release"
        old.mkdir()
        archive = subprocess.check_output(
            ["git", "archive", "v" + version, "agent_builds"],
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
            bundle.extractall(str(old))
        poll = AdmissionPoll(self.store)
        self.assertTrue(poll.due())
        self.assertFalse(poll.due())
        env = dict(
            os.environ, PYTHONPATH=str(old), AGENT_BUILDS_STATE=str(self.store.root)
        )
        env.pop("AGENT_BUILDS_JOB_ID", None)  # isolated old-release native fixture only
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "agent_builds",
                "run",
                "--cwd",
                str(self.root),
                "--jobs",
                "1",
                "--memory-gb",
                ".25",
                "--timeout",
                "5",
                "--",
                sys.executable,
                "-c",
                "raise SystemExit(7)",
            ],
            cwd=str(old),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
        )
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertTrue(poll.due())
        row = self.store.rows()[0]
        self.assertEqual(
            (row["state"], row["exit_code"], row["runner_version"]),
            ("failed", 7, version),
        )
        current = Store(self.store.root)
        try:
            self.assertEqual(
                current.db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION
            )
            self.assertTrue(current.claim(self.job()))
        finally:
            current.close()

    def test_legacy_unversioned_queue_upgrades_without_removing_protocol_guards(self):
        self.store.db.execute("PRAGMA user_version=0")
        # Simulate an existing pre-versioning queue, with v2 additions absent.
        for name in ("insert", "delete", "update", "policy"):
            self.store.db.execute("DROP TRIGGER queue_event_" + name)
        self.store.db.execute("DROP TABLE queue_events")
        other = Store(self.store.root)
        try:
            self.assertEqual(
                other.db.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION
            )
            self.assertTrue(
                other.db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='protected_activation_barrier'"
                ).fetchone()
            )
        finally:
            other.close()
        self.store.db.execute("PRAGMA user_version=999")
        with self.assertRaises(ValueError):
            Store(self.store.root)

    def test_config_cache_invalidates_on_atomic_replacement_and_never_caches_policy(
        self,
    ):
        original = Path.read_text
        reads = []

        def read(path, *args, **kwargs):
            if path == self.store.config_path:
                reads.append(path)
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, "read_text", read):
            self.store._config_cache = None
            self.store.config()
            self.store.config()
            self.store.config()
            self.assertEqual(len(reads), 1)
            replacement = self.root / "config-new"
            value = json.loads(original(self.store.config_path))
            value["paused"] = True
            replacement.write_text(json.dumps(value))
            replacement.replace(self.store.config_path)
            self.assertTrue(self.store.config()["paused"])
            self.assertEqual(len(reads), 2)
            self.store.db.execute("UPDATE admission_policy SET requested=1 WHERE id=1")
            self.assertTrue(self.store.config()["protected_quick_requested"])
            self.assertEqual(len(reads), 2)

    def test_read_only_polling_ignores_telemetry_but_wakes_for_capacity_and_config(
        self,
    ):
        clock = [0.0]
        poll = AdmissionPoll(self.store, clock=lambda: clock[0])
        job = self.job()
        self.assertTrue(poll.due())
        self.assertFalse(poll.due())
        self.store.record_usage(job, dict(samples=1))
        self.store.db.execute(
            "UPDATE jobs SET reason=? WHERE id=?", ("just diagnostics", job)
        )
        self.assertFalse(poll.due())
        self.store.finish(job, "succeeded", 0)
        self.assertTrue(poll.due())
        self.assertFalse(poll.due())
        self.store.update_config(dict(paused=True))
        self.assertTrue(poll.due())
        self.assertFalse(poll.due())
        clock[0] = 2
        self.assertTrue(poll.due())  # host headroom and aging have bounded refresh

    def test_periodic_recovery_is_shared_across_connections_and_explicit_recovery_remains_available(
        self,
    ):
        other = Store(self.store.root)
        try:
            with mock.patch.object(self.store, "recover") as first, mock.patch.object(
                other, "recover"
            ) as second:
                self.assertTrue(self.store.recover_if_due())
                self.assertFalse(other.recover_if_due())
                self.assertFalse(self.store.recover_if_due())
                first.assert_called_once()
                second.assert_not_called()
                other.recover()
                second.assert_called_once()
        finally:
            other.close()

    def test_launch_gate_eof_cannot_execute_and_authorized_pid_is_preserved(self):
        marker = self.root / "ran"
        command = [
            sys.executable,
            "-c",
            "from pathlib import Path; Path({!r}).touch()".format(str(marker)),
        ]
        child, gate = spawn_gated(command, self.root, os.environ.copy())
        os.close(gate)
        self.assertEqual(child.wait(timeout=5), 125)
        child.stdout.close()
        self.assertFalse(marker.exists())
        child, gate = spawn_gated(command, self.root, os.environ.copy())
        token = identity(child.pid)
        try:
            job = self.job()
            self.store.db.execute(
                "UPDATE jobs SET child_pid=?,child_start=? WHERE id=?",
                (child.pid, token, job),
            )
            os.write(gate, b"1")
        finally:
            os.close(gate)
        self.assertEqual(child.wait(timeout=5), 0)
        child.stdout.close()
        self.assertTrue(marker.exists())
        row = self.store.get(job)
        self.assertEqual((row["child_pid"], row["child_start"]), (child.pid, token))

    def request(self):
        return dict(
            protocol=remote.PROTOCOL,
            state_dir=str(self.root / "remote"),
            action="submit",
            request_id="a" * 32,
            cwd=str(self.root),
            command=["true"],
            lane="quick",
            backend="local",
            jobs=1,
            memory_gb=0.25,
            timeout=5,
        )

    def test_batch_stop_during_spawn_cannot_launch_a_child_after_the_group_signal(self):
        import signal
        from agent_builds import batch

        marker = self.root / "late-batch-child"
        definition = batch.validate(
            dict(
                checks=[
                    dict(
                        name="late",
                        command=[
                            sys.executable,
                            "-c",
                            "import time; from pathlib import Path; time.sleep(.2); Path({!r}).touch()".format(
                                str(marker)
                            ),
                        ],
                    )
                ]
            )
        )
        argv = batch.command(definition)
        job = self.store.submit(
            self.root, argv, "local", "quick", 1, 0.25, str(self.root), "", 5
        )
        self.assertTrue(self.store.claim(job))
        spawn = subprocess.Popen

        def stopped_during_spawn(*args, **kwargs):
            os.kill(os.getpid(), signal.SIGTERM)  # handled by this private batch worker
            return spawn(*args, **kwargs)

        env = dict(AGENT_BUILDS_STATE=str(self.store.root), AGENT_BUILDS_JOB_ID=job)
        with mock.patch.dict(os.environ, env), mock.patch(
            "agent_builds.batch.subprocess.Popen", side_effect=stopped_during_spawn
        ):
            self.assertEqual(batch.run(argv[-1]), 130)
        self.assertFalse(marker.exists())
        self.assertNotEqual(
            self.store.get(job)["batch"]["checks"][0]["state"], "succeeded"
        )

    def test_timeout_before_batch_bootstrap_preserves_pending_checks_without_native_results(
        self,
    ):
        from agent_builds import batch, runner
        from agent_builds.__main__ import parser

        definition = dict(checks=[dict(name="never-started", command=["true"])])
        command = batch.command(definition)
        args = parser().parse_args(
            [
                "run",
                "--cwd",
                str(self.root),
                "--jobs",
                "1",
                "--memory-gb",
                ".25",
                "--timeout",
                ".05",
                "--",
            ]
            + command
        )
        spawn = runner.spawn_gated

        def stalled_bootstrap(argv, cwd, env):
            return spawn([sys.executable, "-c", "import time; time.sleep(5)"], cwd, env)

        env = dict(os.environ, AGENT_BUILDS_STATE=str(self.store.root))
        env.pop("AGENT_BUILDS_JOB_ID", None)  # isolated early-launch fixture only
        with mock.patch.dict(os.environ, env, clear=True), mock.patch(
            "agent_builds.runner.spawn_gated", side_effect=stalled_bootstrap
        ):
            self.assertEqual(runner.execute(args), 124)
        row = self.store.rows()[0]
        stored = json.loads(
            self.store.db.execute(
                "SELECT batch FROM jobs WHERE id=?", (row["id"],)
            ).fetchone()[0]
        )
        self.assertEqual(stored["checks"][0]["state"], "pending")
        self.assertEqual(row["batch"]["checks"][0]["state"], "skipped")
        self.assertIsNone(row["batch"]["checks"][0]["exit_code"])
        summary = batch.summary(row)
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["checks"][0]["state"], "skipped")
        self.assertIsNone(summary["worker_exit_code"])

    def test_invalid_remote_request_does_not_consume_its_token(self):
        for edit in (
            dict(command=[]),
            dict(jobs=True),
            dict(memory_gb=float("nan")),
            dict(cwd=7),
            dict(timeout=-1),
        ):
            with self.subTest(edit=edit):
                with self.assertRaises(ValueError):
                    remote.serve(dict(self.request(), **edit))
        reader = Store(self.root / "remote", read_only=True)
        try:
            self.assertEqual(
                reader.db.execute("SELECT COUNT(*) FROM remote_requests").fetchone()[0],
                0,
            )
        finally:
            reader.close()

    def test_remote_database_errors_are_protocol_json_without_tracebacks(self):
        source = io.BytesIO(json.dumps(self.request()).encode())
        output = io.StringIO()
        with mock.patch("sys.stdin", new=mock.Mock(buffer=source)), mock.patch(
            "sys.stdout", output
        ), mock.patch(
            "agent_builds.remote.serve",
            side_effect=sqlite3.OperationalError("fixture busy"),
        ):
            self.assertEqual(remote.main(), 0)
        result = json.loads(output.getvalue())
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "fixture busy")

    def test_cwd_policy_rejects_sibling_prefixes_and_symlink_escapes(self):
        inside = self.root / "allowed"
        inside.mkdir()
        outside = self.root / "allowed-sibling"
        outside.mkdir()
        (inside / "escape").symlink_to(outside, target_is_directory=True)
        with mock.patch.dict(os.environ, {}, clear=False):
            execution_policy.configure([str(inside)])
            execution_policy.check_cwd(inside)
            for path in (outside, inside / "escape"):
                with self.assertRaises(ValueError):
                    execution_policy.check_cwd(path)
            os.environ[execution_policy.VARIABLE] = "not-json"
            with self.assertRaises(ValueError):
                execution_policy.check_cwd(inside)

    def test_runner_death_before_identity_attachment_never_executes_user_command(self):
        from agent_builds.store import alive

        marker = self.root / "crash-gap-command-ran"
        wrapper = """import json,os,signal,sys
from agent_builds import runner
from agent_builds.store import identity
original=runner.spawn_gated
def crash(*args):
    child,gate=original(*args)
    print(json.dumps(dict(gate_child=child.pid,start=identity(child.pid))),flush=True)
    os.kill(os.getpid(),signal.SIGKILL)
runner.spawn_gated=crash
from agent_builds.__main__ import main
sys.exit(main())
"""
        env = dict(
            os.environ,
            AGENT_BUILDS_STATE=str(self.store.root),
            PYTHONPATH=str(Path(__file__).resolve().parent.parent),
        )
        env.pop("AGENT_BUILDS_JOB_ID", None)  # deliberately isolated fixture only
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                wrapper,
                "run",
                "--cwd",
                str(self.root),
                "--lane",
                "quick",
                "--jobs",
                "1",
                "--memory-gb",
                ".25",
                "--timeout",
                "5",
                "--",
                sys.executable,
                "-c",
                "from pathlib import Path; Path({!r}).touch()".format(str(marker)),
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, -9, result.stderr)
        proof = next(
            json.loads(line)
            for line in result.stdout.splitlines()
            if line.startswith("{")
        )
        deadline = time.monotonic() + 5
        while (
            alive(proof["gate_child"], proof["start"]) and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        self.assertFalse(alive(proof["gate_child"], proof["start"]))
        self.assertFalse(marker.exists())
        job = self.store.rows()[0]
        self.assertIsNone(job["child_pid"])
        self.store.recover()
        self.assertEqual(self.store.get(job["id"])["state"], "lost")

    def test_mcp_policy_rejects_new_out_of_scope_work_before_binding_a_key(self):
        from agent_builds import mcp

        allowed = self.root / "allowed"
        allowed.mkdir()
        env = dict(
            os.environ,
            AGENT_BUILDS_STATE=str(self.store.root),
            BUILDR_ALLOWED_ROOTS=json.dumps([str(allowed)]),
        )
        env.pop("AGENT_BUILDS_JOB_ID", None)  # private queue fixture only
        args = dict(
            cwd=str(self.root),
            request_key="out-of-root-fixture",
            command=["true"],
            jobs=1,
            memory_gb=0.25,
        )
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ValueError):
                mcp.call("buildr_submit", args)
        self.assertEqual(
            self.store.db.execute("SELECT COUNT(*) FROM mcp_requests").fetchone()[0], 0
        )

    def test_a_hundred_unchanged_waiters_do_not_take_polling_write_locks(self):
        clock = [0.0]
        waiters = [
            AdmissionPoll(self.store, clock=lambda: clock[0]) for _ in range(100)
        ]
        trace = []
        self.store.db.set_trace_callback(trace.append)
        try:
            for waiter in waiters:
                self.assertTrue(waiter.due())
            for _ in range(10):
                clock[0] += 0.1
                for waiter in waiters:
                    self.assertFalse(waiter.due())
        finally:
            self.store.db.set_trace_callback(None)
        self.assertFalse(
            any(sql.startswith(("BEGIN", "UPDATE", "INSERT")) for sql in trace)
        )
