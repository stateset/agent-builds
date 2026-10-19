"""Explicit stage navigation uses only accepted plan evidence."""

import copy
import curses
import unittest
from concurrent.futures import Future
from unittest import mock

import test_tui_browser as fixtures
from agent_builds import tui
from agent_builds.tui_plan_stages import PlanStages
from agent_builds.tui_data import LazyStore


def stage(name, handle="a" * 12, state="failed"):
    return dict(
        name=name,
        state=state,
        job_id=handle,
        job=dict(id=handle, state=state) if handle else None,
        reason="",
    )


def plan(handle="plan00000001", stages=None):
    return dict(
        id=handle,
        name="parallel checks",
        state="failed",
        reason="",
        elapsed_seconds=0,
        controller_alive=None,
        stages=stages
        if stages is not None
        else [stage("lint"), stage("tests", "b" * 12)],
    )


class PlanStageTests(unittest.TestCase):
    def test_cycles_overview_and_all_stages_in_both_directions(self):
        selected, report = PlanStages(), plan()
        self.assertIsNone(selected.current(report))
        for direction, names in (
            (1, ["lint", "tests", None]),
            (-1, ["tests", "lint", None]),
        ):
            for name in names:
                selected.choose(report, direction)
                self.assertEqual(selected.name, name)
        selected.choose(plan(stages=[]), -1)
        self.assertIsNone(selected.name)

    def test_refresh_keeps_stage_identity_after_reorder_and_insertion(self):
        selected, report = PlanStages(), plan()
        selected.choose(report, 1)
        refreshed = copy.deepcopy(report)
        refreshed["stages"] = [
            stage("new"),
            refreshed["stages"][1],
            refreshed["stages"][0],
        ]
        refreshed["stages"][2]["state"] = "succeeded"
        self.assertEqual(selected.current(refreshed)["state"], "succeeded")
        self.assertEqual(selected.name, "lint")
        self.assertEqual(selected.log_target(refreshed), "a" * 12)

    def test_removed_stage_or_different_plan_returns_to_overview(self):
        selected, report = PlanStages(), plan()
        selected.choose(report, 1)
        self.assertIsNone(selected.current(plan(stages=[stage("tests")])))
        selected.choose(report, 1)
        self.assertIsNone(selected.current(plan("plan00000002")))
        selected.choose(report, 1)
        self.assertIsNone(selected.current(None))

    def test_no_lookup_for_unsubmitted_invalid_or_unconfirmed_identity(self):
        for item in (
            stage("pending", None),
            stage("bad", "not-a-job"),
            dict(stage("missing"), job=None),
            dict(stage("mismatch"), job=dict(id="b" * 12)),
        ):
            with self.subTest(stage=item):
                selected, report = PlanStages(), plan(stages=[item])
                selected.choose(report, 1)
                self.assertIsNone(selected.log_target(report))
                self.assertEqual(selected.lines(report)[1], "No recorded log")


class PlanStageDashboardTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TuiBrowserTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.report = plan(
            stages=[
                stage("lint", self.fixture.jobs[0], "running"),
                stage("tests", self.fixture.jobs[2]),
            ]
        )
        for item in self.report["stages"]:
            item["job"] = self.fixture.store.get(item["job_id"])

    def dashboard(self, keys, size=(24, 100), **kwargs):
        config = self.fixture.store.config_path.read_bytes()
        with mock.patch.object(tui, "choose_view", return_value=7), mock.patch(
            "agent_builds.tui.read_tool",
            side_effect=lambda root, key: [copy.deepcopy(self.report)],
        ):
            with mock.patch.object(
                LazyStore, "writable", side_effect=AssertionError("queue mutation")
            ):
                frames = self.fixture.dashboard(keys, size, **kwargs)
        self.assertEqual(self.fixture.store.config_path.read_bytes(), config)
        self.assertFalse(self.fixture.prompt_calls)
        return frames

    def test_selected_stage_opens_exact_log_and_back_keeps_choice(self):
        for size in ((24, 100), (8, 20)):
            with self.subTest(size=size):
                frames = self.dashboard(
                    [ord("V"), 9, 9, 10, -1, ord("b"), ord("q")], size
                )
                log = [f for f in frames if f.get(0, "").startswith("LOG ")]
                self.assertTrue(log, frames)
                self.assertTrue(all(self.fixture.jobs[2] in f[1] for f in log))
                text = "\n".join(frames[-1].values())
                self.assertIn("> tests", text)
                self.assertIn("Enter " + self.fixture.jobs[2], text)

    def test_overview_keeps_existing_live_stage_log_preference(self):
        frames = self.dashboard([ord("V"), 10, -1, ord("q")])
        self.assertIn(self.fixture.jobs[0], frames[-1][1])

    def test_unsubmitted_stage_does_not_fall_back_to_another_job(self):
        self.report["stages"][1] = stage("pending", None, "waiting")
        with mock.patch.object(
            tui.LogLinks, "open", side_effect=AssertionError("unexpected log lookup")
        ):
            frames = self.dashboard(
                [ord("V"), curses.KEY_BTAB, 10, -1, ord("q")], (8, 20)
            )
        self.assertIn("No recorded log", "\n".join(frames[-1].values()))

    def test_changing_stage_discards_pending_log_reply(self):
        pending, requests = Future(), []

        def submit(loader, request):
            requests.append(request)
            return pending

        keys = iter([ord("V"), 9, 10, 9, -1, ord("q")])

        def key():
            value = next(keys)
            if value == -1:
                pending.set_result(self.report["stages"][0]["job"])
            return value

        frames = self.dashboard(key, link_submit=submit)
        self.assertEqual(len(requests), 1)
        self.assertFalse(any(f.get(0, "").startswith("LOG ") for f in frames))
        self.assertIn("> tests", "\n".join(frames[-1].values()))

    def test_refreshed_job_identity_discards_pending_log_reply(self):
        pending, requests, now = Future(), [], [100]

        def submit(loader, request):
            requests.append(request)
            return pending

        keys = iter([ord("V"), 9, 10, -1, -1, ord("q")])

        def key():
            value = next(keys)
            if value == -1 and now[0] == 100:
                self.report["stages"][0] = copy.deepcopy(self.report["stages"][1])
                self.report["stages"][0]["name"] = "lint"
                now[0] = 102
            elif value == -1:
                pending.set_result(self.fixture.store.get(self.fixture.jobs[0]))
            return value

        frames = self.dashboard(key, link_submit=submit, clock=lambda: now[0])
        self.assertEqual(len(requests), 1)
        self.assertFalse(any(f.get(0, "").startswith("LOG ") for f in frames))
        text = "\n".join(frames[-1].values())
        self.assertIn("> lint", text)
        self.assertIn("Enter " + self.fixture.jobs[2], text)
