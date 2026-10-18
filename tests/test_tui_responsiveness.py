"""The native observer retains failures and never reuses ambiguous evidence."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "tui_observer",
    Path(__file__).resolve().parent.parent / "tools/verify_tui_responsiveness.py",
)
observer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(observer)


class ResponsivenessReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.baseline, self.candidate = self.root / "baseline", self.root / "candidate"
        for root in (self.baseline, self.candidate):
            (root / "agent_builds").mkdir(parents=True)
            (root / "agent_builds/tui.py").write_text("fixture")
        self.output = self.root / "report.json"
        patcher = mock.patch.multiple(
            observer,
            ROOT=self.candidate,
            parent_budget=mock.Mock(return_value=dict(slots=1, memory_gb=2)),
            identity=mock.Mock(side_effect=lambda root: root.name),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def measured(root, expected, mode, pair):
        return dict(
            mode=mode,
            pair=pair,
            median_seconds=0.01,
            p95_seconds=0.02,
            max_seconds=0.03,
            help_wrap_calls=1,
            quit_seconds=0.01,
        )

    def test_complete_counterbalanced_arms_are_retained_without_speed_claim(self):
        with mock.patch.object(
            observer, "measure", side_effect=self.measured
        ) as measure:
            result = observer.run(self.baseline, self.output)
        self.assertEqual(
            [(call.args[2], call.args[3]) for call in measure.call_args_list],
            [("baseline", 0), ("candidate", 0), ("candidate", 1), ("baseline", 1)],
        )
        self.assertEqual(result["result"], "passed")
        self.assertFalse(result["source_changed"])
        self.assertNotIn("current_arm", result)
        self.assertEqual(json.loads(self.output.read_text()), result)

    def test_failure_and_interruption_preserve_completed_and_unknown_current_arm(self):
        for exception in (
            RuntimeError("fixture failure"),
            KeyboardInterrupt(),
            SystemExit(143),
        ):
            output = self.root / ("report-" + type(exception).__name__ + ".json")
            with self.subTest(exception=type(exception).__name__), mock.patch.object(
                observer,
                "measure",
                side_effect=[self.measured(None, None, "baseline", 0), exception],
            ):
                with self.assertRaises(type(exception)):
                    observer.run(self.baseline, output)
            result = json.loads(output.read_text())
            self.assertEqual(len(result["records"]), 1)
            self.assertEqual(result["current_arm"], dict(mode="candidate", pair=0))
            self.assertEqual(
                result["result"],
                "interrupted"
                if isinstance(exception, (KeyboardInterrupt, SystemExit))
                else "failed",
            )
            self.assertIsNone(result["source_changed"])

    def test_source_change_prevents_passing_report(self):
        with mock.patch.object(
            observer, "measure", side_effect=self.measured
        ), mock.patch.object(
            observer,
            "identity",
            side_effect=["old-baseline", "old-candidate", "changed", "old-candidate"],
        ):
            with self.assertRaisesRegex(ValueError, "Source changed"):
                observer.run(self.baseline, self.output)
        result = json.loads(self.output.read_text())
        self.assertEqual(result["result"], "failed")
        self.assertTrue(result["source_changed"])

    def test_existing_symlink_and_source_destinations_are_rejected_before_measurement(
        self,
    ):
        existing = self.root / "existing"
        existing.write_text("keep")
        link = self.root / "link"
        link.symlink_to(self.root / "missing")
        for path in (
            existing,
            link,
            self.baseline / "report",
            self.candidate / "report",
        ):
            with self.subTest(path=path), mock.patch.object(
                observer, "measure"
            ) as measure:
                with self.assertRaises(ValueError):
                    observer.run(self.baseline, path)
                measure.assert_not_called()
        self.assertEqual(existing.read_text(), "keep")
        self.assertFalse((self.root / "missing").exists())

    def test_baseline_must_be_a_separate_checkout(self):
        with self.assertRaisesRegex(ValueError, "separate checkout"):
            observer.run(self.candidate, self.output)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
