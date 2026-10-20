"""Selected stage outcomes remain visible in minimum-size terminal panes."""

import unittest
from concurrent.futures import Future

import test_tui_plan_stages as fixtures


class StageHeaderTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PlanStageDashboardTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.stage = self.fixture.report["stages"][0]
        self.stage["name"] = "長い名前-stage-" * 30
        # A stage can fail evidence validation even when its job exited zero.
        # Neither job nor enclosing plan outcome should replace stage state.
        self.stage["job"].update(state="succeeded", exit_code=0)
        self.fixture.report["state"] = "failed"

    def test_all_stage_states_precede_long_names_and_keep_the_exact_log_handle(self):
        for size in ((8, 20), (24, 100)):
            for state in (
                "pending",
                "queued",
                "running",
                "succeeded",
                "failed",
                "lost",
                "cancelled",
                "skipped",
            ):
                with self.subTest(size=size, state=state):
                    self.stage["state"] = state
                    frames = self.fixture.dashboard([ord("V"), 9, ord("q")], size)
                    lines = list(frames[-1].values())
                    self.assertTrue(
                        any(line.startswith(state + " > ") for line in lines), lines
                    )
                    self.assertIn("Enter " + self.stage["job_id"], lines)
                    self.assertEqual(self.stage["job"]["state"], "succeeded")
                    self.assertEqual(self.stage["job"]["exit_code"], 0)

    def test_accepted_refresh_updates_the_pinned_stage_state_without_switching_job(
        self,
    ):
        now = [100]
        self.stage["state"] = "running"
        keys = iter([ord("V"), 9, -1, ord("q")])

        def key():
            value = next(keys)
            if value == -1:
                self.stage["state"] = "lost"
                now[0] = 102
            return value

        frames = self.fixture.dashboard(key, (8, 20), clock=lambda: now[0])
        self.assertTrue(frames[2][4].startswith("running > "))
        self.assertTrue(frames[-1][4].startswith("lost > "))
        self.assertEqual(frames[-1][5], "Enter " + self.stage["job_id"])

    def test_failed_refresh_retains_observed_state_with_stale_warning(self):
        now, reads, pending = [100], [0], Future()
        self.stage["state"] = "lost"

        def submit(loader, request):
            if request == ("plans", None):
                reads[0] += 1
                if reads[0] > 1:
                    return pending
            result = Future()
            try:
                result.set_result(loader(request))
            except Exception as error:
                result.set_exception(error)
            return result

        keys = iter([ord("V"), 9, -1, -1, ord("q")])

        def key():
            value = next(keys)
            if value == -1 and now[0] == 100:
                self.stage["state"] = "succeeded"
                now[0] = 103
            elif value == -1:
                pending.set_exception(OSError("fixture plan read failed"))
            return value

        frames = self.fixture.dashboard(
            key, (8, 20), background_submit=submit, clock=lambda: now[0]
        )
        self.assertTrue(frames[-1][4].startswith("lost > "))
        self.assertIn("STALE", frames[-1][6])
        self.assertEqual(frames[-1][5], "Enter " + self.stage["job_id"])
        self.assertEqual(reads[0], 2)
