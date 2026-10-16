"""Log inspection remains bounded and never controls a job or terminal."""

from pathlib import Path
import os
import tempfile
import unittest
from unittest import mock

from agent_builds.tui_logs import MAX_BYTES, LogView, clean_lines, wrap_line


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


if __name__ == "__main__":
    unittest.main()
