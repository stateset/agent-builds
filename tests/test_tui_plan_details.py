"""Focused plan evidence wraps, remains bounded, and follows stage identity."""

import copy
import curses
import unittest
from concurrent.futures import Future
from unittest import mock

import test_tui_plan_stages as fixtures
from agent_builds import tui
from agent_builds.tui_plan_details import PlanDetails, stage_details
from agent_builds.tui_plan_stages import PlanStages


def report():
    result = fixtures.plan()
    for item in result["stages"]:
        item["job"].update(
            reason="",
            exit_code=1,
            wait_seconds=2,
            elapsed_seconds=3,
            command_display="check " + item["name"],
            source={},
            started=None,
        )
    return result


class PlanDetailTests(unittest.TestCase):
    def test_focus_excludes_other_stage_evidence_and_overview_includes_it(self):
        selected, data = PlanStages(), report()
        view = PlanDetails(selected)
        self.assertIn("check tests", "\n".join(view.page_plan(data, 99, 100)))
        selected.choose(data, 1)
        text = "\n".join(view.page_plan(data, 99, 100))
        self.assertIn("check lint", text)
        self.assertNotIn("check tests", text)
        self.assertIn(data["id"], text)
        self.assertIn("2 total", text)

    def test_long_commands_wrap_and_resize_preserves_reviewed_position(self):
        selected, data = PlanStages(), report()
        selected.choose(data, 1)
        data["stages"][0]["job"]["command_display"] = " ".join(
            "TOKEN{:03d}".format(i) for i in range(150)
        )
        view = PlanDetails(selected)
        view.page_plan(data, 39, 2)
        view.scroll(10)
        target = view.page_plan(data, 39, 2)[0]
        self.assertIn("TOKEN", target)
        view.page_plan(data, 19, 1)
        view.page_plan(data, 99, 1000)
        self.assertEqual(view.page_plan(data, 39, 2)[0], target)
        self.assertIn("TOKEN149", "".join(view.wrapped))
        self.assertTrue(all(len(line) <= 39 for line in view.wrapped))

    def test_new_stage_and_recorded_job_reset_reading_position(self):
        selected, data = PlanStages(), report()
        selected.choose(data, 1)
        data["stages"][0]["reason"] = "long reason " * 200
        view = PlanDetails(selected)
        view.page_plan(data, 19, 2)
        view.end()
        view.page_plan(data, 19, 2)
        self.assertGreater(view.offset, 0)
        changed = copy.deepcopy(data)
        changed["stages"][0]["job_id"] = "c" * 12
        changed["stages"][0]["job"]["id"] = "c" * 12
        self.assertEqual(view.page_plan(changed, 19, 2)[0], "Stage lint | failed")
        self.assertEqual(view.offset, 0)
        selected.choose(changed, 1)
        self.assertEqual(view.page_plan(changed, 19, 2)[0], "Stage tests | faile")
        self.assertEqual(view.offset, 0)

    def test_unknown_exit_and_identity_mismatch_do_not_borrow_evidence(self):
        item = report()["stages"][0]
        item["job"]["exit_code"] = None
        self.assertIn("exit not recorded", "\n".join(stage_details(item)))
        item["job"]["id"] = "c" * 12
        item["job"]["reason"] = "WRONG JOB REASON"
        text = "\n".join(stage_details(item))
        self.assertIn("identity mismatch", text)
        self.assertNotIn("WRONG JOB", text)
        self.assertNotIn("check lint", text)

    def test_bounded_sanitized_details_and_idle_wrapping_cache(self):
        selected, data = PlanStages(), report()
        selected.choose(data, 1)
        data["stages"][0]["reason"] = "\x1b[31m" + "reason " * 20000
        view = PlanDetails(selected)
        view.page_plan(data, 39, 3)
        self.assertEqual(view.logical[-1], view.clip_notice)
        self.assertNotIn("\x1b", "\n".join(view.wrapped))
        with mock.patch(
            "agent_builds.tui_text.wrap_line", side_effect=AssertionError("idle rewrap")
        ):
            view.page_plan(data, 39, 3)
            view.page_plan(data, 39, 2)


class PlanDetailDashboardTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PlanStageDashboardTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.report["stages"][1]["reason"] = (
            "BEGIN-REASON " + "detail " * 60 + " END-REASON"
        )

    def test_paging_keeps_stage_handle_and_snapshot_status_visible(self):
        for size in ((24, 100), (14, 40)):
            with self.subTest(size=size):
                frames = self.fixture.dashboard(
                    [ord("V"), 9, 9, curses.KEY_END, curses.KEY_HOME, ord("q")], size
                )
                for frame in frames[3:]:
                    text = "\n".join(frame.values())
                    self.assertIn("> tests", text)
                    self.assertIn("Enter " + self.fixture.fixture.jobs[2], text)
                    self.assertTrue("plans" in text.lower(), text)
                self.assertIn("Stage tests", "\n".join(frames[-1].values()))

    def test_selected_stage_shows_wrapped_reason_without_other_stage_command(self):
        frames = self.fixture.dashboard([ord("V"), 9, 9, ord("q")], (30, 65))
        text = "\n".join(frames[-1].values())
        self.assertIn("BEGIN-REASON", text)
        self.assertIn("Stage tests", text)
        self.assertNotIn("Stage lint", text)

    def test_failed_refresh_keeps_selected_evidence_labelled_stale(self):
        now, reads = [100], [0]

        def submit(loader, key):
            future = Future()
            try:
                if key == ("plans", None):
                    reads[0] += 1
                    if reads[0] > 1:
                        raise OSError("fixture plan read failed")
                future.set_result(loader(key))
            except Exception as error:
                future.set_exception(error)
            return future

        for size in ((24, 100), (14, 40), (8, 20)):
            now[0], reads[0] = 100, 0
            keys = iter([ord("V"), 9, 9, -1, -1, ord("q")])

            def key():
                value = next(keys)
                if value == -1:
                    now[0] = 103
                return value

            with self.subTest(size=size):
                frames = self.fixture.dashboard(
                    key, size, background_submit=submit, clock=lambda: now[0]
                )
                text = "\n".join(frames[-1].values())
                self.assertIn("stale", text.casefold())
                self.assertIn("> tests", text)
                self.assertIn("Enter " + self.fixture.fixture.jobs[2], text)
                self.assertEqual(reads[0], 2)
