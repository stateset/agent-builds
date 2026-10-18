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
