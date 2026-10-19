"""Long selected evidence stays readable without repeated formatting or reads."""

import copy
import curses
from concurrent.futures import Future
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_builds import tui_details, tui_text
import test_tui_browser as browser_fixture
from agent_builds.store import Store
from agent_builds.tui_details import DetailsView, detail_lines


class DetailsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "state")
        handle = self.store.submit(
            self.temp.name,
            ["echo", "argument-" * 60],
            "local",
            "quick",
            1,
            0.25,
            "details",
            "",
            5,
        )
        self.row = self.store.get(handle)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_long_unicode_command_keeps_logical_position_across_resize_and_hidden_read(
        self,
    ):
        self.row["command_display"] = "".join(
            "界{:03d}e\u0301 ".format(i) for i in range(300)
        )
        view = DetailsView()
        view.page(self.row, 39, 2)
        view.scroll(10)
        before = view.page(self.row, 39, 2)[0]
        anchor = view.anchor
        self.assertGreater(anchor[2], 0)
        view.page(self.row, 19, 1)
        self.assertEqual(view.anchor, anchor)
        view.page(self.row, 99, 1000)
        self.assertEqual(view.offset, 0)
        self.assertEqual(view.page(self.row, 39, 2)[0], before)
        view.clear()
        self.assertIsNone(view.row)
        self.assertEqual(view.paragraphs, [])
        self.assertEqual(view.visible, [])
        self.assertEqual(view.locations, [])
        self.assertEqual(view.anchor, anchor)
        self.assertEqual(view.page(copy.deepcopy(self.row), 39, 2)[0], before)
        self.assertEqual(view.job, self.row["id"])

    def test_navigation_after_height_clamp_and_scope_change_replace_the_anchor(self):
        view = DetailsView()
        self.row["command_display"] = "token " * 500
        first = view.page(self.row, 19, 2)
        view.scroll(20)
        view.page(self.row, 19, 2)
        view.page(self.row, 99, 1000)
        view.scroll(1)
        view.page(self.row, 99, 1000)
        self.assertEqual(view.page(self.row, 19, 2)[0], first[0])
        view.end()
        self.assertEqual(view.page(self.row, 39, 2)[-1], view.wrapped[-1])
        view.page(dict(self.row, id="new-job"), 39, 2)
        self.assertEqual(view.offset, 0)
        view.home()
        self.assertEqual(view.page(self.row, 19, 2)[0], first[0])

    def test_long_command_paths_and_reason_are_preserved_across_wrapped_rows(self):
        self.row.update(cwd="/checkout/" + "folder/" * 30, reason="wait " * 60)
        original, view = copy.deepcopy(self.row), DetailsView()
        view.page(self.row, 19, 2)
        self.assertTrue(all(len(line) <= 19 for line in view.wrapped))
        self.assertEqual("".join(view.wrapped), "".join(detail_lines(self.row)))
        self.assertEqual(self.row, original)

    def test_navigation_clamps_and_resize_rewraps_without_losing_evidence(self):
        view = DetailsView()
        first = view.page(self.row, 19, 2)
        view.scroll(1)
        self.assertNotEqual(view.page(self.row, 19, 2), first)
        view.end()
        last = view.page(self.row, 19, 2)
        self.assertTrue(view.position().endswith("/" + str(len(view.wrapped))))
        self.assertEqual(view.offset, len(view.wrapped) - 2)
        view.scroll(500)
        self.assertEqual(view.page(self.row, 19, 2), last)
        view.page(self.row, 99, 200)
        self.assertEqual(view.offset, 0)
        self.assertLessEqual(len(view.wrapped), 200)
        view.scroll(-500)
        view.page(self.row, 99, 2)
        self.assertEqual(view.offset, 0)
        view.end()
        view.page(self.row, 99, 2)
        view.home()
        self.assertEqual(view.page(self.row, 19, 2), first)

    def test_idle_frames_and_height_changes_reuse_formatted_and_wrapped_evidence(self):
        view = DetailsView()
        with mock.patch.object(
            tui_details, "detail_lines", wraps=detail_lines
        ) as formatted:
            with mock.patch.object(
                tui_text, "wrap_line", wraps=tui_text.wrap_line
            ) as wrapped:
                view.page(self.row, 40, 3)
                calls = wrapped.call_count
                for _ in range(100):
                    view.page(self.row, 40, 5)
                self.assertEqual(formatted.call_count, 1)
                self.assertEqual(wrapped.call_count, calls)
                view.page(self.row, 60, 3)
                self.assertEqual(formatted.call_count, 1)
                self.assertGreater(wrapped.call_count, calls)

    def test_refreshed_snapshot_updates_outcome_and_selected_job_resets_scroll(self):
        view = DetailsView()
        view.page(self.row, 40, 3)
        view.end()
        view.page(self.row, 40, 3)
        refreshed = copy.deepcopy(self.row)
        refreshed.update(state="failed", exit_code=7)
        view.page(refreshed, 40, 3)
        self.assertIn("Job exit: 7 | failed", "\n".join(view.wrapped))
        self.assertGreater(view.offset, 0)
        different = dict(refreshed, id="different", command_display="other job")
        view.page(different, 40, 3)
        self.assertEqual(view.offset, 0)
        self.assertNotIn("argument-", "".join(view.wrapped))

    def test_hidden_or_unavailable_details_release_previous_evidence(self):
        view = DetailsView()
        view.page(self.row, 40, 3)
        view.scroll(1)
        view.page(self.row, 40, 3)
        offset = view.offset
        self.assertEqual(view.page(None, 40, 3), [])
        self.assertIsNone(view.row)
        self.assertEqual(view.logical, [])
        self.assertEqual(view.wrapped, [])
        self.assertEqual(view.position(), "0/0")
        view.page(copy.deepcopy(self.row), 40, 3)
        self.assertEqual(view.offset, offset)

    def test_terminal_controls_and_wide_unicode_are_cleaned_before_wrapping(self):
        view = DetailsView()
        self.row["command_display"] = "\x1b[31m界界e\u0301\x1b[0m\nsecond\tvalue"
        view.page(self.row, 4, 3)
        joined = "".join(view.wrapped)
        self.assertIn("界界e\u0301", joined)
        self.assertIn("second", joined)
        self.assertNotIn("\x1b", joined)
        self.assertNotIn("\t", joined)

    def test_unknown_native_exit_is_explicit_and_not_inferred_from_state(self):
        self.row.update(state="lost", exit_code=None)
        lines = detail_lines(self.row)
        self.assertIn("Job exit: not recorded | lost", lines)
        self.assertIn("Checkout: " + self.temp.name, lines)

    def test_oversized_rendering_is_capped_and_labels_the_missing_evidence(self):
        self.row["command_display"] = "x" * (tui_details.MAX_DETAIL_CHARS * 2)
        view = DetailsView()
        view.page(self.row, 19, 2)
        self.assertLess(sum(map(len, view.logical)), tui_details.MAX_DETAIL_CHARS + 100)
        self.assertIn("Details clipped", "".join(view.wrapped))
        self.assertLess(len(view.wrapped), 4000)


class DetailsAnchorDashboardTests(unittest.TestCase):
    setUp = browser_fixture.TuiBrowserTests.setUp
    tearDown = browser_fixture.TuiBrowserTests.tearDown
    dashboard = browser_fixture.TuiBrowserTests.dashboard

    def test_details_resize_and_help_round_trip_keep_the_inspected_text(self):
        size = [(24, 100)]
        actions = iter(
            [
                ord("i"),
                -1,
                curses.KEY_END,
                curses.KEY_PPAGE,
                -1,
                -1,
                ord("?"),
                ord("q"),
                -1,
                ord("q"),
            ]
        )
        step = [0]

        def key():
            index = step[0]
            step[0] += 1
            if index == 4:
                size[0] = (8, 20)
            elif index == 5:
                size[0] = (24, 100)
            return next(actions)

        def completed(loader, request):
            future = Future()
            try:
                future.set_result(loader(request))
            except Exception as error:
                future.set_exception(error)
            return future

        with mock.patch(
            "agent_builds.tui_details.detail_lines",
            return_value=["prefix " * 100]
            + ["TARGET-{:02d}".format(i) for i in range(50)],
        ), mock.patch("agent_builds.tui_reads.submit_read", side_effect=completed):
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
        self.assertEqual(first_target(frames[9]), target)


if __name__ == "__main__":
    unittest.main()
