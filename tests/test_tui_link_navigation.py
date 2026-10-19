"""Linked logs stay read-only, independent of browser filters, and interruptible."""

from concurrent.futures import Future
import copy
import curses
import json
import unittest
from unittest import mock

import test_tui_browser as fixtures

from agent_builds import tui
from agent_builds.store import Store
from agent_builds.tui_browser import BrowserCache
from agent_builds.tui_data import LazyStore
from agent_builds.tui_logs import LogView, TailCache


def completed(loader, key):
    future = Future()
    try:
        future.set_result(loader(key))
    except Exception as error:
        future.set_exception(error)
    return future


def text(frame):
    return "\n".join(frame.values())


class PendingLinks:
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


class TuiLinkedLogNavigationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TuiBrowserTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root, self.store, self.jobs = (
            self.fixture.root,
            self.fixture.store,
            self.fixture.jobs,
        )
        self.target = self.jobs[2]
        self.source = self.jobs[1]
        for index, job in enumerate(self.jobs):
            self.store.log_path(job).write_text("LINKED OUTPUT {}\n".format(index))
        for job in self.jobs[:2]:
            self.store.db.execute(
                "UPDATE jobs SET reason=?,wait_context=? WHERE id=?",
                (
                    "repository already building",
                    json.dumps(
                        dict(
                            measured_at=100,
                            blocking_job=dict(
                                id=self.target,
                                elapsed_seconds=0,
                                wait_seconds=0,
                                command_display="true",
                            ),
                        )
                    ),
                    job,
                ),
            )
        self.tools = {
            "watches": [self.watch("watch-one", self.target)],
            "groups": [self.group("group-one", self.target)],
            "plans": [self.plan("plan-one", self.target)],
        }

    def watch(self, handle, job):
        return dict(
            id=handle,
            state="waiting",
            complete=False,
            controller_alive=None,
            definition=dict(external_ref="fixture external task"),
            probe_count=1,
            external_idle_seconds=0,
            reason="Read-only fixture observation",
            probes=[dict(job_id=job)],
        )

    def group(self, handle, job):
        return dict(
            id=handle,
            state="failed",
            name="fixture group",
            jobs=[self.store.get(job)],
        )

    def plan(self, handle, job):
        row = self.store.get(job)
        return dict(
            id=handle,
            state="failed",
            name="fixture plan",
            elapsed_seconds=0,
            controller_alive=None,
            reason="",
            stages=[
                dict(
                    name="check",
                    state="failed",
                    job_id=job,
                    job=row,
                    reason="",
                )
            ],
        )

    def dashboard(self, keys, dimensions=(24, 100), **kwargs):
        config = self.store.config_path.read_bytes()
        rows = list(map(tuple, self.store.db.execute("SELECT * FROM jobs ORDER BY id")))

        def tool(root, key):
            kind, repository = key
            return copy.deepcopy(self.tools[kind])

        with mock.patch.object(
            LazyStore, "writable", side_effect=AssertionError("queue mutation")
        ), mock.patch.object(
            Store, "_ensure_schema", side_effect=AssertionError("queue initialized")
        ), mock.patch.object(
            Store, "recover", side_effect=AssertionError("recovery from inspection")
        ), mock.patch.object(
            TailCache, "read", side_effect=AssertionError("synchronous inline log read")
        ), mock.patch.object(
            LogView, "poll", side_effect=AssertionError("synchronous focused log read")
        ), mock.patch.object(
            tui.subprocess,
            "Popen",
            side_effect=AssertionError("native command launched"),
        ), mock.patch.object(tui, "read_tool", side_effect=tool), mock.patch(
            "agent_builds.external.lines",
            side_effect=lambda row: [row["id"], row["state"], row["reason"]],
        ):
            frames = self.fixture.dashboard(keys, dimensions, **kwargs)
        self.assertEqual(self.store.config_path.read_bytes(), config)
        self.assertEqual(
            list(map(tuple, self.store.db.execute("SELECT * FROM jobs ORDER BY id"))),
            rows,
        )
        return frames

    def test_every_link_opens_evicted_and_filtered_jobs_without_browser_handoff(self):
        for index in range(200):
            self.store.submit(
                self.root,
                ["true"],
                "local",
                "quick",
                1,
                0.25,
                "backlog-{}".format(index),
                "",
                5,
            )
        loaded = self.store.browser_rows()
        self.assertEqual(len(loaded), 200)
        self.assertNotIn(self.target, [row["id"] for row in loaded])
        self.assertIn(self.source, [row["id"] for row in loaded])
        destinations = (
            ("watch", 10, [ord("V"), 10], "browser", self.target),
            ("plan", 7, [ord("V"), 10], "browser", self.target),
            ("group", 9, [ord("V"), 10], "browser", self.target),
            ("jobs blocker", None, [ord("g")], "blocker", self.source),
            ("details blocker", None, [ord("i"), ord("g")], "blocker", self.source),
            ("why blocker", None, [ord("w"), -1, ord("g")], "browser", self.target),
        )
        for size in ((24, 100), (8, 20)):
            for name, view, opening, kind, handle in destinations:
                if name == "group" and size[1] < 65:
                    continue  # Groups deliberately remain a full-size-only view.
                requests = []

                def submit(loader, key):
                    requests.append(key)
                    return completed(loader, key)

                with self.subTest(size=size, source=name), mock.patch.object(
                    tui, "choose_view", return_value=view
                ), mock.patch(
                    "agent_builds.tui_reads.submit_read", side_effect=completed
                ):
                    frames = self.dashboard(
                        [ord("/")] + opening + [-1, ord("q")],
                        size,
                        query=self.source,
                        link_submit=submit,
                    )
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0][1:], (kind, handle))
                self.assertIn("LOG LIVE", frames[-1][0])
                self.assertTrue(frames[-1][1].startswith(self.target))
                self.assertIn("LINKED OUTPUT 2", text(frames[-1]))
                self.assertEqual(len(self.fixture.prompt_calls), 1)

    def test_return_preserves_browser_controls_selection_and_selected_watch(self):
        self.tools["watches"].append(self.watch("watch-two", self.jobs[0]))
        requests, controls = [], []
        original = BrowserCache.get

        def browse(cache, loaded, mode, query, order, repository):
            controls.append((mode, query, order, repository))
            return original(cache, loaded, mode, query, order, repository)

        def submit(loader, key):
            requests.append(key)
            return completed(loader, key)

        with mock.patch.object(tui, "choose_view", return_value=10), mock.patch.object(
            BrowserCache, "get", browse
        ):
            frames = self.dashboard(
                [
                    ord("/"),
                    ord("R"),
                    ord("f"),
                    ord("f"),
                    ord("s"),
                    ord("s"),
                    9,
                    ord("V"),
                    ord("j"),
                    10,
                    -1,
                    ord("l"),
                    ord("V"),
                    10,
                    -1,
                    ord("q"),
                ],
                query=self.source,
                link_submit=submit,
            )
        self.assertEqual(
            controls[-1], ("waiting", self.source, "runtime", str(self.root))
        )
        self.assertEqual([key[1:] for key in requests], [("browser", self.jobs[0])] * 2)
        self.assertEqual(frames[7][5], frames[12][5])
        self.assertIn("JOBS waiting", frames[12][5])
        self.assertIn("sort: runtime", frames[12][5])
        self.assertIn("search: " + self.source, frames[12][5])
        # The full expanded table marks selection with A_REVERSE, not a
        # textual prefix. This literal-ID query leaves exactly one visible row.
        selected = [
            line for line in frames[12].values() if line.startswith(self.source)
        ]
        self.assertEqual(len(selected), 1)
        self.assertIn(self.source, selected[0])
        self.assertIn("LINKED OUTPUT 0", text(frames[-1]))

    def test_pending_lookup_keeps_origin_help_resize_and_quit_available(self):
        for initial_size in ((24, 100), (8, 20)):
            pending = PendingLinks()
            size = [initial_size]
            keys = iter(
                [
                    ord("V"),
                    10,
                    -1,
                    ord("?"),
                    ord("p"),
                    ord("q"),
                    curses.KEY_RESIZE,
                    ord("q"),
                ]
            )

            def key():
                value = next(keys)
                if value == curses.KEY_RESIZE:
                    size[0] = (8, 20) if size[0][1] >= 65 else (24, 100)
                return value

            with self.subTest(size=initial_size), mock.patch.object(
                tui, "choose_view", return_value=10
            ):
                frames = self.dashboard(key, lambda: size[0], link_submit=pending)
            self.assertEqual(len(pending.calls), 1)
            self.assertFalse(pending.calls[0][2].done())
            self.assertNotIn("LOG LIVE", "\n".join(text(frame) for frame in frames))
            self.assertTrue(any("BUILDR HELP" in frame.get(0, "") for frame in frames))
            self.assertTrue(frames[-1][size[0][0] - 2].startswith("Opening log "))
            self.assertEqual(self.fixture.prompt_calls, [])

    def test_help_defers_completed_lookup_until_origin_is_visible_again(self):
        for size in ((24, 100), (8, 20)):
            pending = PendingLinks()
            steps = iter([ord("V"), 10, ord("?"), -1, ord("q"), -1, ord("q")])

            def key():
                value = next(steps)
                if value == -1 and not pending.calls[0][2].done():
                    pending.finish()
                return value

            with self.subTest(size=size), mock.patch.object(
                tui, "choose_view", return_value=10
            ):
                frames = self.dashboard(key, size, link_submit=pending)
            self.assertIn("BUILDR HELP", frames[3][0])
            self.assertIn("BUILDR HELP", frames[4][0])
            self.assertIn("LOG LIVE", frames[5][0])
            self.assertIn("LINKED OUTPUT 2", text(frames[-1]))
            self.assertEqual(len(pending.calls), 1)

    def test_group_lookup_defers_completed_handoff_while_source_needs_full_size(self):
        pending = PendingLinks()
        size = [(24, 100)]
        steps = iter(
            [
                ord("V"),
                10,
                curses.KEY_RESIZE,
                -1,
                -1,
                curses.KEY_RESIZE,
                -1,
                ord("q"),
            ]
        )
        completed_while_hidden = [False]

        def key():
            value = next(steps)
            if value == curses.KEY_RESIZE:
                size[0] = (8, 20) if size[0][1] >= 65 else (24, 100)
            elif value == -1 and not completed_while_hidden[0]:
                self.assertEqual(size[0], (8, 20))
                self.assertEqual(len(pending.calls), 1)
                pending.finish()
                completed_while_hidden[0] = True
            return value

        with mock.patch.object(tui, "choose_view", return_value=9):
            frames = self.dashboard(key, lambda: size[0], link_submit=pending)
        self.assertTrue(completed_while_hidden[0])
        self.assertEqual(len(pending.calls), 1)
        self.assertEqual(pending.calls[0][1][1:], ("browser", self.target))
        for frame in frames[3:6]:
            self.assertIn("GROUPS", text(frame))
            self.assertIn("Resize to 65x15", text(frame))
            self.assertTrue(frame[6].startswith("Opening log "))
            self.assertNotIn("LOG LIVE", text(frame))
        self.assertIn("LOG LIVE", frames[6][0])
        self.assertTrue(frames[6][1].startswith(self.target))
        self.assertIn("LINKED OUTPUT 2", text(frames[6]))
        self.assertEqual(self.fixture.prompt_calls, [])

    def test_repeated_and_aba_intents_discard_old_success_or_error_without_overlap(
        self,
    ):
        self.tools["watches"].append(self.watch("watch-two", self.jobs[0]))
        for size in ((24, 100), (8, 20)):
            for failed in (False, True):
                pending = PendingLinks()
                steps = iter(
                    [ord("V"), 10, 10, ord("j"), 10, ord("k"), 10, -1, -1, -1, ord("q")]
                )
                completions = [0]

                def key():
                    value = next(steps)
                    if value == -1:
                        if completions[0] == 0:
                            self.assertEqual(len(pending.calls), 1)
                            pending.finish(
                                0, OSError("obsolete lookup error") if failed else None
                            )
                        elif completions[0] == 1:
                            self.assertEqual(len(pending.calls), 2)
                            pending.finish(1)
                        completions[0] += 1
                    return value

                with self.subTest(size=size, failed=failed), mock.patch.object(
                    tui, "choose_view", return_value=10
                ):
                    frames = self.dashboard(key, size, link_submit=pending)
                requests = [key for loader, key, future in pending.calls]
                self.assertEqual(len(requests), 2)
                self.assertGreater(requests[1][0], requests[0][0] + 1)
                self.assertEqual(
                    [key[1:] for key in requests], [("browser", self.target)] * 2
                )
                self.assertIn("LOG LIVE", frames[-1][0])
                self.assertIn("LINKED OUTPUT 2", text(frames[-1]))
                for frame in frames:
                    self.assertNotIn("obsolete lookup error", text(frame))

    def test_changing_origin_view_or_source_job_abandons_late_lookup(self):
        for source in ("watch", "jobs"):
            for failed in (False, True):
                pending = PendingLinks()
                opening = (
                    [ord("V"), 10, ord("V")]
                    if source == "watch"
                    else [ord("g"), ord("j")]
                )
                steps = iter(opening + [-1, -1, ord("q")])
                finished = [False]

                def key():
                    value = next(steps)
                    if value == -1 and not finished[0]:
                        pending.finish(
                            error=OSError("abandoned lookup error") if failed else None
                        )
                        finished[0] = True
                    return value

                with self.subTest(source=source, failed=failed), mock.patch.object(
                    tui, "choose_view", side_effect=[10, 0]
                ):
                    frames = self.dashboard(key, link_submit=pending)
                self.assertEqual(len(pending.calls), 1)
                for frame in frames:
                    self.assertNotIn("LOG LIVE", text(frame))
                    self.assertNotIn("abandoned lookup error", text(frame))
                self.assertNotIn("Opening log", text(frames[-1]))

    def test_lookup_failure_remains_visible_after_resize_without_automatic_retry(self):
        for initial_size in ((24, 100), (8, 20)):
            size, now, requests = [initial_size], [100], []
            steps = iter([ord("V"), 10, -1, curses.KEY_RESIZE, -1, -1, ord("q")])

            def submit(loader, key):
                requests.append(key)
                future = Future()
                future.set_exception(OSError("fixture link unavailable"))
                return future

            def key():
                value = next(steps)
                now[0] += 100
                if value == curses.KEY_RESIZE:
                    size[0] = (8, 20) if size[0][1] >= 65 else (24, 100)
                return value

            with self.subTest(size=initial_size), mock.patch.object(
                tui, "choose_view", return_value=10
            ):
                frames = self.dashboard(
                    key, lambda: size[0], clock=lambda: now[0], link_submit=submit
                )
            self.assertEqual(len(requests), 1)
            self.assertTrue(
                frames[2][initial_size[0] - 2].startswith("Log unavailable:")
            )
            self.assertTrue(frames[-1][size[0][0] - 2].startswith("Log unavailable:"))
            full = frames[2] if initial_size[1] >= 65 else frames[-1]
            self.assertIn("fixture link unavailable", text(full))
            self.assertNotIn("LOG LIVE", "\n".join(text(frame) for frame in frames))
            self.assertEqual(self.fixture.prompt_calls, [])

    def test_link_handoff_and_shared_action_keys_never_launch_or_mutate_jobs(self):
        for size in ((24, 100), (8, 20)):
            with self.subTest(size=size), mock.patch.object(
                tui, "choose_view", return_value=10
            ):
                frames = self.dashboard(
                    [ord("V"), 10, -1]
                    + list(map(ord, "cpntoxreag"))
                    + [10, ord("?"), ord("c"), ord("p"), ord("q"), ord("q")],
                    size,
                    link_submit=completed,
                )
            self.assertIn("LOG ", frames[-1][0])
            self.assertIn("LINKED OUTPUT 2", text(frames[-1]))
            self.assertEqual(self.fixture.prompt_calls, [])

    def test_back_returns_to_each_log_source_in_full_and_narrow_panes(self):
        sources = (
            (0, [ord("l")], "JOBS"),
            (3, [ord("i"), ord("l")], "DETAILS"),
            (14, [ord("w"), -1, ord("g")], "WHY"),
            (7, [ord("V"), 10], "PLANS"),
            (9, [ord("V"), 10], "GROUPS"),
            (10, [ord("V"), 10], "EXTERNAL"),
        )
        for size in ((24, 100), (8, 20)):
            for source, opening, heading in sources:
                if source == 9 and size[1] < 65:
                    continue
                with self.subTest(size=size, source=source), mock.patch.object(
                    tui, "choose_view", return_value=source
                ), mock.patch(
                    "agent_builds.tui_reads.submit_read", side_effect=completed
                ):
                    frames = self.dashboard(
                        [ord("/")] + opening + [-1, ord("b"), -1, ord("q")],
                        size,
                        query=self.source,
                    )
                self.assertTrue(any("LOG LIVE" in f.get(0, "") for f in frames))
                self.assertIn(heading, text(frames[-1]))
                if source in (3, 14):
                    self.assertIn(self.source, text(frames[-1]))

    def test_back_retains_selected_watch_and_handles_resize_to_full_only_source(self):
        self.tools["watches"].append(self.watch("watch-two", self.jobs[0]))
        with mock.patch.object(tui, "choose_view", return_value=10):
            frames = self.dashboard(
                [ord("V"), ord("j"), 10, -1, ord("b"), -1, ord("q")]
            )
        self.assertIn("watch-two", text(frames[-1]))
        size = [(24, 100)]
        steps = iter([ord("V"), 10, -1, curses.KEY_RESIZE, ord("b"), ord("q")])

        def key():
            value = next(steps)
            if value == curses.KEY_RESIZE:
                size[0] = (8, 20)
            return value

        with mock.patch.object(tui, "choose_view", return_value=9):
            frames = self.dashboard(key, lambda: size[0])
        self.assertIn("GROUPS", text(frames[-1]))
        self.assertIn("Resize to 65x15", text(frames[-1]))


if __name__ == "__main__":
    unittest.main()
