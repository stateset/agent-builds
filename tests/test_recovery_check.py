"""Real native children survive runner loss without losing their locks."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools import verify_recovery as recovery


class RecoveryCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.archive = tempfile.TemporaryDirectory(prefix="buildr-recovery-old-")
        cls.old = recovery.baseline(Path(cls.archive.name))

    @classmethod
    def tearDownClass(cls):
        cls.archive.cleanup()

    def check_case(self, checkout, cancel):
        with tempfile.TemporaryDirectory() as directory:
            result = recovery.run_case(Path(directory) / "case", checkout, cancel)
        self.assertEqual(result["result"], "passed")
        self.assertEqual(result["outcome"], "cancelled" if cancel else "lost")
        self.assertEqual(result["native_attempts"], 1)
        self.assertEqual(result["recoveries_while_child_live"], 4)
        self.assertTrue(result["secondary_lock_preserved"])
        self.assertTrue(result["unrelated_work_completed"])
        return result

    def test_current_orphan_completion_stays_unknown_without_retry(self):
        result = self.check_case(recovery.ROOT, False)
        self.assertEqual(result["holder_version"], recovery.__version__)
        self.assertIsNone(result["exit_code"])

    def test_current_orphan_explicit_cancellation_releases_only_after_exit(self):
        self.check_case(recovery.ROOT, True)

    def test_old_loaded_runner_orphan_retains_locks_during_upgrade(self):
        result = self.check_case(self.old, False)
        self.assertEqual(result["holder_version"], "0.24.2")
        self.assertIsNone(result["exit_code"])

    def test_old_loaded_runner_orphan_can_be_cancelled_by_current_code(self):
        result = self.check_case(self.old, True)
        self.assertEqual(result["holder_version"], "0.24.2")

    def test_unqueued_driver_creates_no_output(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"AGENT_BUILDS_JOB_ID": ""}):
                with self.assertRaisesRegex(ValueError, "shared queue"):
                    recovery.run(Path(directory) / "report.json")
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_invalid_cycles_never_launch(self):
        with mock.patch.object(recovery, "parent_budget") as budget:
            for cycles in (0, 11, True, 1.5):
                with self.assertRaisesRegex(ValueError, "cycles"):
                    recovery.run("/tmp/not-created-recovery.json", cycles)
            budget.assert_not_called()

    def test_existing_report_never_launches_or_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text("retain me")
            with mock.patch.object(recovery, "parent_budget", return_value={}):
                with mock.patch.object(recovery, "baseline") as archive:
                    with self.assertRaisesRegex(ValueError, "new path"):
                        recovery.run(path)
                    archive.assert_not_called()
            self.assertEqual(path.read_text(), "retain me")

    def test_failure_keeps_completed_cases_and_stops_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            fixture = Path(directory) / "fixture"
            fixture.mkdir()
            with mock.patch.object(recovery, "parent_budget", return_value={}):
                with mock.patch.object(
                    recovery.tempfile, "mkdtemp", return_value=str(fixture)
                ):
                    with mock.patch.object(recovery, "baseline", return_value=fixture):
                        with mock.patch.object(
                            recovery, "capture", return_value={"status": "captured"}
                        ):
                            with mock.patch.object(
                                recovery,
                                "run_case",
                                side_effect=[
                                    {"result": "passed"},
                                    ValueError("native fixture fault"),
                                ],
                            ) as case:
                                with self.assertRaisesRegex(
                                    ValueError, "native fixture fault"
                                ):
                                    recovery.run(output, cycles=2)
            report = json.loads(output.read_text())
            self.assertEqual(report["result"], "failed")
            self.assertEqual(report["error"], "native fixture fault")
            self.assertEqual(len(report["cases"]), 1)
            self.assertEqual(case.call_count, 2)
            progress = json.loads(
                output.with_name(output.name + ".progress.json").read_text()
            )
            self.assertEqual(progress["fixture_directory"], str(fixture))
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
