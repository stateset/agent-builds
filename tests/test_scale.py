import argparse
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_builds import scale
from agent_builds.store import Store


class ScaleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state")
        self.store.update_config(
            dict(
                cpu_slots=2,
                max_running=2,
                max_heavy=1,
                memory_gb=4,
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

    def job(self, index=0, locks=None):
        return self.store.submit(
            self.root,
            ["true"],
            "local",
            "quick",
            1,
            0.25,
            "repo-" + str(index),
            "",
            5,
            locks=locks,
        )

    def args(self, **options):
        return argparse.Namespace(
            **dict(
                dict(
                    agents=4,
                    backlog=8,
                    history=20,
                    hold=2,
                    baseline_checkout=None,
                    output=str(self.root / "scale.json"),
                ),
                **options,
            )
        )

    def test_idle_control_reads_never_expand_job_diagnostics(self):
        job = self.job()
        with mock.patch.object(
            self.store, "describe", side_effect=AssertionError("diagnostics expanded")
        ):
            self.assertEqual(
                dict(self.store.get_control(job)),
                dict(state="queued", cancel_requested=0),
            )
            self.store.db.execute(
                "UPDATE jobs SET cancel_requested=1 WHERE id=?", (job,)
            )
            self.assertEqual(self.store.get_control(job)["cancel_requested"], 1)
            with self.assertRaises(ValueError):
                self.store.get_control("missing")
        missing = self.root / "never-initialized"
        reader = Store(missing, read_only=True)
        try:
            with self.assertRaises(ValueError):
                reader.get_control("missing")
            self.assertFalse(missing.exists())
        finally:
            reader.close()

    def test_admission_decodes_each_lock_list_once_and_preserves_storage(self):
        jobs = [self.job(index, ["shared-output"]) for index in range(50)]
        stored = [
            row[0] for row in self.store.db.execute("SELECT resource_keys FROM jobs")
        ]
        encoded = set(stored)
        original = json.loads
        decoded = []

        def read(value, *args, **kwargs):
            if isinstance(value, str) and value in encoded:
                decoded.append(value)
            return original(value, *args, **kwargs)

        with mock.patch("agent_builds.locks.json.loads", side_effect=read):
            self.assertTrue(self.store.claim(jobs[0]))
        self.assertLessEqual(len(decoded), len(jobs) + 10, decoded)
        self.assertEqual(
            [row[0] for row in self.store.db.execute("SELECT resource_keys FROM jobs")],
            stored,
        )
        self.assertFalse(self.store.claim(jobs[-1]))
        self.assertIn(
            "earlier job in this repository", self.store.get(jobs[-1])["reason"]
        )

    def test_scale_requires_a_real_parent_reservation_before_creating_output(self):
        with mock.patch.dict(os.environ, {"AGENT_BUILDS_JOB_ID": ""}):
            with self.assertRaisesRegex(ValueError, "shared queue"):
                scale.run(self.args())
        self.assertFalse((self.root / "scale.json").exists())
        with mock.patch(
            "agent_builds.experiments.parent_budget",
            return_value=dict(slots=2, memory_gb=2, job_id="fixture"),
        ):
            with self.assertRaisesRegex(ValueError, "at least 4 GiB"):
                scale.run(self.args(agents=100))
        self.assertFalse((self.root / "scale.json").exists())

    def test_idle_measurement_refuses_incomplete_process_coverage(self):
        with self.assertRaises(AssertionError):
            scale.idle_delta({0: {}}, {})

    def test_native_ledger_rejects_duplicate_attempts_and_overlapping_locks(self):
        def event(index, kind, at):
            return dict(index=index, kind=kind, at=at)

        valid = [
            event(2, "start", 0),
            event(3, "start", 1),
            event(2, "end", 2),
            event(3, "end", 3),
        ]
        self.assertEqual(scale.check_events(valid, 4), 2)
        for invalid in (
            valid + [event(2, "start", 4)],
            [event(0, "start", 0)],
            [event(2, "start", 0), event(10, "start", 1)],
            [event(2, "start", 0), event(3, "start", 1), event(4, "start", 2)],
        ):
            with self.subTest(ledger=invalid):
                with self.assertRaises(AssertionError):
                    scale.check_events(invalid, 11)

    def test_real_runners_preserve_failure_cancel_loss_locks_and_budgets(self):
        # Explicit bounded inner fixture, valid in the independent CI unittest
        # step as well as the globally queued suite. No real queue is modified.
        with mock.patch(
            "agent_builds.experiments.parent_budget",
            return_value=dict(slots=2, memory_gb=2, job_id="fixture-parent"),
        ):
            result = scale.run(self.args())
        self.assertEqual(result["result"], "passed")
        arm = result["candidate"]
        self.assertEqual((arm["actual_runner_processes"], arm["peak_queued"]), (4, 12))
        self.assertEqual((arm["native_attempts"], arm["expected_failures"]), (2, 1))
        self.assertEqual(
            (arm["queued_cancel_exit"], arm["killed_runner_state"]), (130, "lost")
        )
        self.assertEqual(arm["lock_conflicts"], 0)
        self.assertLessEqual(arm["peak_reserved_slots"], 2)
        self.assertEqual(arm["idle"]["coverage"], 4)
        self.assertGreater(arm["idle"]["control_reads"], arm["idle"]["full_job_reads"])
        self.assertFalse(Path(result["fixture_directory"]).exists())
        self.assertEqual(
            json.loads((self.root / "scale.json").read_text())["result"], "passed"
        )


if __name__ == "__main__":
    unittest.main()
