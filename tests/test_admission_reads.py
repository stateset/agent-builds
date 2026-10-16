"""Admission stays independent of bulky execution evidence and presentation."""

import contextlib
import json
from pathlib import Path
import sqlite3
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
    def without_evidence_reads(self):
        denied = []

        def authorize(action, table, column, database, trigger):
            if (
                action == sqlite3.SQLITE_READ
                and table == "jobs"
                and column in {"source", "usage", "batch", "admission"}
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


if __name__ == "__main__":
    unittest.main()
