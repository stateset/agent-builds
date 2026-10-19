"""Exact-ID jumps are independent of browser rows and never dispatch work."""

from concurrent.futures import Future
import copy
import curses
import unittest
from unittest import mock

import test_tui_browser as fixtures
import test_tui_link_navigation as links
from agent_builds import tui
from agent_builds.store import Store
from agent_builds.tui_data import LazyStore


class JobJumpTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TuiBrowserTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.store, self.root, self.jobs = (
            self.fixture.store,
            self.fixture.root,
            self.fixture.jobs,
        )
        self.target = self.jobs[2]
        for index, job in enumerate(self.jobs):
            self.store.log_path(job).write_text("JUMP OUTPUT {}\n".format(index))

    def dashboard(self, keys, size=(24, 100), **kwargs):
        before = list(self.store.db.iterdump()), self.store.config_path.read_bytes()
        with mock.patch.object(
            LazyStore, "writable", side_effect=AssertionError("write")
        ), mock.patch.object(
            Store, "_ensure_schema", side_effect=AssertionError("migration")
        ), mock.patch.object(
            Store, "recover", side_effect=AssertionError("recovery")
        ), mock.patch.object(
            Store, "submit", side_effect=AssertionError("submission")
        ), mock.patch.object(
            tui.subprocess, "Popen", side_effect=AssertionError("command")
        ):
            frames = self.fixture.dashboard(keys, size, **kwargs)
        self.assertEqual(
            (list(self.store.db.iterdump()), self.store.config_path.read_bytes()),
            before,
        )
        return frames

    def test_exact_jump_opens_evicted_filtered_job_and_preserves_browser(self):
        for index in range(200):
            self.store.submit(
                self.root,
                ["true"],
                "local",
                "quick",
                1,
                0.25,
                "backlog-" + str(index),
                "",
                5,
            )
        self.assertNotIn(self.target, [r["id"] for r in self.store.browser_rows()])
        for size in ((24, 100), (8, 20)):
            calls = []

            def submit(loader, key):
                calls.append(key)
                return links.completed(loader, key)

            with self.subTest(size=size):
                frames = self.dashboard(
                    [ord("/"), ord("J"), -1, ord("l"), ord("q")],
                    size,
                    answers=["not-a-matching-job", "  " + self.target.upper() + "  "],
                    link_submit=submit,
                )
                self.assertIn("LOG LIVE", frames[2][0])
                self.assertIn(self.target, frames[2][1])
                self.assertTrue(
                    any("JUMP OUTPUT 2" in links.text(frame) for frame in frames[2:4])
                )
                self.assertIn("No matching jobs", links.text(frames[-1]))
                self.assertEqual([c[1:] for c in calls], [("browser", self.target)])

    def test_jump_is_available_from_inspection_submissions_and_narrow_full_only_views(
        self,
    ):
        for size in ((24, 100), (8, 20)):
            for view in (0, 3, 4, 7, 9, 10, 13, 15):
                with self.subTest(size=size, view=view), mock.patch.object(
                    tui, "choose_view", return_value=view
                ):
                    frames = self.dashboard(
                        [ord("V"), ord("J"), -1, ord("q")], size, answers=[self.target]
                    )
                self.assertIn("LOG LIVE", frames[-1][0])
                self.assertIn(self.target, frames[-1][1])
                self.assertEqual(len(self.fixture.prompt_calls), 1)

    def test_cancel_blank_and_invalid_ids_keep_held_log_without_lookup(self):
        for value in (
            None,
            "",
            "   ",
            self.target[:6],
            "buildr logs " + self.target,
            "../" + self.target,
            "z" * 12,
        ):
            calls = []

            def submit(loader, key):
                calls.append(key)
                return links.completed(loader, key)

            with self.subTest(value=value):
                frames = self.dashboard(
                    [ord("l"), ord(" "), ord("J"), -1, ord("q")],
                    answers=[value],
                    link_submit=submit,
                )
                self.assertIn("LOG HELD", frames[-1][0])
                self.assertEqual(frames[2][1], frames[-1][1])
                self.assertEqual(calls, [])
                if value and value.strip():
                    self.assertIn("full 12-character job ID", links.text(frames[-1]))

    def test_passive_browser_eviction_does_not_abandon_requested_target(self):
        now, ticks, browser_calls = [100], [0], []
        pending = links.PendingLinks()
        keys = iter([ord("J"), -1, -1, -1, ord("q")])

        def background(loader, key):
            future = links.completed(loader, key)
            if key == "queue":
                browser_calls.append(key)
                if len(browser_calls) > 1:
                    report = copy.deepcopy(future.result())
                    report["rows"] = []
                    future = Future()
                    future.set_result(report)
            return future

        def key():
            value = next(keys)
            if value == -1:
                ticks[0] += 1
                if ticks[0] == 1:
                    now[0] += 2
                elif ticks[0] == 2:
                    pending.finish()
            return value

        frames = self.dashboard(
            key,
            answers=[self.target],
            clock=lambda: now[0],
            background_submit=background,
            link_submit=pending,
        )
        self.assertGreater(len(browser_calls), 1)
        self.assertIn("LOG LIVE", frames[-1][0])
        self.assertIn(self.target, frames[-1][1])
        self.assertEqual(len(pending.calls), 1)

    def test_help_defers_jump_and_navigation_discards_late_result(self):
        for abandon in (False, True):
            pending, ticks, size = links.PendingLinks(), [0], [(24, 100)]
            keys = (
                iter([ord("J"), ord("?"), -1, ord("q"), curses.KEY_RESIZE, ord("q")])
                if not abandon
                else iter([ord("J"), ord("V"), -1, -1, ord("q")])
            )

            def key():
                value = next(keys)
                if value == -1:
                    ticks[0] += 1
                    if ticks[0] == 1:
                        pending.finish()
                elif value == curses.KEY_RESIZE:
                    size[0] = (8, 20)
                return value

            with self.subTest(abandon=abandon), mock.patch.object(
                tui, "choose_view", return_value=15
            ):
                frames = self.dashboard(
                    key, lambda: size[0], answers=[self.target], link_submit=pending
                )
            if abandon:
                self.assertFalse(any("LOG LIVE" in f.get(0, "") for f in frames))
                self.assertIn("SUBMISSIONS", frames[-1][0])
            else:
                self.assertIn("BUILDR HELP", frames[2][0])
                self.assertIn("BUILDR HELP", frames[3][0])
                self.assertIn("LOG LIVE", frames[-1][0])
            self.assertEqual(len(pending.calls), 1)

    def test_jump_opens_while_initial_browser_read_is_stalled(self):
        for size in ((24, 100), (8, 20)):
            browser = Future()

            def background(loader, key):
                return browser if key == "queue" else links.completed(loader, key)

            with self.subTest(size=size):
                frames = self.dashboard(
                    [ord("J"), -1, ord("l"), ord("q")],
                    size,
                    answers=[self.target],
                    background_submit=background,
                )
                self.assertIn("LOG LIVE", frames[1][0])
                self.assertIn(self.target, frames[1][1])
                self.assertIn("JUMP OUTPUT 2", links.text(frames[2]))
                self.assertNotIn("LOG LIVE", frames[-1][0])
                self.assertFalse(browser.done())

    def test_unknown_id_missing_state_and_help_never_create_or_retry(self):
        missing = self.root / "missing-state"
        calls = []

        def submit(loader, key):
            calls.append(key)
            return links.completed(loader, key)

        frames = self.dashboard(
            [ord("?"), ord("J"), ord("q"), ord("J"), -1, -1, ord("q")],
            answers=["a" * 12],
            state_path=missing,
            link_submit=submit,
        )
        self.assertEqual(len(self.fixture.prompt_calls), 1)
        self.assertEqual(len(calls), 1)
        self.assertIn("Log unavailable:", links.text(frames[-1]))
        self.assertFalse(missing.exists())

    def test_nested_jump_keeps_original_inspected_job_after_browser_eviction(self):
        for size in ((24, 100), (8, 20)):
            hidden, now = [False], [100]
            original = Store.browser_rows

            def rows(store, *args, **kwargs):
                return [
                    row
                    for row in original(store, *args, **kwargs)
                    if not hidden[0] or row["id"] != self.jobs[0]
                ]

            steps = iter([ord("i"), ord("l"), ord("J"), -1, ord("b"), -1, ord("q")])

            def key():
                value = next(steps)
                if value == -1:
                    hidden[0], now[0] = True, now[0] + 2
                return value

            with self.subTest(size=size), mock.patch.object(
                Store, "browser_rows", rows
            ):
                frames = self.dashboard(
                    key, size, answers=[self.target], clock=lambda: now[0]
                )
            self.assertTrue(
                any(
                    self.target in f.get(1, "") and "LOG LIVE" in f.get(0, "")
                    for f in frames
                )
            )
            self.assertIn("DETAILS", links.text(frames[-1]))
            self.assertIn(self.jobs[0], links.text(frames[-1]))
            self.assertNotIn(self.jobs[1], links.text(frames[-1]))

    def test_back_restores_history_checkout_and_submission_overview(self):
        self.store.db.execute(
            "UPDATE jobs SET cwd=? WHERE id=?", (str(self.root / "other"), self.target)
        )
        for size in ((24, 100), (8, 20)):
            for source, heading in ((4, "HISTORY"), (15, "SUBMISSIONS")):
                requests = []

                def submit(loader, key):
                    requests.append(key)
                    return links.completed(loader, key)

                with self.subTest(size=size, source=source), mock.patch.object(
                    tui, "choose_view", return_value=source
                ), mock.patch(
                    "agent_builds.tui_history.submit_read", side_effect=submit
                ):
                    frames = self.dashboard(
                        [ord("V"), ord("J"), -1, ord("b"), -1, ord("q")],
                        size,
                        answers=[self.target],
                    )
                self.assertIn(heading, links.text(frames[-1]))
                if source == 4:
                    self.assertEqual(requests, [str(self.root)] * 2)

    def test_cancelled_invalid_and_failed_jumps_keep_existing_back_destination(self):
        for value in (None, "", "invalid", "f" * 12):
            with self.subTest(value=value):
                frames = self.dashboard(
                    [ord("i"), ord("l"), ord("J"), -1, ord("b"), -1, ord("q")],
                    answers=[value],
                )
            self.assertIn("DETAILS", links.text(frames[-1]))
            self.assertIn(self.jobs[0], links.text(frames[-1]))

    def test_help_consumes_back_and_tiny_panes_defer_it(self):
        size = [(24, 100)]
        steps = iter(
            [
                ord("i"),
                ord("l"),
                ord("?"),
                ord("b"),
                ord("q"),
                curses.KEY_RESIZE,
                ord("b"),
                curses.KEY_RESIZE,
                ord("b"),
                -1,
                ord("q"),
            ]
        )

        def key():
            value = next(steps)
            if value == curses.KEY_RESIZE:
                size[0] = (7, 19) if size[0][0] >= 8 else (8, 20)
            return value

        frames = self.dashboard(key, lambda: size[0])
        self.assertIn("BUILDR HELP", frames[4][0])
        self.assertIn("LOG LIVE", frames[5][0])
        self.assertIn("LOG LIVE", frames[8][0])
        self.assertIn("DETAILS", links.text(frames[-1]))


if __name__ == "__main__":
    unittest.main()
