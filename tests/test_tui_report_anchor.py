"""Report reading position survives wrapping changes without I/O or idle scans."""

from concurrent.futures import Future
import curses
import unittest
from unittest import mock

from agent_builds.tui_text import ReportView
import test_tui_browser as browser_fixture


class TextView(ReportView):
    def lines(self, scope, report):
        return report["lines"]


class ReportAnchorTests(unittest.TestCase):
    def test_logical_row_survives_large_wrapping_change_and_height_clamp(self):
        view = TextView()
        report = dict(
            lines=["prefix " * 100] + ["TARGET-{:02d}".format(i) for i in range(50)]
        )
        view.page("same", report, 99, 8)
        view.end()
        view.page("same", report, 99, 8)
        view.scroll(-10)
        before = view.page("same", report, 99, 8)[0]
        self.assertTrue(before.startswith("TARGET-"))
        self.assertEqual(view.page("same", report, 19, 1)[0], before)
        view.page("same", report, 19, 1000)
        self.assertEqual(view.offset, 0)
        self.assertEqual(view.page("same", report, 99, 8)[0], before)

    def test_long_unicode_paragraph_keeps_codepoint_anchor_across_round_trips(self):
        view = TextView()
        text = "".join("界{:03d}e\u0301 ".format(i) for i in range(200))
        report = dict(lines=["heading\n" + text, "tail"])
        view.page("same", report, 39, 2)
        view.scroll(20)
        before = view.page("same", report, 39, 2)[0]
        anchor = view.anchor
        view.page("same", report, 19, 1)
        self.assertEqual(view.anchor, anchor)
        self.assertEqual(view.page("same", report, 39, 2)[0], before)
        self.assertEqual(view.anchor, anchor)

    def test_navigation_while_clamped_replaces_old_anchor_and_end_uses_new_layout(self):
        view = TextView()
        report = dict(lines=["row-{:03d}".format(i) for i in range(100)])
        view.page("same", report, 19, 2)
        view.scroll(80)
        self.assertEqual(view.page("same", report, 19, 2)[0], "row-080")
        view.page("same", report, 19, 1000)
        view.scroll(1)
        view.page("same", report, 19, 1000)
        self.assertEqual(view.page("same", report, 19, 2)[0], "row-000")
        view.end()
        self.assertEqual(view.page("same", report, 39, 4)[-1], "row-099")
        view.home()
        self.assertEqual(view.page("same", report, 19, 2)[0], "row-000")

    def test_hidden_refresh_scope_changes_and_shorter_reports_use_current_content(self):
        view = TextView()
        report = dict(lines=["row-{:03d}".format(i) for i in range(40)])
        view.page("a", report, 39, 3)
        view.scroll(20)
        before = view.page("a", report, 39, 3)[0]
        self.assertEqual(view.page("a", None, 19, 1), [])
        self.assertEqual(view.page("a", dict(report), 19, 1)[0], before)
        self.assertEqual(view.page("a", dict(lines=["new"]), 19, 1), ["new"])
        self.assertEqual(view.page("b", report, 19, 1)[0], "row-000")
        failed = view.page("c", None, 19, 1, "read failed")
        self.assertEqual(failed, ["read failed"])
        self.assertNotIn("row-", "".join(failed))

    def test_idle_and_height_only_frames_do_not_rebuild_or_scan_report_text(self):
        view = TextView()
        report = dict(lines=["prefix " * 500, "last"])
        view.page("a", report, 39, 2)
        view.scroll(10)
        view.page("a", report, 39, 2)
        with mock.patch.object(
            view, "lines", side_effect=AssertionError("report rebuilt")
        ), mock.patch(
            "agent_builds.tui_text.wrap_line",
            side_effect=AssertionError("report rewrapped"),
        ), mock.patch(
            "agent_builds.tui_text.clean_lines",
            side_effect=AssertionError("report rescanned"),
        ):
            for unused in range(300):
                view.page("a", report, 39, 2)
            view.page("a", report, 39, 3)
            view.page("a", report, 39, 2)


class ReportAnchorDashboardTests(unittest.TestCase):
    setUp = browser_fixture.TuiBrowserTests.setUp
    tearDown = browser_fixture.TuiBrowserTests.tearDown
    dashboard = browser_fixture.TuiBrowserTests.dashboard

    def test_health_keeps_same_visible_row_full_to_minimum_and_back(self):
        size = [(24, 100)]
        step = [0]
        actions = [ord("u"), -1, curses.KEY_END, curses.KEY_PPAGE, -1, -1, ord("q")]

        def key():
            index = step[0]
            step[0] += 1
            if index == 4:
                size[0] = (8, 20)
            elif index == 5:
                size[0] = (24, 100)
            return actions[index]

        lines = ["prefix " * 100] + ["TARGET-{:02d}".format(i) for i in range(50)]

        def completed(loader, request):
            future = Future()
            try:
                future.set_result(loader(request))
            except Exception as error:
                future.set_exception(error)
            return future

        with mock.patch(
            "agent_builds.tui_inspect.HealthView.lines", return_value=lines
        ):
            with mock.patch(
                "agent_builds.tui_reads.submit_read", side_effect=completed
            ):
                frames = self.dashboard(key, dimensions=lambda: size[0])

        def first_target(frame):
            return next(
                (
                    line
                    for y, line in sorted(frame.items())
                    if line.startswith("TARGET-")
                ),
                None,
            )

        target = first_target(frames[4])
        self.assertIsNotNone(target)
        self.assertEqual(first_target(frames[5]), target)
        self.assertEqual(first_target(frames[6]), target)
