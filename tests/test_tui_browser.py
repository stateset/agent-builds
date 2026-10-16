"""Dashboard browsing is local to the view and preserves shared reservations."""

import copy
import curses
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_builds import tui
from agent_builds.store import Store
from agent_builds.tui_browser import filter_rows


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
        original = Store.rows

        def observed(store, *args, **kwargs):
            reads.append(store.root)
            return original(store, *args, **kwargs)

        with mock.patch.object(Store, "rows", observed):
            frames = self.dashboard([-1] * 20 + [ord("q")])
        self.assertEqual(len(frames), 21)
        self.assertEqual(len(reads), 1)

    def test_job_snapshot_refreshes_after_the_interval(self):
        now, reads = [100], []
        original = Store.rows

        def observed(store, *args, **kwargs):
            reads.append(now[0])
            return original(store, *args, **kwargs)

        def key():
            if now[0] == 100:
                now[0] = 102
                return -1
            return ord("q")

        with mock.patch.object(Store, "rows", observed):
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


if __name__ == "__main__":
    unittest.main()
