"""Empty job searches have an actionable, read-only route back to jobs."""

import unittest
from unittest import mock

import test_tui_browser as fixtures
from agent_builds import tui
from agent_builds.tui_actions import blocked, commands
from agent_builds.tui_browser import empty_jobs


class EmptyJobTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TuiBrowserTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def test_menu_clears_empty_search_preserving_table_order_and_queue(self):
        before = self.fixture.store.config_path.read_bytes()
        counts = self.fixture.store.counts()
        for dimensions in ((20, 100), (8, 20)):
            with self.subTest(dimensions=dimensions), mock.patch.object(
                tui,
                "choose_action",
                side_effect=lambda screen, view, job, plan, focused, paint: next(
                    c
                    for c in commands(view, job, plan, focused)
                    if c.number == "clear_filters"
                ),
            ):
                frames = self.fixture.dashboard(
                    [9, ord("s"), ord("/"), ord(":"), ord("q")],
                    dimensions,
                    query="NO_MATCH",
                )
            empty = next(
                f for f in frames if "No matching jobs" in "\n".join(f.values())
            )
            self.assertIn(
                "C clear" if dimensions[1] < 65 else "C clears",
                "\n".join(empty.values()),
            )
            final = "\n".join(frames[-1].values())
            self.assertIn(
                "JOBS wait all" if dimensions[1] < 65 else "sort: wait", final
            )
            self.assertIn(self.fixture.jobs[1], final)
            self.assertIn("JOB", final)
        self.assertEqual(self.fixture.store.config_path.read_bytes(), before)
        self.assertEqual(self.fixture.store.counts(), counts)

    def test_empty_queue_shows_help_and_actions_in_minimum_pane(self):
        self.fixture.store.db.execute("DELETE FROM jobs")
        frames = self.fixture.dashboard([ord("q")], (8, 20))
        final = "\n".join(frames[-1].values())
        self.assertIn("No jobs yet", final)
        self.assertIn(": actions | ? help", final)
        self.assertNotIn("cargo", final)

    def test_stale_and_missing_queue_guidance_do_not_claim_current_results(self):
        self.assertIn("last snapshot", empty_jobs(True, stale=True)[0])
        self.assertTrue(
            any("current jobs may differ" in line for line in empty_jobs(stale=True))
        )
        self.assertTrue(
            any("creates nothing" in line for line in empty_jobs(initialized=False))
        )

    def test_browsing_menu_entries_work_without_job_and_stay_in_their_views(self):
        expected = dict(
            search_jobs="/",
            clear_filters="C",
            filter_jobs="f",
            sort_jobs="s",
            table="\t",
        )
        for entry in commands(0):
            if entry.number in expected:
                self.assertEqual(entry.key, ord(expected.pop(entry.number)))
                self.assertEqual(blocked(entry, 8, 20), "")
        self.assertFalse(expected)
        self.assertNotIn("table", {c.number for c in commands(3)})
        self.assertNotIn("clear_filters", {c.number for c in commands(7)})
