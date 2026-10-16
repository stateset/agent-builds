"""Dashboard browsing is local to the view and preserves shared reservations."""

import copy
import curses
import os
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_builds import tui, tui_browser
from agent_builds.store import Store
from agent_builds.tui_browser import (
    filter_rows,
    sort_rows,
    page_size,
    duration,
    job_line,
    SelectedJobRead,
    BrowserCache,
)


class TuiBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state")
        self.store.update_config(dict(min_free_gb=0, min_available_gb=0, nice=0))
        self.jobs = []
        for key, state in (
            ("running", "running"),
            ("waiting", "queued"),
            ("failed", "failed"),
        ):
            job = self.store.submit(
                self.root, ["true"], "local", "quick", 1, 0.25, key, "", 5
            )
            self.store.db.execute("UPDATE jobs SET state=? WHERE id=?", (state, job))
            self.jobs.append(job)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def dashboard(self, keys, dimensions=(20, 100), query="", clock=None):
        screen = mock.Mock()
        screen.getmaxyx.return_value = dimensions
        screen.getch.side_effect = keys
        frame, frames = {}, []
        screen.erase.side_effect = frame.clear
        screen.addnstr.side_effect = lambda y, x, text, count, attr: frame.update(
            {y: text[:count]}
        )
        screen.refresh.side_effect = lambda: frames.append(dict(frame))
        machine = dict(
            cpus=8,
            load=0,
            available_gb=16,
            total_gb=32,
            free_gb=20,
            cpu_pressure=0,
            memory_pressure=0,
            io_pressure=0,
        )
        with mock.patch.dict(
            os.environ, AGENT_BUILDS_STATE=str(self.store.root)
        ), mock.patch(
            "agent_builds.tui.time.monotonic", side_effect=clock or (lambda: 100)
        ), mock.patch("agent_builds.tui.curses.curs_set"), mock.patch(
            "agent_builds.tui.curses.has_colors", return_value=False
        ), mock.patch("agent_builds.tui.curses.color_pair", return_value=0), mock.patch(
            "agent_builds.tui.snapshot", return_value=machine
        ), mock.patch("agent_builds.tui.ProcessSampler") as sampler, mock.patch.object(
            Store, "recover_if_due", return_value=False
        ), mock.patch.object(
            Store, "update_config", side_effect=AssertionError("settings changed")
        ), mock.patch(
            "agent_builds.tui.cancel_job", side_effect=AssertionError("job cancelled")
        ), mock.patch(
            "agent_builds.tui.spawn", side_effect=AssertionError("job launched")
        ), mock.patch("agent_builds.tui.prompt", return_value=query):
            sampler.return_value.sample.return_value = []
            tui.main(screen, self.root)
            self.process_samples = sampler.return_value.sample.call_count
        return frames

    def test_filters_and_literal_search_cover_commands_paths_and_agent_origin(self):
        rows = [
            dict(
                id="One",
                state="queued",
                cwd="/repo/Alpha",
                command_display="cargo test [slow]",
                reason="repository already building",
                origin=dict(pane_id="w1:pA"),
            ),
            dict(
                id="Two", state="failed", cwd="/repo/Beta", command_display="npm test"
            ),
            dict(id="Three", state="cancelled", exit_code=124),
            dict(id="Four", state="cancelled", exit_code=130),
            dict(id="Five", state="lost"),
        ]
        original = copy.deepcopy(rows)
        self.assertEqual([r["id"] for r in filter_rows(rows, "active")], ["One"])
        self.assertEqual([r["id"] for r in filter_rows(rows, "waiting")], ["One"])
        self.assertEqual(
            [r["id"] for r in filter_rows(rows, "problems")], ["Two", "Three", "Five"]
        )
        for query in ("ALPHA", "[slow]", "W1:PA", "One", "already building"):
            self.assertEqual([r["id"] for r in filter_rows(rows, query=query)], ["One"])
        self.assertEqual(filter_rows(rows, "waiting", "npm"), [])
        self.assertEqual(rows, original)

    def test_search_and_filters_keep_global_reservations_visible(self):
        frames = self.dashboard([ord("f"), ord("/"), ord("q")], query=self.jobs[1])
        text = "\n".join(frames[-1].values())
        self.assertIn("JOBS active | 1/3 loaded", text)
        self.assertIn(self.jobs[1], text)
        self.assertIn("Managed reservations: 1/", text)
        self.assertNotIn(self.jobs[0], text)
        self.assertFalse(self.store.get(self.jobs[1])["cancel_requested"])

    def test_help_consumes_action_keys_without_mutating_or_losing_selection(self):
        frames = self.dashboard(
            [ord("j"), ord("?"), *map(ord, "pcbnat"), ord("?"), ord("q")]
        )
        self.assertTrue(
            any("BUILDR HELP" in "\n".join(frame.values()) for frame in frames)
        )
        final = "\n".join(frames[-1].values())
        self.assertIn(self.jobs[1], final)
        self.assertEqual(self.store.counts()["running"], 1)
        self.assertFalse(self.store.config()["paused"])

    def test_empty_search_and_page_keys_are_safe_in_full_and_narrow_panes(self):
        for dimensions in ((20, 100), (8, 20)):
            frames = self.dashboard(
                [
                    ord("/"),
                    curses.KEY_END,
                    curses.KEY_NPAGE,
                    curses.KEY_PPAGE,
                    curses.KEY_HOME,
                    ord("q"),
                ],
                dimensions,
                "no such job",
            )
            text = "\n".join(frames[-1].values())
            self.assertIn("No matching", text)
            self.assertTrue(frames[-1])
            self.assertLessEqual(len(frames[-1]), dimensions[0])

    def test_enter_opens_details_and_home_end_change_only_selection(self):
        frames = self.dashboard([curses.KEY_END, curses.KEY_HOME, 10, ord("q")])
        self.assertIn("JOB DETAILS", "\n".join(frames[-1].values()))
        self.assertIn(self.jobs[0], "\n".join(frames[-1].values()))

    def test_smallest_pane_shows_selected_wait_reason_before_repository_and_logs(self):
        row = self.store.get(self.jobs[1])
        row["reason"] = "repository locked"
        health = dict(
            status="busy",
            waiting_jobs=1,
            reservations=dict(jobs=1, cpu_slots=1, memory_gb=0.25),
        )
        lines = tui.compact_lines(
            [row],
            0,
            [],
            0,
            0,
            self.store.config(),
            8,
            20,
            queue_health=health,
            browser=dict(mode="waiting", query="", loaded=3),
        )
        self.assertEqual(len(lines), 8)
        self.assertIn("repository locked", lines)
        self.assertIn("?", lines[-1])

    def test_idle_keyboard_frames_reuse_job_and_machine_reads(self):
        reads = []
        original = Store.browser_rows

        def observed(store, *args, **kwargs):
            reads.append(store.root)
            return original(store, *args, **kwargs)

        with mock.patch.object(Store, "browser_rows", observed):
            frames = self.dashboard([-1] * 20 + [ord("q")])
        self.assertEqual(len(frames), 21)
        self.assertEqual(len(reads), 1)

    def test_job_snapshot_refreshes_after_the_interval(self):
        now, reads = [100], []
        original = Store.browser_rows

        def observed(store, *args, **kwargs):
            reads.append(now[0])
            return original(store, *args, **kwargs)

        def key():
            if now[0] == 100:
                now[0] = 102
                return -1
            return ord("q")

        with mock.patch.object(Store, "browser_rows", observed):
            self.dashboard(key, clock=lambda: now[0])
        self.assertEqual(reads, [100, 102])

    def test_process_scans_run_only_in_the_process_view(self):
        self.dashboard([-1, ord("q")])
        self.assertEqual(self.process_samples, 0)
        self.dashboard([ord("v"), ord("q")])
        self.assertEqual(self.process_samples, 1)

    def test_focused_logs_work_in_narrow_panes_and_consume_action_keys(self):
        self.store.log_path(self.jobs[0]).write_text("readable log\n")
        for size in ((20, 100), (8, 20)):
            frames = self.dashboard(
                [ord("l"), *map(ord, "pcbnat"), ord(" "), ord("q")], size
            )
            text = "\n".join(frames[-1].values())
            self.assertIn("LOG HELD", text)
            self.assertIn("readable log", text)
            self.assertIn(self.jobs[0], text)
        self.assertFalse(self.store.config()["paused"])
        self.assertEqual(self.process_samples, 0)

    def test_log_focus_stays_on_finished_job_when_active_filter_changes(self):
        self.store.log_path(self.jobs[0]).write_text("pinned output\n")
        self.store.log_path(self.jobs[1]).write_text("wrong job output\n")
        now = [100]
        keys = iter([ord("f"), ord("l"), ord(" "), -1, ord("q")])

        def key():
            value = next(keys)
            if value == -1:
                self.store.db.execute(
                    "UPDATE jobs SET state='succeeded' WHERE id=?", (self.jobs[0],)
                )
                now[0] = 102
            return value

        frames = self.dashboard(key, clock=lambda: now[0])
        text = "\n".join(frames[-1].values())
        self.assertIn(self.jobs[0], text)
        self.assertIn("succeeded", text)
        self.assertIn("pinned output", text)
        self.assertNotIn("wrong job output", text)

    def test_log_home_page_and_end_preserve_selection_and_return_to_live(self):
        self.store.log_path(self.jobs[0]).write_text(
            "\n".join("line-" + str(i) for i in range(60)) + "\n"
        )
        frames = self.dashboard(
            [
                ord("l"),
                curses.KEY_HOME,
                curses.KEY_NPAGE,
                curses.KEY_PPAGE,
                curses.KEY_END,
                ord("l"),
                ord("q"),
            ]
        )
        self.assertTrue(any("line-0" in "\n".join(frame.values()) for frame in frames))
        self.assertTrue(any("line-59" in "\n".join(frame.values()) for frame in frames))
        self.assertIn("JOBS all", "\n".join(frames[-1].values()))

    def test_log_search_navigation_stays_local_in_full_and_narrow_panes(self):
        self.store.log_path(self.jobs[0]).write_text(
            "first NEEDLE\n" + "padding\n" * 40 + "last needle\n"
        )
        for size in ((20, 100), (8, 20)):
            frames = self.dashboard(
                [ord("l"), ord("/"), ord("n"), ord("N"), ord("q")],
                size,
                query="needle",
            )
            text = "\n".join(frames[-1].values())
            self.assertIn("LOG HELD", text)
            self.assertIn("Find 1/2: needle", text)
            self.assertIn("first NEEDLE", text)
        self.assertFalse(self.store.config()["paused"])

    def test_cancelled_log_search_preserves_live_mode_and_browser_query(self):
        self.store.log_path(self.jobs[0]).write_text("separate log text\n")
        frames = self.dashboard(
            [ord("/"), ord("l"), ord("/"), ord("l"), ord("q")],
            query=None,
        )
        self.assertTrue(
            any("LOG LIVE" in "\n".join(frame.values()) for frame in frames)
        )
        self.assertIn("JOBS all", "\n".join(frames[-1].values()))

    def test_prompt_keeps_typed_text_visible_in_a_narrow_pane(self):
        screen = mock.Mock()
        screen.getmaxyx.return_value = (8, 20)
        screen.get_wch.side_effect = ["n", "e", "e", "d", "l", "e", "\n"]
        with mock.patch("agent_builds.tui.curses.curs_set"):
            self.assertEqual(
                tui.prompt(screen, "Find in loaded log (blank clears)"), "needle"
            )
        drawn = [call.args[2] for call in screen.addnstr.call_args_list]
        self.assertTrue(any(line.endswith("needle") for line in drawn))
        self.assertTrue(all(len(line) <= 19 for line in drawn))
        screen.timeout.assert_called_with(200)

    def test_display_orders_are_deterministic_and_leave_input_untouched(self):
        rows = [
            dict(
                id="done",
                state="succeeded",
                created=1,
                cwd="/z",
                wait_seconds=900,
                elapsed_seconds=900,
            ),
            dict(id="young", state="queued", created=3, cwd="/B", wait_seconds=10),
            dict(id="run", state="running", created=2, cwd="/a", elapsed_seconds=20),
            dict(id="old-b", state="queued", created=1, cwd="/b", wait_seconds=80),
            dict(id="old-a", state="queued", created=1, cwd="/b", wait_seconds=80),
            dict(
                id="cancel", state="cancelling", created=2, cwd="/a", elapsed_seconds=40
            ),
        ]
        original = copy.deepcopy(rows)
        ids = lambda order: [row["id"] for row in sort_rows(rows, order)]
        self.assertEqual(ids("queue"), [row["id"] for row in rows])
        self.assertEqual(ids("wait")[:3], ["old-a", "old-b", "young"])
        self.assertEqual(ids("runtime")[:3], ["cancel", "run", "done"])
        self.assertEqual(
            ids("repository"), ["cancel", "run", "old-a", "old-b", "young", "done"]
        )
        self.assertEqual(rows, original)
        self.assertEqual(sort_rows([], "runtime"), [])
        with self.assertRaises(ValueError):
            sort_rows(rows, "invalid")

    def test_table_durations_and_layout_handle_sizes_and_invalid_numbers(self):
        for value, text in (
            (0, "0s"),
            (59, "59s"),
            (90, "1m30s"),
            (3660, "1h01m"),
            (90000, "1d01h"),
            (float("inf"), "0s"),
            (None, "0s"),
        ):
            self.assertEqual(duration(value), text)
        self.assertEqual(page_size(46, expanded=True), 36)
        self.assertEqual(page_size(8, compact=True, expanded=True), 1)
        self.assertEqual(page_size(20), 4)
        row = self.store.get(self.jobs[1])
        row.update(wait_seconds=90, elapsed_seconds=3660)
        self.assertIn("1m30s", job_line(row, 100))
        self.assertIn("1h01m", job_line(row, 50))
        self.assertIn(row["id"], job_line(row, 20))

    def test_sort_preserves_selected_job_and_shared_state_in_full_and_narrow_panes(
        self,
    ):
        for size in ((20, 100), (8, 20)):
            frames = self.dashboard([ord("j"), ord("s"), ord("l"), ord("q")], size)
            text = "\n".join(frames[-1].values())
            self.assertIn(self.jobs[1], text)
            self.assertIn("LOG LIVE", text)
        self.assertFalse(self.store.config()["paused"])
        self.assertFalse(
            any(self.store.get(job)["cancel_requested"] for job in self.jobs)
        )

    def test_expanded_table_uses_height_pages_and_retains_selection_when_returning(
        self,
    ):
        for index in range(45):
            job = self.store.submit(
                self.root,
                ["true"],
                "local",
                "quick",
                1,
                0.25,
                "extra-" + str(index),
                "",
                5,
            )
            self.store.db.execute("UPDATE jobs SET state='failed' WHERE id=?", (job,))
        frames = self.dashboard(
            [9, curses.KEY_NPAGE, ord("l"), ord("l"), ord("q")], (46, 100)
        )
        table = "\n".join(frames[2].values())
        self.assertIn("WAIT", table)
        self.assertIn("Selected 37/48", table)
        self.assertGreaterEqual(
            sum(job["id"] in table for job in self.store.rows()), 36
        )
        self.assertIn("Selected 37/48", "\n".join(frames[-1].values()))
        self.assertEqual(self.process_samples, 0)

    def test_expanded_table_does_not_read_inline_logs_and_is_safe_when_empty(self):
        keys = iter([9, -1, ord("q")])
        original_tail = tui.TailCache.read
        reads = []

        def tail(*args):
            reads.append(len(reads))
            return original_tail(*args)

        with mock.patch.object(tui.TailCache, "read", tail):
            self.dashboard(keys)
        self.assertEqual(len(reads), 1)
        for size in ((20, 100), (8, 20)):
            frames = self.dashboard(
                [9, ord("/"), ord("s"), curses.KEY_END, 9, ord("q")], size, "absent job"
            )
            self.assertIn("No matching", "\n".join(frames[-1].values()))

    def test_sorted_snapshot_refresh_preserves_selection_when_order_changes(self):
        now = [100]
        keys = iter([ord("s"), curses.KEY_HOME, -1, ord("l"), ord("q")])
        selected = self.jobs[1]

        def key():
            value = next(keys)
            if value == -1:
                self.store.db.execute(
                    "UPDATE jobs SET state='succeeded' WHERE id=?", (selected,)
                )
                now[0] = 102
            return value

        frames = self.dashboard(key, clock=lambda: now[0])
        self.assertIn(selected, "\n".join(frames[-1].values()))

    def test_help_and_log_focus_consume_sort_and_table_keys(self):
        frames = self.dashboard(
            [ord("?"), ord("s"), 9, ord("?"), ord("l"), ord("s"), 9, ord("l"), ord("q")]
        )
        text = "\n".join(frames[-1].values())
        self.assertIn("JOBS all", text)
        self.assertNotIn("sort: wait", text)
        self.assertNotIn("Selected 1/", text)

    def test_narrow_table_page_size_matches_visible_rows(self):
        for index in range(12):
            self.store.submit(
                self.root,
                ["true"],
                "local",
                "quick",
                1,
                0.25,
                "narrow-" + str(index),
                "",
                5,
            )
        rows = self.store.rows()
        for size in ((8, 20), (8, 40), (14, 50), (14, 64)):
            expected = rows[page_size(size[0], compact=True, expanded=True)]["id"]
            frames = self.dashboard([9, curses.KEY_NPAGE, ord("l"), ord("q")], size)
            table = "\n".join(frames[2].values())
            self.assertIn(">" + expected, table)
            self.assertEqual("WAIT" in frames[2][5], size[1] - 1 >= 40)
            self.assertIn(expected, "\n".join(frames[-1].values()))
            self.assertLessEqual(len(frames[2]), size[0])

    def test_ordinary_browsing_never_expands_job_evidence(self):
        self.store.db.execute(
            "UPDATE jobs SET source='invalid JSON',batch='invalid JSON'"
        )
        with mock.patch.object(
            Store, "describe", side_effect=AssertionError("full job expanded")
        ):
            for size in ((46, 100), (8, 20)):
                frames = self.dashboard(
                    [9, ord("s"), ord("?"), ord("?"), ord("l"), ord("l"), 9, ord("q")],
                    size,
                )
                self.assertTrue(frames[-1])
        self.assertFalse(self.store.config()["paused"])

    def test_details_fetch_only_selected_jobs_and_cache_keyboard_frames(self):
        self.store.db.execute(
            "UPDATE jobs SET source=? WHERE id=?",
            (json.dumps({"submission": {"head": "selected-evidence"}}), self.jobs[0]),
        )
        original, reads = Store.get, []

        def get(store, job):
            reads.append(job)
            return original(store, job)

        with mock.patch.object(Store, "get", get):
            frames = self.dashboard([10, -1, -1, ord("j"), ord("q")], (46, 100))
        self.assertEqual(reads, self.jobs[:2])
        self.assertTrue(
            any("selected-evidence" in "\n".join(frame.values()) for frame in frames)
        )

    def test_selected_read_retries_errors_without_reusing_another_jobs_details(self):
        load = mock.Mock(
            side_effect=[
                {"id": "one"},
                sqlite3.OperationalError("writer busy"),
                {"id": "two"},
            ]
        )
        cache = SelectedJobRead(load)
        self.assertEqual(cache.get("one", 0), {"id": "one"})
        self.assertEqual(cache.get("one", 0.9), {"id": "one"})
        self.assertEqual(load.call_count, 1)
        self.assertIsNone(cache.get("two", 0.9))
        self.assertEqual(cache.error, "writer busy")
        self.assertIsNone(cache.get("two", 1))
        self.assertEqual(load.call_count, 2)
        self.assertEqual(cache.get("two", 2), {"id": "two"})
        self.assertIsNone(cache.error)
        self.assertIsNone(cache.get(None, 2.1))
        self.assertIsNone(cache.value)
        self.assertEqual(load.call_count, 3)

    def test_details_read_errors_are_visible_and_rate_limited(self):
        with mock.patch.object(
            Store, "get", side_effect=sqlite3.OperationalError("fixture busy")
        ) as get:
            frames = self.dashboard([10, -1, -1, ord("q")])
        self.assertEqual(get.call_count, 1)
        self.assertIn(
            "Details unavailable: fixture busy", "\n".join(frames[-1].values())
        )

    def test_inline_wait_reads_are_cached_and_released_in_log_focus(self):
        original, reads = Store.get_wait_context, []

        def context(store, job):
            reads.append(job)
            return original(store, job)

        with mock.patch.object(Store, "get_wait_context", context):
            self.dashboard([-1, -1, ord("l"), -1, ord("l"), ord("q")])
        self.assertEqual(reads, [self.jobs[0], self.jobs[0]])

    def test_blocker_jump_reads_selected_observation_and_preserves_navigation(self):
        context = dict(
            blocking_job=dict(
                id=self.jobs[0], command_display="true", elapsed_seconds=0
            ),
            disk_free_gb=30,
            required_free_gb=0,
            measured_at=123,
        )
        self.store.db.execute(
            "UPDATE jobs SET wait_context=? WHERE id=?",
            (json.dumps(context), self.jobs[1]),
        )
        frames = self.dashboard([ord("j"), ord("g"), ord("l"), ord("q")])
        self.assertIn(self.jobs[0], "\n".join(frames[-1].values()))

    def test_idle_frames_reuse_filter_and_sort_results(self):
        with mock.patch.object(
            tui_browser, "filter_rows", wraps=filter_rows
        ) as filtered:
            with mock.patch.object(
                tui_browser, "sort_rows", wraps=sort_rows
            ) as ordered:
                self.dashboard([-1] * 200 + [ord("q")])
        self.assertEqual(filtered.call_count, 1)
        self.assertEqual(ordered.call_count, 1)

    def test_browser_cache_invalidates_on_every_control_and_new_snapshot(self):
        rows = self.store.browser_rows()
        before, cache = copy.deepcopy(rows), BrowserCache()
        initial = cache.get(rows)
        for _ in range(200):
            self.assertIs(cache.get(rows), initial)
        waiting = cache.get(rows, mode="waiting")
        self.assertEqual([r["id"] for r in waiting], [self.jobs[1]])
        self.assertEqual(cache.get(rows, query=self.jobs[2])[0]["state"], "failed")
        runtime = cache.get(rows, order="runtime")
        self.assertEqual(runtime[0]["id"], self.jobs[0])
        self.store.finish(self.jobs[0], "succeeded", 0)
        updated = self.store.browser_rows()
        self.assertEqual(len(cache.get(updated, mode="active")), 1)
        self.assertEqual(rows, before)

    def test_help_skips_inline_rendering_without_changing_selection(self):
        with mock.patch.object(tui.TailCache, "read", return_value=[]) as tail:
            frames = self.dashboard([ord("?"), -1, ord("q"), ord("q")])
        self.assertEqual(tail.call_count, 2)  # before and after help only
        self.assertIn("HELP", frames[1][0])
        self.assertIn(self.jobs[0], "\n".join(frames[-1].values()))

    def test_help_over_focused_logs_does_not_poll_the_hidden_snapshot(self):
        polls, original = [], tui.LogView.poll

        def poll(view, now):
            polls.append(now)
            return original(view, now)

        with mock.patch.object(tui.LogView, "poll", poll):
            self.dashboard([ord("l"), ord("?"), -1, ord("?"), ord("q")])
        self.assertEqual(len(polls), 2)

    def test_paint_removes_ansi_decoration_without_printing_control_fragments(self):
        screen = mock.Mock()
        screen.getmaxyx.return_value = (20, 100)
        tui.paint(screen, 1, 0, "\x1b[31merror\x1b[0m \x1b]0;title\x07plain")
        self.assertEqual(screen.addnstr.call_args.args[2], "error plain")

    def focus_repository(self):
        checkout = self.root / "focused"
        checkout.mkdir(exist_ok=True)
        for job in self.jobs[1:]:
            self.store.db.execute(
                "UPDATE jobs SET cwd=? WHERE id=?", (str(checkout), job)
            )
        return str(checkout)

    def test_repository_filter_uses_exact_checkout_and_combines_with_other_controls(
        self,
    ):
        rows = [
            dict(
                id="one", cwd="/work/app", state="queued", command_display="cargo test"
            ),
            dict(
                id="two",
                cwd="/work/app-copy",
                state="queued",
                command_display="cargo test",
            ),
            dict(
                id="three",
                cwd="/other/app",
                state="queued",
                command_display="cargo test",
            ),
            dict(
                id="four", cwd="/work/app", state="failed", command_display="npm test"
            ),
        ]
        before = copy.deepcopy(rows)
        with mock.patch.object(
            Path, "resolve", side_effect=AssertionError("filesystem lookup")
        ):
            scoped = filter_rows(rows, repository="/work/app")
            self.assertEqual([r["id"] for r in scoped], ["one", "four"])
            self.assertEqual(filter_rows(rows, "problems", "cargo", "/work/app"), [])
            self.assertEqual(
                filter_rows(rows, "waiting", "TEST", "/work/app"), [rows[0]]
            )
            self.assertEqual(filter_rows(rows, repository="/missing"), [])
        self.assertEqual(rows, before)

    def test_repository_focus_keeps_global_reservations_and_selected_log(self):
        self.focus_repository()
        for size in ((20, 100), (8, 20)):
            frames = self.dashboard([ord("j"), ord("R"), ord("l"), ord("q")], size)
            focused = "\n".join(frames[2].values())
            self.assertIn("2/3", focused)
            self.assertNotIn(self.jobs[0], focused)
            self.assertIn(
                "Managed reservations: 1/" if size[1] == 100 else "CPU 1/", focused
            )
            self.assertIn(self.jobs[1], "\n".join(frames[-1].values()))
        self.assertFalse(self.store.config()["paused"])

    def test_repository_toggle_keeps_search_and_can_leave_an_empty_result(self):
        self.focus_repository()
        for size in ((20, 100), (8, 20)):
            frames = self.dashboard(
                [ord("j"), ord("R"), ord("/"), ord("R"), ord("C"), ord("q")],
                size,
                query="absent",
            )
            self.assertIn("No matching", "\n".join(frames[3].values()))
            self.assertIn("No matching", "\n".join(frames[4].values()))
            self.assertIn("3/3", "\n".join(frames[-1].values()))

    def test_clear_filters_preserves_order_table_setting_and_selection(self):
        self.focus_repository()
        frames = self.dashboard(
            [
                9,
                ord("j"),
                ord("R"),
                ord("s"),
                ord("/"),
                ord("f"),
                ord("C"),
                ord("l"),
                ord("q"),
            ],
            (46, 120),
            query=self.jobs[1],
        )
        cleared = "\n".join(frames[7].values())
        self.assertIn("JOBS all | 3/3 loaded | sort: wait", cleared)
        self.assertIn("WAIT", cleared)
        self.assertNotIn("@focused", cleared)
        self.assertIn(self.jobs[1], "\n".join(frames[-1].values()))

    def test_help_and_log_focus_ignore_repository_and_clear_keys(self):
        self.focus_repository()
        frames = self.dashboard(list(map(ord, "jR/?CR?lCRlq")), query=self.jobs[1])
        final = "\n".join(frames[-1].values())
        self.assertIn("JOBS all | 1/3 loaded @focused", final)
        self.assertIn("search: " + self.jobs[1], final)
        self.assertIn(self.jobs[1], final)

    def test_repository_focus_is_available_from_details(self):
        checkout = self.focus_repository()
        frames = self.dashboard([ord("j"), 10, ord("R"), ord("C"), ord("q")], (46, 120))
        self.assertIn("DETAILS | repo: " + checkout, "\n".join(frames[3].values()))
        self.assertIn(self.jobs[1], "\n".join(frames[-1].values()))
        self.assertNotIn("DETAILS | repo:", "\n".join(frames[-1].values()))

    def test_repository_scope_cache_invalidates_and_reuses_idle_snapshots(self):
        checkout = self.focus_repository()
        rows, cache = self.store.browser_rows(), BrowserCache()
        initial = cache.get(rows)
        with mock.patch.object(
            tui_browser, "filter_rows", wraps=filter_rows
        ) as filtered:
            scoped = cache.get(rows, repository=checkout)
            for _ in range(100):
                self.assertIs(cache.get(rows, repository=checkout), scoped)
            self.assertEqual(filtered.call_count, 1)
            self.assertEqual([r["id"] for r in scoped], self.jobs[1:])
            self.assertEqual(cache.get(rows), initial)
            self.assertEqual(filtered.call_count, 2)
            self.assertEqual(len(cache.get(list(rows), repository=checkout)), 2)
            self.assertEqual(filtered.call_count, 3)

    def test_repository_focus_survives_refresh_and_clears_after_jobs_leave_scope(self):
        self.focus_repository()
        now = [100]
        keys = iter(map(ord, "jRzRCq"))

        def key():
            value = next(keys)
            if value == ord("z"):
                for job in self.jobs[1:]:
                    self.store.db.execute(
                        "UPDATE jobs SET cwd=? WHERE id=?", (str(self.root), job)
                    )
                now[0] = 102
            return value

        frames = self.dashboard(key, clock=lambda: now[0])
        self.assertIn("No matching jobs", "\n".join(frames[3].values()))
        self.assertIn("JOBS all | 3/3 loaded", "\n".join(frames[4].values()))

    def test_empty_repository_toggle_does_not_create_a_scope_or_read_evidence(self):
        self.store.db.execute("DELETE FROM jobs")
        with mock.patch.object(
            Store, "describe", side_effect=AssertionError("full evidence read")
        ):
            frames = self.dashboard(list(map(ord, "RCq")))
        self.assertIn("No job selected", "\n".join(frames[1].values()))
        self.assertIn("JOBS all | 0/0 loaded", "\n".join(frames[-1].values()))


if __name__ == "__main__":
    unittest.main()
