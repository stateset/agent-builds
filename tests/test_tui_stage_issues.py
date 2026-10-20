"""Explicit failed/lost navigation never infers outcomes or dispatches work."""

import copy
import curses
import unittest
from concurrent.futures import Future

import test_tui_plan_stages as fixtures
from agent_builds.tui_plan_stages import PlanStages


class IssueSelectionTests(unittest.TestCase):
    def test_forward_and_reverse_wrap_in_stage_order_from_any_selection(self):
        selected = PlanStages()
        report = fixtures.plan(
            stages=[
                fixtures.stage("pending", None, "pending"),
                fixtures.stage("artifact", state="failed"),
                fixtures.stage("ok", state="succeeded"),
                fixtures.stage("unknown", state="lost"),
            ]
        )
        for direction, expected in (
            (1, ["artifact", "unknown", "artifact"]),
            (-1, ["unknown", "artifact", "unknown"]),
        ):
            selected.name = None
            for name in expected:
                self.assertTrue(selected.issue(report, direction))
                self.assertEqual(selected.name, name)
        selected.name = "ok"
        selected.issue(report, 1)
        self.assertEqual(selected.name, "unknown")
        selected.name = "ok"
        selected.issue(report, -1)
        self.assertEqual(selected.name, "artifact")

    def test_no_observed_issue_and_single_current_issue_preserve_selection(self):
        selected = PlanStages()
        report = fixtures.plan(
            stages=[
                fixtures.stage("ok", state="succeeded"),
                fixtures.stage("pending", None, "pending"),
                fixtures.stage("skipped", None, "skipped"),
                fixtures.stage("cancelled", None, "cancelled"),
            ]
        )
        selected.choose(report, 1)
        for direction in (-1, 1):
            self.assertFalse(selected.issue(report, direction))
            self.assertEqual(selected.name, "ok")
        report["stages"][0]["state"] = "failed"
        self.assertFalse(selected.issue(report, 1))
        self.assertEqual(selected.name, "ok")
        self.assertFalse(selected.issue(None, 1))
        self.assertFalse(selected.issue(fixtures.plan(stages=[]), -1))

    def test_stage_failure_does_not_change_successful_native_job_or_unknown_log(self):
        artifact = fixtures.stage("artifact", state="failed")
        artifact["job"].update(state="succeeded", exit_code=0)
        lost = fixtures.stage("lost", None, "lost")
        report, selected = fixtures.plan(stages=[artifact, lost]), PlanStages()
        before = copy.deepcopy(report)
        selected.issue(report, 1)
        self.assertEqual(selected.name, "artifact")
        self.assertEqual(selected.log_target(report), "a" * 12)
        selected.issue(report, 1)
        self.assertEqual(selected.name, "lost")
        self.assertIsNone(selected.log_target(report))
        self.assertEqual(report, before)

    def test_refresh_does_not_jump_until_requested_and_uses_new_stage_order(self):
        report, selected = fixtures.plan(), PlanStages()
        selected.issue(report, 1)
        changed = copy.deepcopy(report)
        changed["stages"][0]["state"] = "succeeded"
        changed["stages"].reverse()
        self.assertEqual(selected.current(changed)["name"], "lint")
        selected.issue(changed, 1)
        self.assertEqual(selected.name, "tests")


class IssueDashboardTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PlanStageDashboardTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_issue_jump_opens_exact_log_and_back_retains_stage_in_narrow_panes(self):
        for size in ((24, 100), (8, 20)):
            frames = self.fixture.dashboard(
                [ord("V"), ord("]"), 10, -1, ord("b"), ord("q")], size
            )
            logs = [frame for frame in frames if frame.get(0, "").startswith("LOG ")]
            self.assertTrue(logs)
            self.assertTrue(
                all(self.fixture.fixture.jobs[2] in frame[1] for frame in logs)
            )
            self.assertIn("> tests", "\n".join(frames[-1].values()))

    def test_issue_navigation_abandons_pending_reply_without_retry(self):
        pending, calls = Future(), []

        def submit(loader, request):
            calls.append(request)
            return pending

        keys = iter([ord("V"), 9, 10, ord("]"), -1, ord("q")])

        def key():
            value = next(keys)
            if value == -1:
                pending.set_result(self.fixture.report["stages"][0]["job"])
            return value

        frames = self.fixture.dashboard(key, link_submit=submit)
        self.assertEqual(len(calls), 1)
        self.assertFalse(any(frame.get(0, "").startswith("LOG ") for frame in frames))
        self.assertIn("> tests", "\n".join(frames[-1].values()))

    def test_no_issue_keeps_reviewed_detail_page_and_never_opens_a_log(self):
        for stage in self.fixture.report["stages"]:
            stage["state"] = "succeeded"
        self.fixture.report["stages"][0]["reason"] = "reviewed detail " * 100
        frames = self.fixture.dashboard(
            [ord("V"), 9, curses.KEY_END, ord("]"), ord("["), ord("q")]
        )
        self.assertEqual(frames[-3], frames[-2])
        self.assertEqual(frames[-2], frames[-1])
        self.assertIn("> lint", "\n".join(frames[-1].values()))
        self.assertFalse(any(frame.get(0, "").startswith("LOG ") for frame in frames))

    def test_help_consumes_issue_keys_without_changing_stage(self):
        frames = self.fixture.dashboard(
            [ord("V"), 9, ord("?"), ord("]"), ord("["), 27, ord("q")]
        )
        self.assertIn("> lint", "\n".join(frames[-1].values()))
