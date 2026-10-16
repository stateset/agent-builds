"""Admission stays independent of bulky execution evidence and presentation."""

import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

from agent_builds.store import Store


class AdmissionReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state")
        self.store.update_config(
            dict(
                cpu_slots=4,
                memory_gb=4,
                max_running=4,
                max_heavy=2,
                min_free_gb=0,
                min_available_gb=0,
                quick_reserve_slots=0,
                quick_reserve_jobs=0,
                quick_reserve_memory_gb=0,
                nice=0,
            )
        )

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def job(self, key, locks=None):
        return self.store.submit(
            self.root,
            ["printf", "%s", "a quoted ' argument"],
            "local",
            "quick",
            1,
            0.25,
            key,
            "",
            5,
            locks=locks,
            source={"evidence": "x" * 65536},
        )

    @contextlib.contextmanager
    def without_evidence_reads(self, extra=()):
        denied = []
        forbidden = {"source", "usage", "batch", "admission"} | set(extra)

        def authorize(action, table, column, database, trigger):
            if (
                action == sqlite3.SQLITE_READ
                and table == "jobs"
                and column in forbidden
            ):
                denied.append(column)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        self.store.db.set_authorizer(authorize)
        try:
            yield
        finally:
            # Python 3.8 cannot disable the callback with None (added in 3.11).
            self.store.db.set_authorizer(lambda *args: sqlite3.SQLITE_OK)
        self.assertEqual(denied, [])

    def test_admission_and_reservations_do_not_read_execution_evidence(self):
        first = self.job("one")
        independent = self.job("two", ["extra"])
        tail = self.job("one")
        with self.without_evidence_reads(), mock.patch.object(
            self.store, "describe", side_effect=AssertionError("presentation read")
        ):
            self.assertTrue(self.store.claim(first))
            self.assertFalse(self.store.claim(tail))
            self.assertTrue(self.store.claim(independent))
            self.assertFalse(self.store.claim(first))
        self.assertEqual(self.store.counts()["running"], 2)
        context = self.store.get(tail)["wait_context"]
        self.assertEqual(context["blocking_job"]["id"], first)
        self.assertEqual(context["contributing_jobs"][0]["id"], first)
        self.assertIn("quoted", context["contributing_jobs"][0]["command_display"])

    def test_blocker_display_matches_detailed_inspection_for_held_and_running(self):
        head = self.job("one", ["extra"])
        tail = self.job("extra")
        for admitted in (False, True):
            if admitted:
                self.assertTrue(self.store.claim(head))
            with mock.patch("agent_builds.store.time.time", return_value=2000000000):
                expected = self.store.get(head)
                self.assertFalse(self.store.claim(tail))
                actual = self.store.get(tail)["wait_context"]["blocking_job"]
                self.assertEqual(actual, {key: expected[key] for key in actual})
        self.assertEqual(actual["state"], "running")

    def test_cancelled_and_missing_jobs_do_not_gain_reservations(self):
        job = self.job("one", ["extra"])
        self.store.cancel(job)
        with self.without_evidence_reads():
            self.assertFalse(self.store.poll_claim(job))
            with self.assertRaisesRegex(ValueError, "Unknown job"):
                self.store.poll_claim("absent")
        self.assertFalse(self.store.db.in_transaction)
        self.assertEqual(self.store.counts()["running"], 0)
        self.assertEqual(self.store.get(job)["storage_state"], "held")

    def test_queued_backlog_does_not_read_all_commands_or_wait_contexts(self):
        jobs = [self.job("repo-" + str(index)) for index in range(30)]
        huge = json.dumps({"large_unused_field": "x" * 65536})
        # Only the requesting job's wait history is needed by admission.
        self.store.db.execute(
            "UPDATE jobs SET wait_context=?,wait_history=? WHERE id!=?",
            (huge, huge, jobs[-1]),
        )
        selects = []
        self.store.db.set_trace_callback(selects.append)
        try:
            with self.without_evidence_reads():
                self.assertFalse(self.store.claim(jobs[-1]))
        finally:
            self.store.db.set_trace_callback(None)
        backlog = [sql for sql in selects if "ORDER BY created,id" in sql]
        self.assertEqual(len(backlog), 1)
        projection = backlog[0].split(" FROM jobs")[0]
        for unused in ("*", "command", "wait_context", "wait_history", "herdr"):
            self.assertNotIn(unused, projection)
        self.assertEqual(
            self.store.get(jobs[-1])["wait_context"]["blocking_job"]["id"], jobs[0]
        )

    def test_recovery_uses_identity_fields_and_preserves_uncertain_reservations(self):
        queued = self.job("queued")
        cancelled = self.job("cancelled")
        child = self.job("live-child")
        docker = self.job("docker")
        self.store.db.execute(
            "UPDATE jobs SET cancel_requested=1 WHERE id=?", (cancelled,)
        )
        self.store.db.execute(
            "UPDATE jobs SET state='running',child_pid=123,child_start='live' WHERE id=?",
            (child,),
        )
        self.store.db.execute(
            "UPDATE jobs SET state='running',backend='docker' WHERE id=?", (docker,)
        )
        with self.without_evidence_reads(), mock.patch(
            "agent_builds.store.alive",
            side_effect=lambda pid, start: pid == 123 and start == "live",
        ):
            self.store.recover()
        self.assertEqual(self.store.get(queued)["state"], "lost")
        self.assertEqual(self.store.get(cancelled)["state"], "cancelled")
        self.assertEqual(self.store.get(child)["state"], "running")
        self.assertIn("child still running", self.store.get(child)["reason"])
        self.assertEqual(self.store.get(docker)["state"], "running")
        self.assertIn("reconcile Docker", self.store.get(docker)["reason"])

    def test_execution_control_refreshes_cancel_and_pid_identity_without_evidence(self):
        job = self.job("live")
        self.assertTrue(self.store.claim(job))
        self.store.db.execute(
            "UPDATE jobs SET child_pid=123,child_start='first' WHERE id=?", (job,)
        )
        with self.without_evidence_reads(), mock.patch.object(
            self.store, "describe", side_effect=AssertionError("presentation read")
        ):
            first = dict(self.store.get_execution_control(job))
            self.store.db.execute(
                "UPDATE jobs SET child_pid=456,child_start='second',cancel_requested=1 WHERE id=?",
                (job,),
            )
            second = dict(self.store.get_execution_control(job))
        self.assertEqual(
            first,
            dict(
                id=job,
                state="running",
                cancel_requested=0,
                backend="local",
                child_pid=123,
                child_start="first",
            ),
        )
        self.assertEqual(
            (second["child_pid"], second["child_start"], second["cancel_requested"]),
            (456, "second", 1),
        )

    def test_browser_projection_matches_full_inspection_without_evidence_or_wait_payloads(
        self,
    ):
        first = self.job("one")
        self.assertTrue(self.store.claim(first))
        self.job("two", ["extra"])
        failed = self.job("failed")
        self.store.finish(failed, "failed", 7)
        with mock.patch("agent_builds.store.time.time", return_value=2000000000):
            full = self.store.rows()
            with self.without_evidence_reads(
                ("wait_context", "wait_history")
            ), mock.patch.object(
                self.store, "describe", side_effect=AssertionError("presentation read")
            ):
                rows = self.store.browser_rows()
            self.assertEqual([row["id"] for row in rows], [row["id"] for row in full])
            for row, detailed in zip(rows, full):
                self.assertEqual(row, {key: detailed[key] for key in row})
            self.assertEqual(rows[1]["storage_state"], "held")
            self.assertEqual(rows[1]["state"], "queued")

    def test_browser_limits_and_read_only_empty_queues_remain_bounded(self):
        for index in range(201):
            self.store.submit(
                self.root,
                ["true"],
                "local",
                "quick",
                1,
                0.25,
                "repo-" + str(index),
                "",
                5,
            )
        reader = Store(self.store.root, read_only=True)
        try:
            self.assertEqual(len(reader.browser_rows()), 200)
            self.assertEqual(len(reader.browser_rows(2)), 2)
            for invalid in (0, -1, 201, True, "2"):
                with self.assertRaises(ValueError):
                    reader.browser_rows(invalid)
            self.assertEqual(reader.db.total_changes, 0)
        finally:
            reader.close()
        missing = self.root / "empty-browser"
        reader = Store(missing, read_only=True)
        try:
            self.assertEqual(reader.browser_rows(), [])
            with self.assertRaisesRegex(ValueError, "Unknown job"):
                reader.get_wait_context("missing")
            self.assertFalse(missing.exists())
        finally:
            reader.close()

    def test_selected_wait_context_does_not_read_other_payloads(self):
        job = self.job("waiting")
        context = {"blocking_job": {"id": "blocker"}, "measured_at": 123}
        self.store.db.execute(
            "UPDATE jobs SET wait_context=? WHERE id=?", (json.dumps(context), job)
        )
        with self.without_evidence_reads(("wait_history", "command", "herdr")):
            self.assertEqual(self.store.get_wait_context(job), context)
        for invalid in ("[]", "null", "invalid JSON"):
            self.store.db.execute(
                "UPDATE jobs SET wait_context=? WHERE id=?", (invalid, job)
            )
            with self.assertRaises(ValueError):
                self.store.get_wait_context(job)

    def test_cancellation_never_expands_evidence_and_preserves_held_storage(self):
        for state in (
            "queued",
            "held",
            "running",
            "cancelling",
            "succeeded",
            "failed",
            "cancelled",
            "lost",
        ):
            with self.subTest(state=state):
                job = self.job(state)
                self.store.db.execute(
                    "UPDATE jobs SET state=? WHERE id=?", (state, job)
                )
                terminal = state in ("succeeded", "failed", "cancelled", "lost")
                with self.without_evidence_reads(), mock.patch.object(
                    self.store,
                    "describe",
                    side_effect=AssertionError("presentation read"),
                ):
                    self.assertEqual(self.store.cancel(job), not terminal)
                    control = dict(self.store.get_control(job))
                expected = (
                    state if terminal or state in ("queued", "held") else "cancelling"
                )
                self.assertEqual(
                    control, dict(state=expected, cancel_requested=int(not terminal))
                )
                self.assertFalse(self.store.db.in_transaction)
        with self.assertRaisesRegex(ValueError, "Unknown job"):
            self.store.cancel("missing")
        self.assertFalse(self.store.db.in_transaction)

    def test_live_reservation_snapshot_omits_execution_evidence(self):
        first, second = self.job("one"), self.job("two")
        self.assertTrue(self.store.claim(first))
        self.assertTrue(self.store.claim(second))
        with mock.patch("agent_builds.store.time.time", return_value=2000000000):
            expected = [
                self.store.reservation(row)
                for row in self.store.db.execute(
                    "SELECT * FROM jobs WHERE state='running' ORDER BY started"
                )
            ]
            with self.without_evidence_reads(), mock.patch.object(
                self.store, "describe", side_effect=AssertionError("presentation read")
            ):
                result = self.store.reservation_snapshot()
            self.assertEqual(result["jobs"], expected)
            self.assertEqual(result["observed_at"], 2000000000)
        self.store.finish(first, "succeeded", 0)
        with self.without_evidence_reads():
            self.assertEqual(
                [row["id"] for row in self.store.reservation_snapshot()["jobs"]],
                [second],
            )

    def test_execution_control_is_read_only_and_missing_state_is_not_initialized(self):
        job = self.job("one")
        reader = Store(self.store.root, read_only=True)
        try:
            self.assertEqual(
                dict(reader.get_execution_control(job)),
                dict(self.store.get_execution_control(job)),
            )
            with self.assertRaisesRegex(ValueError, "Unknown job"):
                reader.get_execution_control("missing")
        finally:
            reader.close()
        missing = self.root / "absent-state"
        reader = Store(missing, read_only=True)
        try:
            with self.assertRaisesRegex(ValueError, "Unknown job"):
                reader.get_execution_control("missing")
            self.assertFalse(missing.exists())
        finally:
            reader.close()

    def test_real_running_jobs_keep_polling_separate_from_diagnostics(self):
        from agent_builds import runner
        from agent_builds.__main__ import parser

        command_for = runner.command_for
        for mode, expected in (
            ("exit", 7),
            ("cancel", 130),
            ("timeout", 124),
            ("docker_unknown", 1),
        ):
            with self.subTest(mode=mode):
                deep_reads, controls = [], []
                evidence = json.dumps({"opaque_evidence": "x" * (1024 * 1024)})

                class ObservedStore(Store):
                    def submit(instance, *args, **kwargs):
                        job = super().submit(*args, **kwargs)
                        instance.db.execute(
                            "UPDATE jobs SET source=? WHERE id=?", (evidence, job)
                        )
                        return job

                    def get(instance, job):
                        row = instance.db.execute(
                            "SELECT state,child_pid FROM jobs WHERE id=?", (job,)
                        ).fetchone()
                        if (
                            row["state"] in ("running", "cancelling")
                            and row["child_pid"]
                        ):
                            deep_reads.append(job)
                        return super().get(job)

                    def get_execution_control(instance, job):
                        controls.append(job)
                        if mode == "cancel" and len(controls) == 2:
                            instance.cancel(job)
                        return super().get_execution_control(job)

                command = "import time; print('native-output',flush=True); time.sleep({}); raise SystemExit(7)".format(
                    0.6 if mode in ("exit", "docker_unknown") else 30
                )
                args = parser().parse_args(
                    [
                        "run",
                        "--cwd",
                        str(self.root),
                        "--jobs",
                        "1",
                        "--memory-gb",
                        ".25",
                        "--lane",
                        "quick",
                        "--backend",
                        "docker" if mode == "docker_unknown" else "local",
                        "--image",
                        "fixture-only",
                        "--timeout",
                        ".4" if mode == "timeout" else "5",
                        "--",
                        sys.executable,
                        "-c",
                        command,
                    ]
                )
                args.quiet_output = True
                env = dict(os.environ, AGENT_BUILDS_STATE=str(self.store.root))
                env.pop(
                    "AGENT_BUILDS_JOB_ID", None
                )  # explicitly isolated native fixture only
                with mock.patch.dict(os.environ, env, clear=True), mock.patch(
                    "agent_builds.runner.Store", ObservedStore
                ), mock.patch("sys.stdout", io.StringIO()), mock.patch(
                    "agent_builds.runner.command_for",
                    side_effect=lambda row, config: (
                        json.loads(row["command"])
                        if mode == "docker_unknown"
                        else command_for(row, config)
                    ),
                ), mock.patch("agent_builds.runner.docker_stopped", return_value=False):
                    self.assertEqual(runner.execute(args), expected)
                self.assertTrue(controls)
                self.assertEqual(deep_reads, [])
                job = self.store.get(controls[0])
                if mode == "docker_unknown":
                    # No daemon is contacted: the native wrapper is a fixture,
                    # and a mocked unknown daemon outcome must retain capacity.
                    self.assertEqual(job["state"], "cancelling")
                    self.assertIsNone(job["exit_code"])
                    self.assertIn("outcome unknown", job["reason"])
                    self.assertIn(
                        job["id"],
                        [
                            row["id"]
                            for row in self.store.reservation_snapshot()["jobs"]
                        ],
                    )
                else:
                    self.assertEqual(job["exit_code"], expected)
                    self.assertEqual(
                        job["state"], "failed" if mode == "exit" else "cancelled"
                    )
                if mode != "timeout":
                    self.assertIn(
                        "native-output", self.store.log_path(job["id"]).read_text()
                    )


if __name__ == "__main__":
    unittest.main()
