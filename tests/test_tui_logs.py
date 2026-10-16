"""Log inspection remains bounded and never controls a job or terminal."""

from pathlib import Path
import os
import tempfile
import unittest
from unittest import mock

from agent_builds.tui_logs import (
    MAX_BYTES,
    TAIL_BYTES,
    LogView,
    TailCache,
    clean_lines,
    wrap_line,
    read_recent,
)


class TuiLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "job.log"
        self.view = LogView()
        self.view.select("job", self.path)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, lines):
        self.path.write_text("\n".join(lines) + "\n")

    def test_hold_keeps_output_and_position_stable_while_file_grows(self):
        self.write([str(i) for i in range(20)])
        self.view.poll(0)
        self.assertEqual(self.view.page(20, 3), ["17", "18", "19"])
        self.view.scroll(-3, 20, 3)
        self.assertEqual(self.view.page(20, 3), ["14", "15", "16"])
        self.write([str(i) for i in range(30)])
        with mock.patch.object(
            Path, "stat", side_effect=AssertionError("held view polled")
        ):
            self.view.poll(1)
        self.assertEqual(self.view.page(20, 3), ["14", "15", "16"])
        self.view.end()
        self.view.poll(1)
        self.assertEqual(self.view.page(20, 3), ["27", "28", "29"])

    def test_reads_are_cached_and_rate_limited(self):
        self.write(["first"])
        with mock.patch("agent_builds.tui_logs.os.open", wraps=os.open) as opened:
            self.view.poll(0)
            self.view.poll(0.1)
            self.view.poll(0.6)
            self.assertEqual(opened.call_count, 1)
            self.write(["changed"])
            before = opened.call_count
            self.view.poll(0.7)
            self.assertEqual(opened.call_count, before)
            self.view.poll(1.2)
            self.assertEqual(opened.call_count, before + 1)
        self.assertEqual(self.view.page(40, 5), ["changed"])

    def test_large_log_keeps_only_a_bounded_recent_window(self):
        self.path.write_bytes(
            b"private-old-start\n" + b"x\n" * (MAX_BYTES * 2) + b"newest\n"
        )
        self.view.poll(0)
        self.assertTrue(self.view.clipped)
        self.assertNotIn("private-old-start", self.view.lines)
        self.assertLessEqual(sum(len(line) + 1 for line in self.view.lines), MAX_BYTES)
        self.assertEqual(self.view.page(30, 1), ["newest"])
        self.view.home()
        self.assertEqual(self.view.page(30, 1), ["x"])

    def test_rotation_truncation_and_missing_files_refresh_without_mixing(self):
        self.view.poll(0)
        self.assertIn("not available", self.view.error)
        self.write(["old line"])
        self.view.poll(1)
        self.assertIsNone(self.view.error)
        self.path.replace(self.path.with_suffix(".old"))
        self.write(["replacement"])
        self.view.poll(2)
        self.assertEqual(self.view.page(30, 3), ["replacement"])
        self.write(["short"])
        self.view.poll(3)
        self.assertEqual(self.view.page(30, 3), ["short"])
        self.assertFalse(self.view.clipped)

    def test_switching_job_resets_hold_and_stale_snapshot(self):
        self.write(["old"])
        self.view.poll(0)
        self.view.home()
        other = self.path.with_name("other.log")
        other.write_text("different\n")
        self.view.select("other", other)
        self.assertTrue(self.view.follow)
        self.assertEqual(self.view.page(30, 3), [])
        self.view.poll(1)
        self.assertEqual(self.view.page(30, 3), ["different"])

    def test_scroll_resize_and_toggle_clamp_safely(self):
        self.write(["long line wraps here", "last"])
        self.view.poll(0)
        self.view.scroll(-1000, 5, 2)
        self.assertEqual(self.view.top, 0)
        self.view.scroll(1000, 5, 2)
        self.assertLessEqual(self.view.top, len(self.view.wrapped))
        self.view.page(100, 100)
        self.assertEqual(self.view.top, 0)
        self.assertFalse(self.view.follow)
        self.view.toggle()
        self.assertTrue(self.view.follow)
        self.view.toggle()
        self.assertFalse(self.view.follow)

    def test_terminal_controls_progress_invalid_utf8_and_unicode_wrapping(self):
        data = b"\x1b]0;bad title\x07\x1b[31merror\x1b[0m\nold\rnew\n\xff\tend\n"
        lines = clean_lines(data)
        self.assertEqual(lines[:2], ["error", "new"])
        self.assertNotIn("\x1b", "\n".join(lines))
        self.assertIn("end", lines[2])
        self.assertEqual(wrap_line("ab\u754ccd", 4), ["ab\u754c", "cd"])
        self.assertEqual(wrap_line("e\u0301x", 1), ["e\u0301", "x"])
        self.assertEqual(clean_lines(b"safe\x1bP" * 10000), ["safe"])

    def test_nonregular_log_is_rejected_without_waiting_for_a_writer(self):
        os.mkfifo(str(self.path))
        self.view.poll(0)
        self.assertIn("not a regular file", self.view.error)
        self.assertEqual(self.view.page(20, 4), [])

    def test_search_is_literal_casefolded_and_spans_visual_wraps(self):
        self.write(["prefix ERROR.[x] twice ERROR.[x] suffix", "other", "Straße"])
        self.view.poll(0)
        self.view.search("error.[x]", 8, 2)
        self.assertFalse(self.view.follow)
        self.assertEqual(self.view.matches, [0])
        self.assertEqual(self.view.search_label(), "Find 1/1: error.[x]")
        self.assertEqual(self.view.match_kind(0), 2)
        self.view.search("STRASSE", 8, 2)
        self.assertEqual(self.view.matches, [2])
        self.assertIn("Straße", self.view.page(8, 2))

    def test_match_navigation_wraps_even_when_page_clamps_near_the_end(self):
        self.write(["hit first", *["padding"] * 8, "hit next", "hit last"])
        self.view.poll(0)
        self.view.search("hit", 30, 5)
        self.assertEqual(self.view.match_index, 0)
        self.assertEqual(self.view.match_kind(0), 2)
        self.view.next_match(1, 30, 5)
        top = self.view.top
        self.assertEqual(self.view.match_index, 1)
        self.assertEqual(self.view.match_kind(3), 2)
        self.assertEqual(self.view.match_kind(4), 1)
        self.view.next_match(1, 30, 5)
        self.assertEqual(self.view.top, top)
        self.assertEqual(self.view.match_index, 2)
        self.assertEqual(self.view.match_kind(4), 2)
        self.view.next_match(1, 30, 5)
        self.assertEqual(self.view.match_index, 0)
        self.view.next_match(-1, 30, 5)
        self.assertEqual(self.view.match_index, 2)

    def test_search_holds_snapshot_and_clear_preserves_mode_without_file_reads(self):
        self.write(["first hit", "old"])
        self.view.poll(0)
        self.view.search("hit", 20, 2)
        self.write(["new hit", "hit extra"])
        with mock.patch.object(
            Path, "stat", side_effect=AssertionError("search polled")
        ):
            self.view.poll(1)
            self.view.next_match(1, 20, 2)
            self.view.search("", 20, 2)
        self.assertFalse(self.view.follow)
        self.assertEqual(self.view.matches, [])
        self.assertIn("first hit", self.view.page(20, 2))
        self.view.end()
        self.view.poll(2)
        self.assertTrue(self.view.follow)
        self.assertIn("new hit", self.view.page(20, 2))
        self.view.search("", 20, 2)
        self.assertTrue(self.view.follow)

    def test_missing_match_empty_log_and_long_query_are_safe(self):
        self.path.write_bytes(b"")
        self.view.poll(0)
        self.view.search("x" * 300, 20, 2)
        self.assertEqual(len(self.view.query), 256)
        self.assertEqual(self.view.position(2), "rows 0-0/0")
        self.assertEqual(self.view.matches, [])
        self.view.next_match(1, 20, 2)
        self.view.next_match(-1, 20, 2)
        self.assertEqual(self.view.top, 0)
        self.assertEqual(self.view.match_kind(0), 0)

    def test_resize_keeps_selected_match_and_manual_scroll_restarts_from_position(self):
        self.write(["long wrapped introductory line", "hit one", "middle", "hit two"])
        self.view.poll(0)
        self.view.search("hit", 10, 2)
        self.view.page(40, 2)
        self.assertEqual(self.view.top, 1)
        self.assertEqual(self.view.match_kind(0), 2)
        self.assertEqual(self.view.position(2), "rows 2-3/4")
        self.view.scroll(1, 40, 2)
        self.assertIsNone(self.view.match_index)
        self.view.next_match(1, 40, 2)
        self.assertEqual(self.view.match_index, 1)
        self.view.home()
        self.view.next_match(-1, 40, 2)
        self.assertEqual(self.view.match_index, 1)

    def test_resuming_live_updates_search_and_switching_job_clears_it(self):
        self.write(["hit"])
        self.view.poll(0)
        self.view.search("hit", 20, 2)
        self.write(["gone"])
        self.view.end()
        self.view.poll(1)
        self.assertEqual(self.view.matches, [])
        self.assertIsNone(self.view.match_index)
        self.view.select("different", self.path)
        self.assertEqual(self.view.query, "")
        self.assertEqual(self.view.matches, [])
        self.view.next_match(1, 20, 2)
        self.assertTrue(self.view.follow)

    def test_real_terminal_can_search_jump_and_quit_without_running_a_job(self):
        import fcntl
        import pty
        import selectors
        import struct
        import subprocess
        import sys
        import termios
        import time
        from agent_builds.store import Store

        root = Path(self.temp.name)
        store = Store(root / "state")
        try:
            job = store.submit(
                root, ["true"], "local", "quick", 1, 0.25, "fixture", "", 5
            )
            store.db.execute("UPDATE jobs SET state='failed' WHERE id=?", (job,))
            store.log_path(job).write_text(
                "FIRST needle\n" + "padding\n" * 40 + "LAST needle\n"
            )
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 20, 100, 0, 0))
            env = dict(
                os.environ, AGENT_BUILDS_STATE=str(store.root), TERM="xterm-256color"
            )
            env.pop("AGENT_BUILDS_JOB_ID", None)
            child = None
            selector = selectors.DefaultSelector()
            selector.register(master, selectors.EVENT_READ)
            try:
                child = subprocess.Popen(
                    [sys.executable, "-m", "agent_builds", "tui", "--cwd", str(root)],
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    env=env,
                    start_new_session=True,
                )
                os.close(slave)
                slave = None

                def wait_for(needle):
                    output = b""
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        for unused, events in selector.select(0.1):
                            output += os.read(master, 65536)
                        if needle in output:
                            return
                        if child.poll() is not None:
                            break
                    self.fail(
                        "Missing terminal output: "
                        + repr(needle)
                        + " in "
                        + repr(output[-4000:])
                    )

                wait_for(b"BUILDR")
                os.write(master, b"l")
                wait_for(b"LOG LIVE")
                os.write(master, b"/")
                wait_for(b"Find in loaded log")
                os.write(master, b"needle\n")
                wait_for(b"FIRST needle")
                os.write(master, b"n")
                wait_for(b"LAST needle")
                os.write(master, b"q")
                self.assertEqual(child.wait(timeout=3), 0)
                self.assertIsNone(store.get(job)["started"])
                self.assertFalse(store.config()["paused"])
            finally:
                if child is not None and child.poll() is None:
                    child.terminate()
                    child.wait(timeout=3)
                selector.close()
                os.close(master)
                if slave is not None:
                    os.close(slave)
        finally:
            store.close()


class InlineTailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "job.log"
        self.cache = TailCache()

    def tearDown(self):
        self.temp.cleanup()

    def test_idle_frames_throttle_metadata_and_never_reread_unchanged_content(self):
        self.path.write_text("first\nlast\n")
        stat, stats = Path.stat, []

        def observed(path, *args, **kwargs):
            stats.append(path)
            return stat(path, *args, **kwargs)

        with mock.patch.object(Path, "stat", observed):
            with mock.patch("agent_builds.tui_logs.os.open", wraps=os.open) as opened:
                for _ in range(200):
                    self.assertEqual(self.cache.read(self.path, 1, 0.1), ["last"])
                self.assertEqual(len(stats), 1)
                self.assertEqual(opened.call_count, 1)
                self.assertEqual(self.cache.read(self.path, 2, 0.6), ["first", "last"])
                self.assertEqual(len(stats), 2)
                self.assertEqual(opened.call_count, 1)

    def test_changed_tail_refreshes_at_the_deadline_and_height_needs_no_io(self):
        self.path.write_text("old\n")
        self.assertEqual(self.cache.read(self.path, 1, 0), ["old"])
        self.path.write_text("new\nlast\n")
        self.assertEqual(self.cache.read(self.path, 1, 0.49), ["old"])
        self.assertEqual(self.cache.read(self.path, 1, 0.5), ["last"])
        with mock.patch.object(Path, "stat", side_effect=AssertionError("extra poll")):
            self.assertEqual(self.cache.read(self.path, 2, 0.6), ["new", "last"])

    def test_selection_change_and_missing_path_never_reuse_another_jobs_output(self):
        self.path.write_text("old\n")
        self.cache.read(self.path, 1, 0)
        other = self.root / "other.log"
        self.assertEqual(self.cache.read(other, 1, 0.1), [])
        other.write_text("different\n")
        self.assertEqual(self.cache.read(other, 1, 0.6), ["different"])
        self.assertEqual(self.cache.read(self.path, 1, 0.61), ["old"])

    def test_truncation_replacement_and_preserved_mtime_refresh(self):
        self.path.write_text("first\n")
        self.cache.read(self.path, 1, 0)
        before = self.path.stat()
        self.path.write_text("other\n")
        os.utime(str(self.path), ns=(before.st_atime_ns, before.st_mtime_ns))
        # A filesystem can coalesce ctime within one tick too. Even identical
        # metadata must not hold the old contents indefinitely.
        with mock.patch.object(Path, "stat", return_value=before):
            self.assertEqual(self.cache.read(self.path, 1, 4.9), ["first"])
            self.assertEqual(self.cache.read(self.path, 1, 5), ["other"])
        self.path.rename(self.root / "old.log")
        self.path.write_text("replacement\n")
        self.assertEqual(self.cache.read(self.path, 1, 6), ["replacement"])
        self.path.write_bytes(b"")
        self.assertEqual(self.cache.read(self.path, 1, 7), [])

    def test_large_and_growing_files_use_a_hard_read_bound(self):
        self.path.write_bytes(b"line\n" * TAIL_BYTES + b"end\n")
        from agent_builds import tui_logs

        original = tui_logs.os.fdopen
        limits = []

        def opened(*args, **kwargs):
            handle = original(*args, **kwargs)
            wrapper = mock.MagicMock(wraps=handle)
            wrapper.__enter__.return_value = wrapper
            wrapper.__exit__.side_effect = lambda *unused: handle.close()

            def read(limit):
                limits.append(limit)
                with self.path.open("ab") as writer:
                    writer.write(b"appended\n" * 10000)
                return handle.read(limit)

            wrapper.read.side_effect = read
            return wrapper

        with mock.patch.object(tui_logs.os, "fdopen", opened):
            self.assertEqual(self.cache.read(self.path, 1, 0), ["end"])
        self.assertEqual(limits, [TAIL_BYTES])
        self.assertLessEqual(sum(len(line) for line in self.cache.lines), TAIL_BYTES)

    def test_fifo_and_directory_return_without_waiting_for_a_writer(self):
        os.mkfifo(str(self.path))
        self.assertEqual(self.cache.read(self.path, 1, 0), [])
        self.assertEqual(self.cache.read(self.root, 1, 0.1), [])

    def test_zero_or_negative_height_does_not_read_a_file(self):
        with mock.patch.object(Path, "stat", side_effect=AssertionError("no rows")):
            self.assertEqual(self.cache.read(self.path, 0, 0), [])
            self.assertEqual(self.cache.read(self.path, -2, 0), [])

    def test_snapshot_error_closes_descriptor(self):
        opened, closed = [], []
        original = os.fdopen

        def observed(descriptor, *args, **kwargs):
            opened.append(descriptor)
            handle = original(descriptor, *args, **kwargs)
            proxy = mock.MagicMock(wraps=handle)
            proxy.__enter__.return_value = proxy

            def close(*unused):
                handle.close()
                closed.append(descriptor)

            proxy.__exit__.side_effect = close
            return proxy

        os.mkfifo(str(self.path))
        with mock.patch("agent_builds.tui_logs.os.fdopen", observed):
            self.cache.read(self.path, 1, 0)
        self.assertEqual(opened, closed)
        self.assertEqual(len(closed), 1)


if __name__ == "__main__":
    unittest.main()
