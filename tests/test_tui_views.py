"""View discovery is local, searchable, resize-aware and safe to cancel."""

import curses
import unittest
from unittest import mock

from agent_builds.tui_views import VIEWS, ViewPicker, choose, unavailable, footer


class ViewPickerModelTests(unittest.TestCase):
    def test_catalog_covers_every_existing_view_once(self):
        self.assertEqual([view.number for view in VIEWS], list(range(14)))
        self.assertEqual(len({view.name for view in VIEWS}), 14)

    def test_literal_token_search_matches_names_and_descriptions_without_actions(self):
        picker = ViewPicker(0)
        picker.search("QUEUE global")
        self.assertEqual([view.number for view in picker.matches], [11])
        picker.search("$() [unmatched]")
        self.assertEqual(picker.matches, [])
        self.assertIsNone(picker.choice())
        picker.move(100)
        self.assertEqual(picker.page(3), [])
        picker.search("")
        self.assertEqual(len(picker.matches), 14)
        picker.search("x" * 300)
        self.assertEqual(len(picker.query), 256)

    def test_current_selection_filtering_paging_and_resize_keep_selection_visible(self):
        picker = ViewPicker(13)
        self.assertEqual(picker.choice().number, 13)
        self.assertEqual(picker.page(3)[-1][1].number, 13)
        picker.move(-3)
        self.assertEqual(picker.choice().number, 10)
        self.assertIn(10, [view.number for index, view in picker.page(3)])
        picker.page(20)
        self.assertEqual(picker.top, 0)
        picker.search("external")
        self.assertEqual(picker.choice().number, 10)
        picker.search("")
        self.assertEqual(picker.choice().number, 10)
        picker.move(-100)
        self.assertEqual(picker.choice().number, 0)

    def test_footer_controls_fit_every_supported_width_and_compact_actions(self):
        for width in (20, 21, 39, 40, 64, 65, 80, 100):
            for view in VIEWS:
                for compact in (False, True):
                    with self.subTest(width=width, view=view.number, compact=compact):
                        text = footer(view.number, width, compact)
                        self.assertLessEqual(len(text), width - 1)
                        self.assertIn("V", text)
                        self.assertIn("q", text)
        self.assertNotIn("cancel", footer(7, 100, compact=True))
        self.assertNotIn("trial", footer(12, 100, compact=True))
        self.assertIn("cancel", footer(7, 65))
        self.assertIn("n/N", footer(13, 65))

    def test_size_and_job_requirements_are_explicit(self):
        for view in VIEWS:
            self.assertEqual(unavailable(view, 7, 19, True), "Resize to 20x8")
            self.assertEqual(unavailable(view, 20, 100, True), "")
            if view.full:
                self.assertEqual(unavailable(view, 8, 20, True), "Resize to 65x15")
            elif view.job:
                self.assertEqual(unavailable(view, 8, 20, False), "Select a job first")
            else:
                self.assertEqual(unavailable(view, 8, 20, False), "")
        logs = VIEWS[13]
        self.assertEqual(unavailable(logs, 8, 20, False, has_log=True), "")
        self.assertEqual(
            unavailable(VIEWS[3], 8, 20, False, has_log=True), "Select a job first"
        )


class ViewPickerTerminalTests(unittest.TestCase):
    def picker(
        self, keys, dimensions=(20, 100), current=0, has_job=True, has_log=False
    ):
        screen = mock.Mock()
        size = [dimensions]
        screen.getmaxyx.side_effect = lambda: size[0]
        frame, frames = {}, []
        screen.erase.side_effect = frame.clear
        screen.refresh.side_effect = lambda: frames.append(dict(frame))

        def paint(screen, y, x, text, attr=0):
            if 0 <= y < size[0][0]:
                frame[y] = str(text)[: max(0, size[0][1] - x - 1)]

        iterator = iter(keys)

        def read():
            key = next(iterator)
            if isinstance(key, tuple):
                size[0], key = key
            if isinstance(key, Exception):
                raise key
            return key

        screen.get_wch.side_effect = read
        with mock.patch("agent_builds.tui_views.time.sleep") as sleep:
            result = choose(screen, current, has_job, paint, has_log)
        self.sleeps = sleep.call_args_list
        self.assertEqual(screen.timeout.call_args_list[0], mock.call(200))
        self.assertEqual(screen.timeout.call_args_list[-1], mock.call(200))
        return result, frames

    def test_type_to_find_and_accept_in_full_and_smallest_panes(self):
        for dimensions in ((20, 100), (8, 20)):
            with self.subTest(dimensions=dimensions):
                result, frames = self.picker(list("history") + ["\n"], dimensions)
                self.assertEqual(result, 4)
                self.assertIn("History", " ".join(frames[-1].values()))
                self.assertEqual(frames[-1][dimensions[0] - 1], "Arrows Enter Esc")
                self.assertIn("1/1", frames[-1][2])

    def test_cancel_empty_match_and_action_characters_never_select_a_view(self):
        result, frames = self.picker(list("ncapbq") + ["\n", "\x1b"])
        self.assertIsNone(result)
        self.assertIn("No matching views", " ".join(frames[-1].values()))
        result, frames = self.picker(
            list("queue") + ["\x15"] + list("logs") + ["\x7f", "\x1b"]
        )
        self.assertIsNone(result)
        self.assertIn("Find: log", " ".join(frames[-1].values()))

    def test_disabled_choice_needs_explicit_resize_and_another_accept(self):
        result, frames = self.picker(
            list("processes") + ["\n", ((20, 100), "\n"), "\n"], (8, 20)
        )
        self.assertEqual(result, 1)
        self.assertTrue(
            any("Resize to 65x15" in " ".join(frame.values()) for frame in frames)
        )
        self.assertIn("Processes", " ".join(frames[-1].values()))

    def test_no_job_requirement_cannot_be_bypassed_and_current_log_can_stay_open(self):
        result, frames = self.picker(list("details") + ["\n", "\x1b"], has_job=False)
        self.assertIsNone(result)
        self.assertIn("Select a job first", " ".join(frames[-1].values()))
        result, unused = self.picker(["\n"], current=13, has_job=False, has_log=True)
        self.assertEqual(result, 13)

    def test_geometry_changes_discard_input_until_the_prompt_is_visible(self):
        keys = ["h", ((7, 18), "\n"), "x", "\n", ((8, 20), "\n"), "\n"]
        result, frames = self.picker(keys, current=4)
        self.assertEqual(result, 4)
        self.assertTrue(any("Resize:" in frame.get(0, "") for frame in frames))
        self.assertIn("Find: h", " ".join(frames[-1].values()))
        self.assertNotIn("hx", " ".join(frames[-1].values()))
        result, unused = self.picker(["\x1b"], dimensions=(2, 10))
        self.assertIsNone(result)

    def test_page_home_end_and_tab_navigation_are_bounded(self):
        result, frames = self.picker(
            [
                curses.KEY_NPAGE,
                curses.KEY_NPAGE,
                curses.KEY_PPAGE,
                curses.KEY_HOME,
                curses.KEY_END,
                curses.KEY_BTAB,
                "\t",
                "\n",
            ],
            (8, 20),
        )
        self.assertEqual(result, 13)
        self.assertIn("14/14", frames[-1][2])
        self.assertIn("Job logs", " ".join(frames[-1].values()))

    def test_idle_resize_and_permanent_terminal_errors_are_distinct(self):
        result, unused = self.picker([curses.error("no input"), "\x1b"])
        self.assertIsNone(result)
        self.assertEqual(self.sleeps, [mock.call(0.05)])
        with self.assertRaisesRegex(curses.error, "disconnected"):
            self.picker([curses.error("disconnected")])
