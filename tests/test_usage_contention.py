"""Optional telemetry must not turn writer contention into a native failure."""

import io
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from agent_builds import runner
from agent_builds.__main__ import parser
from agent_builds.store import Store


class UsageContentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state")
        self.store.update_config(dict(min_free_gb=0, min_available_gb=0, nice=0))
        self.other = Store(self.store.root)
        self.job = self.store.submit(
            self.root, ["true"], "local", "quick", 1, 0.25, "key", "", 10
        )

    def tearDown(self):
        self.other.close()
        self.store.close()
        self.temp.cleanup()

    def test_busy_writer_defers_sample_without_changes_and_restores_timeout(self):
        self.store.record_usage(self.job, {"samples": 1})
        before = self.store.db.execute("PRAGMA busy_timeout").fetchone()[0]
        self.other.db.execute("BEGIN IMMEDIATE")
        try:
            self.assertFalse(self.store.poll_usage(self.job, {"samples": 2}))
            self.assertEqual(self.store.get(self.job)["usage"]["samples"], 1)
            self.assertFalse(self.store.db.in_transaction)
            self.assertEqual(
                self.store.db.execute("PRAGMA busy_timeout").fetchone()[0], before
            )
        finally:
            self.other.db.execute("ROLLBACK")
        self.assertTrue(self.store.poll_usage(self.job, {"samples": 3}))
        self.assertEqual(self.store.get(self.job)["usage"]["samples"], 3)

    def test_body_error_propagates_once_and_rolls_back(self):
        with mock.patch.object(
            self.store,
            "record_usage",
            side_effect=sqlite3.OperationalError("database is locked"),
        ) as write:
            with self.assertRaisesRegex(sqlite3.OperationalError, "database is locked"):
                self.store.poll_usage(self.job, {"samples": 2})
        write.assert_called_once()
        self.assertFalse(self.store.db.in_transaction)
        self.assertEqual(self.store.get(self.job)["usage"], {})

    def test_native_outcome_and_final_telemetry_commit_in_one_statement(self):
        trace = []
        self.store.db.set_trace_callback(trace.append)
        self.store.finish(self.job, "failed", 7, usage={"samples": 4, "complete": True})
        updates = [sql for sql in trace if sql.startswith("UPDATE jobs SET")]
        self.assertEqual(len(updates), 1)
        row = self.store.get(self.job)
        self.assertEqual(row["exit_code"], 7)
        self.assertTrue(row["usage"]["complete"])

    def test_failed_final_write_never_leaves_a_partial_outcome(self):
        self.store.db.execute(
            "CREATE TRIGGER reject_usage BEFORE UPDATE OF usage ON jobs BEGIN SELECT RAISE(ABORT,'fixture rejection'); END"
        )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "fixture rejection"):
            self.store.finish(self.job, "failed", 7, usage={"complete": True})
        row = self.store.get(self.job)
        self.assertEqual(row["state"], "queued")
        self.assertIsNone(row["exit_code"])
        self.assertEqual(row["usage"], {})

    def test_real_native_failure_survives_a_busy_periodic_sample(self):
        # Other tests use this metadata-only submission; it must not remain an
        # unexecuted priority head in this real-runner fixture.
        self.store.finish(self.job, "cancelled", 130, "unused metadata fixture")
        state, attempts, finished = self.store.root, [], []
        case = self

        class ContendedStore(Store):
            def poll_usage(instance, job, usage):
                blocker = Store(state)
                try:
                    blocker.db.execute("BEGIN IMMEDIATE")
                    deferred = super().poll_usage(job, usage)
                    case.assertFalse(deferred)
                    attempts.append(job)
                finally:
                    blocker.db.execute("ROLLBACK")
                    blocker.close()
                return deferred

            def finish(instance, job, status, code, reason="", usage=None):
                finished.append((job, code, usage))
                return super().finish(job, status, code, reason, usage)

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
                "10",
                "--",
                "python3",
                "-c",
                "import os; os.write(1,b'X'*1048576); raise SystemExit(7)",
            ]
        )
        args.quiet_output = True
        env = dict(os.environ, AGENT_BUILDS_STATE=str(state))
        env.pop("AGENT_BUILDS_JOB_ID", None)  # isolated fixture only
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(
            runner, "Store", ContendedStore
        ), mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(runner.execute(args), 7)
        self.assertTrue(attempts)
        self.assertEqual(len(finished), 1)
        handle, code, usage = finished[0]
        self.assertEqual(code, 7)
        self.assertTrue(usage["complete"])
        self.assertEqual(self.store.get(handle)["exit_code"], 7)
        self.assertEqual(
            self.store.log_path(handle).read_bytes().partition(b"\n")[2], b"X" * 1048576
        )


if __name__ == "__main__":
    unittest.main()
