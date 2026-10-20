"""Focused rendering reclaims rows without hiding global queue evidence."""

import unittest

import test_tui_browser as fixtures


class FocusLayoutTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TuiBrowserTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_toggle_reclaims_two_rows_and_restores_metrics(self):
        frames = self.fixture.dashboard(
            [ord("i"), ord("z"), ord("z"), ord("q")], (20, 100)
        )

        def heading(frame):
            return next(
                row for row, text in frame.items() if text.startswith("JOB DETAILS")
            )

        self.assertEqual(heading(frames[1]) - heading(frames[2]), 2)
        self.assertEqual(heading(frames[1]), heading(frames[-1]))
        focused = frames[2]
        self.assertIn("Managed reservations", focused[1])
        self.assertIn("waiting", focused[2])
        self.assertTrue(all(0 <= row < 20 for row in focused))
        self.assertIn("q", focused[19])

    def test_help_uses_actual_geometry_and_retains_focus_preference(self):
        frames = self.fixture.dashboard(
            [ord("z"), ord("?"), ord("q"), ord("q")], (20, 100)
        )
        helps = [f for f in frames if f.get(0, "").startswith("BUILDR HELP")]
        self.assertTrue(helps)
        self.assertTrue(all(0 <= row < 20 for f in helps for row in f))
        self.assertIn("[focus:", frames[-1][0])
        self.assertIn("Managed reservations", frames[-1][1])

    def test_minimum_pane_keeps_existing_compact_layout(self):
        frames = self.fixture.dashboard([ord("z"), ord("q")], (8, 20))
        self.assertEqual(frames[0], frames[-1])
        self.assertTrue(all(0 <= row < 8 for f in frames for row in f))
        self.assertIn(":", frames[-1][7])

    def test_focus_layout_and_log_round_trip_keep_job_identity(self):
        frames = self.fixture.dashboard(
            [ord("z"), ord("l"), ord("b"), ord("q")], (20, 100)
        )
        logs = [f for f in frames if f.get(0, "").startswith("LOG ")]
        self.assertTrue(logs)
        self.assertTrue(all(self.fixture.jobs[0] in f[1] for f in logs))
        self.assertIn("[focus:", frames[-1][0])
        self.assertIn(self.fixture.jobs[0], "\n".join(frames[-1].values()))
