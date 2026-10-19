"""Help search and navigation remain local, cached, and safe during stalls."""

from concurrent.futures import Future
import curses
import unittest
from unittest import mock

import test_tui_browser as fixtures
from agent_builds import tui_help
from agent_builds.store import Store
from agent_builds.tui_data import LazyStore
from agent_builds.tui_help import HelpView


class HelpViewTests(unittest.TestCase):
    def test_instruction_and_character_anchor_survive_resize_and_height_clamp(self):
        instruction = "".join("word-{:03d} ".format(i) for i in range(300))
        view = HelpView(["prefix " * 50, instruction, "last"])
        view.page(38, 3)
        view.scroll(30)
        first = view.page(38, 3)[0]
        anchor = view.anchor
        self.assertEqual(anchor[0], 1)
        self.assertGreater(anchor[1], 0)
        for width, room in ((18, 3), (98, 1000), (38, 3)):
            view.page(width, room)
            self.assertEqual(view.anchor, anchor)
        self.assertEqual(view.page(38, 3)[0], first)

    def test_navigation_and_search_replace_clamped_or_missing_anchors(self):
        view = HelpView(["row {:03d}".format(i) for i in range(100)])
        first = view.page(18, 2)
        view.scroll(60)
        view.page(18, 2)
        view.page(98, 1000)
        view.scroll(1)
        view.page(98, 1000)
        self.assertEqual(view.page(18, 2), first)
        view.end()
        self.assertEqual(view.page(18, 2)[-1], "row 099")
        view.home()
        self.assertEqual(view.page(18, 2), first)
        view.search("row 06")
        self.assertEqual(view.page(18, 2)[0], "row 060")
        view.search("missing")
        self.assertEqual(view.page(18, 2), [])
        self.assertIsNone(view.anchor)
        view.end()
        view.search("")
        self.assertEqual(view.page(18, 2), first)

    def test_word_wrap_anchor_handles_repeated_words_tabs_newlines_and_hyphens(self):
        instruction = "repeat repeat\talpha-beta\nnext  " * 100
        view = HelpView([instruction, "last"])
        view.page(38, 2)
        view.scroll(20)
        first = view.page(38, 2)[0]
        anchor = view.anchor
        view.page(18, 2)
        self.assertEqual(view.anchor, anchor)
        self.assertEqual(view.page(38, 2)[0], first)
        with mock.patch.object(
            tui_help.textwrap, "wrap", side_effect=AssertionError("rewrapped")
        ):
            for unused in range(300):
                view.page(38, 2)
            view.page(38, 1000)
            self.assertEqual(view.page(38, 2)[0], first)

    def test_idle_paging_and_height_changes_reuse_wrapping(self):
        view = HelpView(["A long instruction " * 20, "Other instruction"])
        with mock.patch.object(
            tui_help.textwrap, "wrap", wraps=tui_help.textwrap.wrap
        ) as wrap:
            first = view.page(30, 4)
            calls = wrap.call_count
            for unused in range(300):
                self.assertEqual(view.page(30, 4), first)
            view.scroll(1, page=True)
            view.page(30, 8)
            self.assertEqual(wrap.call_count, calls)
            view.page(20, 8)
            self.assertGreater(wrap.call_count, calls)

    def test_literal_case_insensitive_search_matches_before_wrapping(self):
        view = HelpView(["Full job ID opens logs", "[literal].* text", "Other"])
        view.search("JOB id")
        self.assertEqual(" ".join(view.page(9, 10)), "Full job ID opens logs")
        view.search("[literal].*")
        self.assertEqual(view.page(40, 3), ["[literal].* text"])
        view.search("missing")
        self.assertEqual(view.page(40, 3), [])
        self.assertEqual(view.position(), "0-0/0")
        view.search("")
        self.assertEqual(len(view.page(40, 5)), 3)

    def test_pages_home_end_resize_and_query_changes_clamp_position(self):
        view = HelpView(["row " + str(i) for i in range(30)])
        view.page(20, 4)
        view.end()
        self.assertEqual(view.page(20, 4), ["row " + str(i) for i in range(26, 30)])
        self.assertEqual(view.position(), "27-30/30")
        view.page(20, 20)
        self.assertEqual(view.offset, 10)
        view.home()
        self.assertEqual(view.offset, 0)
        view.scroll(-100)
        self.assertEqual(view.offset, 0)
        view.scroll(100)
        view.search("row 1")
        self.assertEqual(view.offset, 0)
        view.page(20, 2)
        view.end()
        offset = view.offset
        view.search("row 1")
        self.assertEqual(view.offset, offset)
        view.search("x" * 1000)
        self.assertEqual(len(view.query), 256)


class HelpNavigationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TuiBrowserTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def dashboard(self, keys, size, answers, **kwargs):
        store = self.fixture.store
        before = list(store.db.iterdump()), store.config_path.read_bytes()
        with mock.patch.object(
            LazyStore, "writable", side_effect=AssertionError("write")
        ), mock.patch.object(
            Store, "_ensure_schema", side_effect=AssertionError("migration")
        ), mock.patch.object(
            Store, "recover", side_effect=AssertionError("recover")
        ), mock.patch(
            "agent_builds.tui.subprocess.Popen", side_effect=AssertionError("command")
        ):
            frames = self.fixture.dashboard(keys, size, answers=answers, **kwargs)
        self.assertEqual(
            (list(store.db.iterdump()), store.config_path.read_bytes()), before
        )
        return frames

    def test_help_resize_and_reopen_retain_instruction_while_reads_are_stalled(self):
        size = [(24, 100)]
        actions = iter(
            [
                ord("?"),
                curses.KEY_END,
                curses.KEY_PPAGE,
                -1,
                -1,
                ord("q"),
                ord("?"),
                ord("q"),
                ord("q"),
            ]
        )
        step = [0]

        def key():
            index = step[0]
            step[0] += 1
            if index == 3:
                size[0] = (8, 20)
            elif index == 4:
                size[0] = (24, 100)
            return next(actions)

        pending = Future()
        instructions = ["prefix " * 100] + [
            "TARGET-{:02d}".format(i) for i in range(60)
        ]
        with mock.patch("agent_builds.tui.HELP", instructions):
            frames = self.dashboard(
                key,
                lambda: size[0],
                [],
                background_submit=lambda loader, request: pending,
            )

        def first_target(frame):
            return next(
                (
                    line
                    for y, line in sorted(frame.items())
                    if line.startswith("TARGET-")
                ),
                None,
            )

        target = first_target(frames[3])
        self.assertIsNotNone(target)
        self.assertEqual(first_target(frames[4]), target)
        self.assertEqual(first_target(frames[5]), target)
        self.assertEqual(first_target(frames[7]), target)
        self.assertFalse(pending.done())

    def test_search_cancel_clear_and_reopen_preserve_browser_and_help_state(self):
        for size in ((24, 100), (8, 20)):
            with self.subTest(size=size):
                frames = self.dashboard(
                    [
                        ord("/"),
                        ord("?"),
                        ord("/"),
                        curses.KEY_END,
                        ord("/"),
                        ord("q"),
                        ord("?"),
                        ord("/"),
                        ord("/"),
                        ord("q"),
                        ord("q"),
                    ],
                    size,
                    ["unmatched-browser", "blocker", None, "no-such-instruction", ""],
                )
                self.assertIn("Find: blocker", "\n".join(frames[3].values()))
                self.assertEqual(frames[4], frames[5])
                self.assertIn("No matching jobs", "\n".join(frames[6].values()))
                self.assertEqual(frames[5], frames[7])
                self.assertIn("No matching help", "\n".join(frames[8].values()))
                self.assertNotIn("No matching help", "\n".join(frames[9].values()))
                self.assertIn("No matching jobs", "\n".join(frames[-1].values()))

    def test_search_is_available_with_stalled_browser_and_actions_stay_inert(self):
        for size in ((24, 100), (8, 20)):
            future = Future()
            with self.subTest(size=size):
                frames = self.dashboard(
                    [
                        ord("?"),
                        ord("/"),
                        ord("c"),
                        ord("n"),
                        ord("p"),
                        ord("J"),
                        curses.KEY_END,
                        curses.KEY_HOME,
                        ord("q"),
                        ord("q"),
                    ],
                    size,
                    ["cancel"],
                    background_submit=lambda loader, key: future,
                )
                self.assertIn("Find: cancel", "\n".join(frames[2].values()))
                self.assertEqual(len(self.fixture.prompt_calls), 1)
                self.assertFalse(future.done())
                self.assertIn("BUILDR HELP", frames[7][0])


if __name__ == "__main__":
    unittest.main()
