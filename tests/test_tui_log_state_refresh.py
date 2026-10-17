"""Pinned log state refreshes independently of browser rows and held output."""

from concurrent.futures import Future
import curses
import sqlite3
import unittest
from unittest import mock

import test_tui_browser as fixtures

from agent_builds.store import Store
from agent_builds.tui_logs import LogView, TailCache


def completed(loader, key):
    future = Future()
    try:
        future.set_result(loader(key))
    except Exception as error:
        future.set_exception(error)
    return future


def frame_text(frame):
    return "\n".join(frame.values())


class PendingState:
    def __init__(self):
        self.calls = []

    def __call__(self, loader, key):
        future = Future()
        self.calls.append((loader, key, future))
        return future

    def finish(self, index=-1, error=None):
        loader, key, future = self.calls[index]
        if error is None:
            future.set_result(loader(key))
        else:
            future.set_exception(error)


class TuiLogStateRefreshTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TuiBrowserTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root, self.store, self.jobs = (
            self.fixture.root,
            self.fixture.store,
            self.fixture.jobs,
        )
        for index, job in enumerate(self.jobs):
            self.store.log_path(job).write_text("private output {}\n".format(index))

    def set_state(self, job, state):
        self.store.db.execute(
            "UPDATE jobs SET state=?,exit_code=?,finished=? WHERE id=?",
            (state, 7 if state == "failed" else None, None, job),
        )

    def dashboard(self, keys, dimensions=(24, 100), **kwargs):
        config = self.store.config_path.read_bytes()
        controls = list(
            map(
                tuple,
                self.store.db.execute(
                    "SELECT id,cancel_requested FROM jobs ORDER BY id"
                ),
            )
        )
        with mock.patch.object(
            Store, "_ensure_schema", side_effect=AssertionError("queue initialized")
        ), mock.patch.object(
            TailCache, "read", side_effect=AssertionError("synchronous inline log read")
        ), mock.patch.object(
            LogView, "poll", side_effect=AssertionError("synchronous focused log read")
        ):
            frames = self.fixture.dashboard(keys, dimensions, **kwargs)
        self.assertEqual(self.store.config_path.read_bytes(), config)
        self.assertEqual(
            list(
                map(
                    tuple,
                    self.store.db.execute(
                        "SELECT id,cancel_requested FROM jobs ORDER BY id"
                    ),
                )
            ),
            controls,
        )
        return frames

    def test_evicted_job_finishes_while_held_or_searched_output_stays_frozen(self):
        for index in range(200):
            self.store.submit(
                self.root,
                ["true"],
                "local",
                "quick",
                1,
                0.25,
                "private-backlog-{}".format(index),
                "",
                5,
            )
        job = self.jobs[0]
        for dimensions in ((24, 100), (12, 40)):
            for hold in (ord(" "), ord("/")):
                self.set_state(job, "running")
                self.store.log_path(job).write_text("FROZEN needle output\n")
                now = [100]
                keys = iter((ord("l"), hold, -1, ord("q")))
                reads = []

                def submit(loader, key):
                    reads.append(key)
                    return completed(loader, key)

                def key():
                    value = next(keys)
                    if value == -1:
                        self.set_state(job, "failed")
                        self.store.log_path(job).write_text("AFTER_FINISH output\n")
                        now[0] = 102
                    return value

                with self.subTest(dimensions=dimensions, hold=hold):
                    frames = self.dashboard(
                        key,
                        dimensions,
                        query="needle",
                        clock=lambda: now[0],
                        log_status_submit=submit,
                    )
                    self.assertEqual(frames[1][1], job + " running")
                    self.assertEqual(frames[-1][1], job + " failed")
                    self.assertIn("LOG HELD", frames[-1][0])
                    self.assertIn("FROZEN needle output", frame_text(frames[-1]))
                    self.assertNotIn("AFTER_FINISH", frame_text(frames[-1]))
                    self.assertNotIn("State loading", frames[-1][2])
                    self.assertNotIn("STALE state", frames[-1][2])
                    self.assertEqual(reads, [job, job])
                    rows = self.store.browser_rows()
                    self.assertEqual(len(rows), 200)
                    self.assertNotIn(job, [row["id"] for row in rows])

    def test_pending_state_keeps_output_navigation_help_resize_and_quit_available(self):
        for initial_size in ((24, 100), (12, 40)):
            pending = PendingState()
            size = [initial_size]
            keys = iter(
                (
                    ord("l"),
                    ord("j"),
                    ord("?"),
                    ord("p"),
                    ord("q"),
                    curses.KEY_RESIZE,
                    ord("q"),
                )
            )

            def key():
                value = next(keys)
                if value == curses.KEY_RESIZE:
                    size[0] = (12, 40) if size[0][1] >= 65 else (24, 100)
                return value

            with self.subTest(initial_size=initial_size):
                frames = self.dashboard(
                    key,
                    dimensions=lambda: size[0],
                    log_status_submit=pending,
                )
                self.assertEqual(len(frames), 7)
                for index in (1, 2, 5, 6):
                    self.assertEqual(frames[index][1], self.jobs[0] + " running")
                    self.assertTrue(frames[index][2].startswith("State loading"))
                    self.assertIn("private output 0", frame_text(frames[index]))
                self.assertIn("LOG HELD", frames[-1][0])
                self.assertIn("BUILDR HELP", frames[3][0])
                self.assertIn("BUILDR HELP", frames[4][0])
                self.assertEqual(len(pending.calls), 1)
                self.assertFalse(pending.calls[0][2].done())
                self.assertEqual(self.fixture.prompt_calls, [])

    def test_slow_or_failed_state_refresh_keeps_fresh_output_independent(self):
        job = self.jobs[0]
        for dimensions in ((24, 100), (12, 40)):
            for failed in (False, True):
                self.set_state(job, "running")
                self.store.log_path(job).write_text("INITIAL_OUTPUT\n")
                now, calls = [100], []
                keys = iter((ord("l"), -1, -1, ord("q")))
                steps = [0]

                def submit(loader, key):
                    future = completed(loader, key) if not calls else Future()
                    if calls and failed:
                        future.set_exception(
                            sqlite3.OperationalError("fixture metadata busy")
                        )
                    calls.append((key, future))
                    return future

                def key():
                    value = next(keys)
                    if value == -1:
                        if not steps[0]:
                            self.set_state(job, "failed")
                            self.store.log_path(job).write_text(
                                "FRESH_OUTPUT_WITH_STALE_STATE\n"
                            )
                            now[0] = 105
                        else:
                            now[0] = 105.5
                        steps[0] += 1
                    return value

                with self.subTest(dimensions=dimensions, failed=failed):
                    frames = self.dashboard(
                        key,
                        dimensions,
                        clock=lambda: now[0],
                        log_status_submit=submit,
                    )
                    self.assertEqual(frames[1][1], job + " running")
                    for frame in frames[2:]:
                        self.assertEqual(frame[1], job + " running")
                        self.assertTrue(frame[2].startswith("STALE state"))
                        self.assertIn(
                            "FRESH_OUTPUT_WITH_STALE_STATE", frame_text(frame)
                        )
                    if failed:
                        self.assertIn("fixture metadata busy", frames[-1][2])
                    else:
                        self.assertIn("refreshing", frames[-1][2])
                        self.assertFalse(calls[-1][1].done())
                    self.assertEqual([key for key, future in calls], [job, job])
                    self.assertEqual(self.store.get_control(job)["state"], "failed")

    def test_late_state_results_and_errors_do_not_cross_job_or_aba_selection(self):
        for dimensions in ((24, 100), (12, 40)):
            for return_to_first in (False, True):
                for failed in (False, True):
                    self.set_state(self.jobs[0], "running")
                    self.set_state(self.jobs[1], "queued")
                    pending = PendingState()
                    target = self.jobs[0] if return_to_first else self.jobs[1]
                    state = "failed" if return_to_first else "succeeded"
                    keys = iter(
                        [ord("l"), ord("l"), ord("j"), ord("l")]
                        + ([ord("l"), ord("k"), ord("l")] if return_to_first else [])
                        + [-1, -1, -1, ord("q")]
                    )
                    steps = [0]

                    def key():
                        value = next(keys)
                        if value == -1:
                            if steps[0] == 0:
                                pending.finish(
                                    0,
                                    sqlite3.OperationalError("obsolete state error")
                                    if failed
                                    else None,
                                )
                                self.set_state(target, state)
                            elif steps[0] == 1:
                                self.assertEqual(len(pending.calls), 2)
                                pending.finish(1)
                            steps[0] += 1
                        return value

                    with self.subTest(
                        dimensions=dimensions, aba=return_to_first, failed=failed
                    ):
                        frames = self.dashboard(
                            key, dimensions, log_status_submit=pending
                        )
                        self.assertEqual(
                            [key for loader, key, future in pending.calls],
                            [self.jobs[0], target],
                        )
                        self.assertEqual(frames[-1][1], target + " " + state)
                        self.assertEqual(frames[-2][1], target + " " + state)
                        self.assertNotIn("State loading", frames[-1][2])
                        self.assertNotIn("STALE state", frames[-1][2])
                        for frame in frames:
                            self.assertNotIn("obsolete state error", frame_text(frame))

    def test_state_polls_only_visible_focused_logs_and_keeps_polling_held_output(self):
        now, size, reads = [100], [(24, 100)], []
        steps = iter(
            (
                (-1, 0),
                (ord("l"), 0),
                (ord(" "), 1),
                (ord("?"), 2),
                (-1, 2),
                (ord("q"), 2),
                (curses.KEY_RESIZE, 3),
                (-1, 3),
                (curses.KEY_RESIZE, 3),
                (ord("l"), 4),
                (ord("q"), 4),
            )
        )

        def submit(loader, key):
            reads.append((key, now[0]))
            return completed(loader, key)

        def key():
            value, expected = next(steps)
            self.assertEqual(len(reads), expected)
            now[0] += 1
            if value == curses.KEY_RESIZE:
                size[0] = (7, 19) if size[0] == (24, 100) else (8, 20)
            return value

        frames = self.dashboard(
            key,
            dimensions=lambda: size[0],
            clock=lambda: now[0],
            log_status_submit=submit,
        )
        self.assertEqual(reads, [(self.jobs[0], tick) for tick in (102, 103, 106, 109)])
        self.assertIn("LOG HELD", frames[9][0])
        self.assertEqual(frames[9][1], (self.jobs[0] + " running")[:19])
        self.assertNotIn("State loading", frames[9][2])
        self.assertNotIn("STALE state", frames[9][2])
        self.assertEqual(self.fixture.prompt_calls, [])

    def test_foreign_or_invalid_state_reply_never_relabels_the_retained_job(self):
        job = self.jobs[0]
        for dimensions in ((24, 100), (8, 20)):
            for reply in (
                dict(id=self.jobs[1], state="failed", cancel_requested=False),
                dict(id=job, state=None, cancel_requested=False),
            ):

                def submit(loader, key):
                    future = Future()
                    future.set_result(reply)
                    return future

                with self.subTest(dimensions=dimensions, reply=reply):
                    frames = self.dashboard(
                        [ord("l"), ord("q")],
                        dimensions,
                        log_status_submit=submit,
                    )
                    self.assertEqual(
                        frames[-1][1], (job + " running")[: dimensions[1] - 1]
                    )
                    self.assertTrue(frames[-1][2].startswith("STALE state"))
                    if dimensions[1] >= 65:
                        self.assertIn("invalid reply", frames[-1][2])


if __name__ == "__main__":
    unittest.main()
