"""Literal report filtering stays cached, bounded and independent of queue actions."""

from concurrent.futures import Future
from pathlib import Path
import unittest
from unittest import mock

from agent_builds.tui_text import ReportView
import test_tui_browser as browser_fixture


class TextView(ReportView):
    def lines(self, scope, report):
        return report["lines"]


class ReportSearchTests(unittest.TestCase):
    def test_literal_unicode_search_uses_sanitized_logical_lines(self):
        view = TextView()
        report = dict(
            lines=["\x1b[31mStraße\x1b[0m", "[.*] literal\nother paragraph", "no match"]
        )
        view.page("scope", report, 19, 3)
        view.search("STRASSE")
        self.assertEqual(view.page("scope", report, 19, 3), ["Straße"])
        self.assertEqual(view.matches, 1)
        view.search("[.*]")
        self.assertEqual(view.page("scope", report, 19, 3), ["[.*] literal"])
        view.search("31m")
        self.assertEqual(view.page("scope", report, 19, 3), [])
        self.assertIn("Find 0:", view.search_status())

    def test_pending_and_failed_reads_do_not_claim_zero_matches_or_hide_errors(self):
        view = TextView()
        view.page("scope", None, 19, 2)
        view.search("needle")
        self.assertEqual(view.page("scope", None, 19, 2), [])
        self.assertIn("Find pending:", view.search_status())
        self.assertIsNone(view.matches)
        self.assertEqual(
            "".join(view.page("scope", None, 19, 2, "database unavailable")),
            "database unavailable",
        )
        self.assertIn("Find unavailable:", view.search_status())
        view.page("scope", dict(lines=["current text"]), 19, 2)
        self.assertEqual(view.matches, 0)
        view.search("")
        self.assertEqual(view.page("scope", view.report, 19, 2), ["current text"])
        self.assertEqual(view.search_status(), "")

    def test_query_changes_reset_position_scope_changes_clear_query_and_refresh_replaces_text(
        self,
    ):
        view = TextView()
        report = dict(lines=["needle " * 100, "another needle", "excluded"])
        view.page("a", report, 19, 2)
        view.search("needle")
        view.page("a", report, 19, 2)
        view.scroll(4)
        view.page("a", report, 19, 2)
        self.assertGreater(view.offset, 0)
        view.search("another")
        self.assertEqual(view.page("a", report, 19, 2), ["another needle"])
        self.assertEqual(view.offset, 0)
        self.assertEqual(
            view.page("a", dict(lines=["another fresh"]), 19, 2), ["another fresh"]
        )
        self.assertTrue(view.page("b", report, 19, 2)[0].startswith("needle"))
        self.assertEqual(view.query, "")
        view.search("x" * 1000)
        self.assertEqual(len(view.query), 256)

    def test_matching_line_count_and_anchor_survive_wrapping_changes(self):
        view = TextView()
        report = dict(lines=["hit " + ("界e\u0301 " * 200), "hit tail", "excluded"])
        view.page("a", report, 39, 2)
        view.search("hit")
        view.page("a", report, 39, 2)
        view.scroll(10)
        before = view.page("a", report, 39, 2)[0]
        self.assertEqual(view.matches, 2)
        view.page("a", report, 19, 1)
        self.assertEqual(view.matches, 2)
        self.assertEqual(view.page("a", report, 39, 2)[0], before)

    def test_idle_height_and_width_changes_reuse_selected_cached_paragraphs(self):
        view = TextView()
        report = dict(lines=["needle " * 100, "excluded"])
        view.page("a", report, 39, 2)
        view.search("needle")
        view.page("a", report, 39, 2)
        with mock.patch.object(
            view, "_select_paragraphs", side_effect=AssertionError("refiltered")
        ), mock.patch.object(
            view, "lines", side_effect=AssertionError("report rebuilt")
        ), mock.patch(
            "agent_builds.tui_text.clean_lines",
            side_effect=AssertionError("resanitized"),
        ):
            for unused in range(300):
                view.page("a", report, 39, 2)
            view.page("a", report, 39, 3)
            view.page("a", report, 19, 1)
        self.assertEqual(view.matches, 1)

    def test_stale_status_stays_first_when_query_would_fill_a_narrow_pane(self):
        view = TextView()
        report = dict(lines=["needle"])
        view.page("a", report, 19, 1)
        view.search("needle")
        view.page("a", report, 19, 1)
        self.assertTrue(
            view.status_label("STALE Snapshot 9s ago; refreshing").startswith(
                "STALE | Find 1:"
            )
        )
        view.page("a", report, 19, 1, "database busy")
        self.assertTrue(
            view.status_label("Stale history: database busy").startswith(
                "STALE | Find 1:"
            )
        )
        view.search("")
        self.assertEqual(
            view.status_label("Stale history: database busy"),
            "Stale history: database busy",
        )

    def test_search_never_loads_clipped_report_text(self):
        view = TextView()
        report = dict(lines=["x" * (view.limit + 10), "OMITTED NEEDLE"])
        view.page("a", report, 39, 2)
        view.search("OMITTED NEEDLE")
        with mock.patch.object(
            Path, "open", side_effect=AssertionError("search read evidence")
        ):
            self.assertEqual(view.page("a", report, 39, 2), [])
        self.assertEqual(view.matches, 0)


def completed(loader, request):
    future = Future()
    try:
        future.set_result(loader(request))
    except Exception as error:
        future.set_exception(error)
    return future


class ReportSearchDashboardTests(unittest.TestCase):
    setUp = browser_fixture.TuiBrowserTests.setUp
    tearDown = browser_fixture.TuiBrowserTests.tearDown
    dashboard = browser_fixture.TuiBrowserTests.dashboard

    def test_search_cancel_clear_stay_local_in_all_supported_views_and_minimum_panes(
        self,
    ):
        for target in (4, 11, 12, 14):
            for size in ((24, 100), (8, 20)):
                with self.subTest(target=target, size=size), mock.patch(
                    "agent_builds.tui.choose_view", return_value=target
                ), mock.patch(
                    "agent_builds.tui_reads.submit_read", side_effect=completed
                ), mock.patch(
                    "agent_builds.tui_history.submit_read", side_effect=completed
                ), mock.patch(
                    "agent_builds.tui_inspect.HealthView.lines",
                    return_value=["haystack", "needle"],
                ), mock.patch(
                    "agent_builds.tui_inspect.WhyView.lines",
                    return_value=["haystack", "needle"],
                ), mock.patch(
                    "agent_builds.tui_inspect.WorkbenchView.lines",
                    return_value=["haystack", "needle"],
                ), mock.patch(
                    "agent_builds.tui_history.HistoryView.lines",
                    return_value=["haystack", "needle"],
                ):
                    frames = self.dashboard(
                        [
                            ord("V"),
                            -1,
                            -1,
                            ord("/"),
                            -1,
                            ord("/"),
                            -1,
                            ord("/"),
                            -1,
                            ord("q"),
                        ],
                        dimensions=size,
                        answers=["needle", None, ""],
                    )
                self.assertTrue(
                    any("Find 1: needle" in "\n".join(f.values()) for f in frames)
                )
                self.assertEqual(
                    [call.args[2] for call in self.prompt_calls],
                    ["", "needle", "needle"],
                )
                self.assertNotIn("Find 1:", "\n".join(frames[-1].values()))

    def test_initial_history_read_error_remains_visible_with_a_query_in_minimum_pane(
        self,
    ):
        with mock.patch("agent_builds.tui.choose_view", return_value=4), mock.patch(
            "agent_builds.tui_history.submit_read", side_effect=completed
        ), mock.patch(
            "agent_builds.tui_history.inspect_history",
            side_effect=OSError("disk blocked"),
        ):
            frames = self.dashboard(
                [ord("V"), -1, -1, ord("/"), -1, ord("q")],
                dimensions=(8, 20),
                answers=["needle"],
            )
        self.assertTrue(
            any("Find unavailable:" in "\n".join(f.values()) for f in frames)
        )
        self.assertTrue(any("disk blocked" in "\n".join(f.values()) for f in frames))

    def test_search_while_inspection_is_pending_shows_unknown_match_count(self):
        pending = Future()
        with mock.patch("agent_builds.tui.choose_view", return_value=11), mock.patch(
            "agent_builds.tui_reads.submit_read", return_value=pending
        ):
            frames = self.dashboard(
                [ord("V"), -1, ord("/"), -1, ord("q")],
                dimensions=(8, 20),
                answers=["needle"],
            )
        self.assertTrue(any("Find pending:" in "\n".join(f.values()) for f in frames))
        self.assertFalse(any("Find 0:" in "\n".join(f.values()) for f in frames))
        self.assertFalse(pending.done())
