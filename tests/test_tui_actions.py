"""Action discovery is read-only; accepted choices use established review flows."""

import unittest
from unittest import mock

import test_tui_browser as fixtures
import test_tui_plan_stages as plan_fixtures
import test_tui_views as picker_fixtures
from agent_builds import tui
from agent_builds.tui_actions import blocked, choose, commands
from agent_builds.tui_views import ViewPicker


class ActionPickerTests(unittest.TestCase):
    def picker(self, keys, size=(20, 100), job=None, view=0):
        helper = picker_fixtures.ViewPickerTerminalTests()
        with mock.patch.object(
            picker_fixtures,
            "choose",
            side_effect=lambda screen, current, has_job, paint, has_log=False: choose(
                screen, view, job, None, False, paint
            ),
        ):
            return helper.picker(keys, size)

    def test_type_to_find_views_and_actions_in_full_and_minimum_panes(self):
        for size in ((20, 100), (8, 20)):
            command, frames = self.picker(list("queue health") + ["\n"], size)
            self.assertEqual(command.view, 11)
            self.assertIn("BUILDR ACTIONS", " ".join(frames[-1].values()))
        command, unused = self.picker(list("new job") + ["\n"])
        self.assertEqual(command.key, ord("n"))

    def test_typing_action_keys_or_quit_never_dispatches(self):
        command, frames = self.picker(list("ncapbq") + ["\n", "\x1b"])
        self.assertIsNone(command)
        self.assertIn("No matching actions", " ".join(frames[-1].values()))

    def test_disabled_action_requires_resize_and_another_enter(self):
        command, frames = self.picker(
            list("new job") + ["\n", ((20, 100), -1), "\n"], (8, 20)
        )
        self.assertEqual(command.key, ord("n"))
        self.assertTrue(
            any("Resize to 65x15" in " ".join(frame.values()) for frame in frames)
        )

    def test_terminal_job_and_missing_job_actions_explain_unavailability(self):
        for job in (None, dict(id="a" * 12, state="succeeded")):
            catalog = commands(0, job)
            action = next(c for c in catalog if c.number == "cancel")
            self.assertTrue(blocked(action, 20, 100, job is not None))
        catalog = commands(13, dict(id="a" * 12, state="running"))
        self.assertNotIn("new_job", [c.number for c in catalog])
        self.assertTrue(
            blocked(next(c for c in catalog if c.number == "focus"), 20, 100, True)
        )

    def test_catalog_search_is_independent_of_view_picker_and_exposes_target(self):
        catalog = commands(0, dict(id="a" * 12, state="running"))
        picker = ViewPicker(None, catalog)
        picker.search("job logs")
        self.assertEqual(picker.choice().view, 13)
        self.assertTrue(picker.choice().description.startswith("a" * 12))
        normal = ViewPicker(0)
        normal.search("new job")
        self.assertFalse(normal.matches)


class ActionRoutingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TuiBrowserTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def dashboard(self, keys, selection, **kwargs):
        def accepted(screen, view, job, plan, focused, paint):
            return (
                next(
                    c
                    for c in commands(view, job, plan, focused)
                    if c.number == selection
                )
                if selection
                else None
            )

        with mock.patch.object(tui, "choose_action", side_effect=accepted):
            return self.fixture.dashboard(keys, **kwargs)

    def test_new_job_menu_opens_review_and_escape_submits_nothing(self):
        self.dashboard([ord(":"), ord("q")], "new_job", answers=[None])
        self.assertEqual(len(self.fixture.prompt_calls), 1)
        self.assertEqual(self.fixture.prompt_calls[0].args[1], "Working directory")

    def test_cancel_menu_requires_existing_exact_job_confirmation(self):
        self.dashboard([ord(":"), ord("q")], "cancel", answers=["no"])
        self.assertEqual(len(self.fixture.prompt_calls), 1)
        self.assertIn(self.fixture.jobs[0], self.fixture.prompt_calls[0].args[1])
        self.assertIn("type yes", self.fixture.prompt_calls[0].args[1])

    def test_cancelled_menu_keeps_selection_and_query(self):
        frames = self.dashboard([ord("j"), ord(":"), ord("q")], None)
        self.assertIn(self.fixture.jobs[1], "\n".join(frames[-1].values()))
        self.assertFalse(self.fixture.prompt_calls)

    def test_view_action_reuses_existing_inspection_pinning(self):
        frames = self.dashboard([ord(":"), ord("q")], "view.14")
        self.assertIn("WHY WAITING", "\n".join(frames[-1].values()))
        self.assertIn(self.fixture.jobs[0], "\n".join(frames[-1].values()))

    def test_plan_stage_menu_uses_confirmed_stage_job_and_returns_to_stage(self):
        helper = plan_fixtures.PlanStageDashboardTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        captured = []

        def accepted(screen, view, job, plan, focused, paint):
            captured.append(job["id"] if job else None)
            return next(
                c for c in commands(view, job, plan, focused) if c.number == "view.13"
            )

        with mock.patch.object(tui, "choose_action", side_effect=accepted):
            frames = helper.dashboard(
                [ord("V"), 9, 9, ord(":"), -1, ord("b"), ord("q")]
            )
        self.assertEqual(captured, [helper.fixture.jobs[2]])
        logs = [f for f in frames if f.get(0, "").startswith("LOG ")]
        self.assertTrue(logs)
        self.assertTrue(all(helper.fixture.jobs[2] in f[1] for f in logs))
        self.assertIn("> tests", "\n".join(frames[-1].values()))
