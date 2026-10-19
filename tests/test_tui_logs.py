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

    def test_identical_periodic_content_rechecks_refresh_without_rewrapping(self):
        self.write(["unchanged log " * 8] * 1000)
        with mock.patch("agent_builds.tui_logs.os.open", wraps=os.open) as opened:
            self.view.poll(0)
            expected = self.view.page(19, 3)
            layout, lines = self.view.wrapped, self.view.lines
            with mock.patch.object(
                self.view,
                "_find_matches",
                side_effect=AssertionError("same text rescanned"),
            ), mock.patch(
                "agent_builds.tui_logs.wrap_line",
                side_effect=AssertionError("same text rewrapped"),
            ):
                for now in (5.5, 6, 10.5):
                    self.view.poll(now)
                    self.assertEqual(self.view.page(19, 3), expected)
            self.assertEqual(opened.call_count, 3)
        self.assertIs(self.view.wrapped, layout)
        self.assertIs(self.view.lines, lines)
        self.assertEqual(self.view.last_content_read, 10.5)
        self.assertEqual(self.view.observed, 10.5)
        self.assertTrue(self.view.loaded)
        self.assertFalse(self.view.content_due(11))

    def test_same_text_updates_signature_clipping_and_freshness_without_file_io(self):
        from agent_builds.tui_log_reads import LogRequest, LogResult

        request = LogRequest(0, ("focused",) + self.view.key)
        self.view.apply(LogResult(request, (1, 2, 3, 4, 5), ("same output",)), 0)
        self.view.page(19, 2)
        layout, matches = self.view.wrapped, self.view.matches
        with mock.patch.object(
            Path, "stat", side_effect=AssertionError("apply IO")
        ), mock.patch.object(Path, "open", side_effect=AssertionError("apply IO")):
            self.assertTrue(
                self.view.apply(
                    LogResult(request, (1, 2, 3, 4, 6), ("same output",), clipped=True),
                    1,
                )
            )
            self.assertEqual(self.view.page(19, 2), ["same output"])
        self.assertIs(self.view.wrapped, layout)
        self.assertIs(self.view.matches, matches)
        self.assertEqual(self.view.signature, (1, 2, 3, 4, 6))
        self.assertTrue(self.view.clipped)
        self.assertEqual(self.view.observed, 1)
        self.assertEqual(self.view.last_content_read, 1)
        self.view.apply(LogResult(request, lines=("same output",), clipped=False), 2)
        self.assertFalse(self.view.clipped)
        self.assertIs(self.view.wrapped, layout)

    def test_changed_content_error_and_empty_recovery_update_evidence_and_notices(self):
        from agent_builds.tui_log_reads import LogRequest, LogResult

        request = LogRequest(0, ("focused",) + self.view.key)
        self.view.apply(LogResult(request, lines=("hit one",)), 0)
        self.view.search("hit", 19, 2)
        self.view.end()
        with mock.patch.object(
            self.view, "_find_matches", wraps=self.view._find_matches
        ) as find:
            self.view.apply(LogResult(request, lines=("different",)), 1)
            self.assertEqual(find.call_count, 1)
        self.assertEqual(self.view.matches, [])
        self.assertEqual(self.view.page(19, 2), ["different"])
        self.view.apply(LogResult(request, lines=(), error="unavailable"), 2)
        self.assertEqual(self.view.page(19, 2), [])
        self.assertEqual(self.view.empty_notice(), "Log unavailable")
        self.assertFalse(self.view.loaded)
        self.assertEqual(self.view.last_content_read, 1)
        with mock.patch(
            "agent_builds.tui_logs.wrap_line",
            side_effect=AssertionError("empty rewrapped"),
        ):
            self.view.apply(LogResult(request, lines=()), 3)
            self.assertEqual(self.view.page(19, 2), [])
        self.assertEqual(self.view.empty_notice(), "Empty log snapshot")
        self.assertTrue(self.view.loaded)
        self.assertIsNone(self.view.error)
        self.assertEqual(self.view.last_content_read, 3)
        self.view.apply(LogResult(request, lines=("hit recovered",)), 4)
        self.assertEqual(self.view.matches, [0])
        self.assertEqual(self.view.page(19, 2), ["hit recovered"])

    def test_resume_same_text_keeps_search_cache_and_next_match_resize_work(self):
        from agent_builds.tui_log_reads import LogRequest, LogResult

        content = tuple(["x" * 30 + "needle target"] + ["padding"] * 20)
        request = LogRequest(0, ("focused",) + self.view.key)
        self.view.apply(LogResult(request, lines=content), 0)
        self.view.search("needle", 19, 3)
        matches = self.view.matches
        self.view.end()
        with mock.patch.object(
            self.view,
            "_find_matches",
            side_effect=AssertionError("same text rescanned"),
        ), mock.patch(
            "agent_builds.tui_logs.wrap_line",
            side_effect=AssertionError("same text rewrapped"),
        ):
            self.view.apply(LogResult(request, lines=content), 1)
            self.assertEqual(self.view.page(19, 3), ["padding"] * 3)
            self.view.next_match(1, 19, 3)
            self.assertIn("needle", "".join(self.view.page(19, 3)))
        self.assertIs(self.view.matches, matches)
        self.assertEqual(self.view.match_target, (0, 30))
        first = self.view.page(19, 3)
        self.view.page(7, 2)
        self.assertEqual(self.view.page(19, 3), first)

    def test_same_text_from_another_job_never_reuses_or_accepts_previous_job_cache(
        self,
    ):
        from agent_builds.tui_log_reads import LogRequest, LogResult

        old_request = LogRequest(0, ("focused",) + self.view.key)
        self.view.apply(LogResult(old_request, lines=("same text",)), 0)
        self.view.page(19, 2)
        old_lines, old_layout = self.view.lines, self.view.wrapped
        self.view.select("another-job", self.path)
        self.assertFalse(
            self.view.apply(LogResult(old_request, lines=("same text",)), 1)
        )
        self.assertFalse(self.view.loaded)
        request = LogRequest(1, ("focused",) + self.view.key)
        self.view.apply(LogResult(request, lines=("same text",)), 2)
        self.assertEqual(self.view.page(19, 2), ["same text"])
        self.assertIsNot(self.view.lines, old_lines)
        self.assertIsNot(self.view.wrapped, old_layout)
        self.assertEqual(self.view.query, "")

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

    def test_focused_content_recheck_is_based_on_content_not_metadata_polls(self):
        self.write(["old"])
        metadata = self.path.stat()
        with mock.patch.object(Path, "stat", return_value=metadata), mock.patch(
            "agent_builds.tui_logs.os.fstat", return_value=metadata
        ), mock.patch("agent_builds.tui_logs.os.open", wraps=os.open) as opened:
            self.view.poll(0)
            self.write(["new"])
            for now in (0.6, 1.2, 2, 4.9):
                self.view.poll(now)
                self.assertEqual(self.view.page(20, 2), ["old"])
            self.assertEqual(opened.call_count, 1)
            self.view.poll(5.5)
            self.assertEqual(self.view.page(20, 2), ["new"])
            self.assertEqual(self.view.last_content_read, 5.5)
            self.view.poll(5.6)
            self.view.poll(9.9)
            self.assertEqual(opened.call_count, 2)
            self.view.poll(10.5)
            self.assertEqual(opened.call_count, 3)

    def test_end_forces_same_metadata_content_while_hold_does_not_read(self):
        self.write(["old"])
        metadata = self.path.stat()
        with mock.patch.object(Path, "stat", return_value=metadata), mock.patch(
            "agent_builds.tui_logs.os.fstat", return_value=metadata
        ), mock.patch("agent_builds.tui_logs.os.open", wraps=os.open) as opened:
            self.view.poll(0)
            self.view.home()
            self.write(["new"])
            self.view.poll(1)
            self.assertEqual(opened.call_count, 1)
            self.assertEqual(self.view.page(20, 2), ["old"])
            self.view.end()
            self.view.poll(1)
            self.assertEqual(opened.call_count, 2)
            self.assertEqual(self.view.page(20, 2), ["new"])

    def test_only_accepted_successful_content_advances_recheck_timestamp(self):
        from agent_builds.tui_log_reads import LogRequest, LogResult

        self.write(["old"])
        self.view.poll(0)
        request = LogRequest(0, ("focused",) + self.view.key)
        self.view.apply(LogResult(request, self.view.signature), 3)
        self.assertEqual(self.view.last_content_read, 0)
        self.view.apply(LogResult(request, lines=(), error="fixture failure"), 4)
        self.assertEqual(self.view.last_content_read, 0)
        self.assertTrue(self.view.content_due(5))
        other = LogRequest(0, ("focused", "other", str(self.path)))
        self.assertFalse(self.view.apply(LogResult(other, lines=("wrong",)), 5))
        self.view.home()
        self.assertFalse(self.view.apply(LogResult(request, lines=("late",)), 5))
        self.assertEqual(self.view.last_content_read, 0)
        self.view.select("other", self.path)
        self.assertTrue(self.view.content_due(5))

    def test_empty_notices_distinguish_missing_uncaptured_and_accepted_content(self):
        self.assertEqual(self.view.empty_notice(), "Loading log")
        self.view.home()
        self.assertEqual(self.view.empty_notice(), "No captured output")
        self.view.end()
        self.view.poll(0)
        self.assertEqual(self.view.empty_notice(), "Log unavailable")
        self.path.write_bytes(b"")
        self.view.poll(1)
        self.assertEqual(self.view.empty_notice(), "Empty log snapshot")
        self.view.search("Empty", 20, 2)
        self.assertEqual(self.view.matches, [])
        self.assertEqual(self.view.page(20, 2), [])
        self.assertEqual(self.view.position(2), "rows 0-0/0")
        self.write(["real output"])
        self.view.poll(2)
        self.assertEqual(self.view.empty_notice(), "Empty log snapshot")
        self.view.end()
        self.view.poll(2)
        self.assertEqual(self.view.empty_notice(), "")
        self.path.write_bytes(b"")
        self.view.poll(3)
        self.assertEqual(self.view.empty_notice(), "Empty log snapshot")
        self.view.select(None, None)
        self.assertEqual(self.view.empty_notice(), "No job selected")

    def test_real_blank_lines_are_output_and_empty_notices_never_read_files(self):
        self.write([""])
        self.view.poll(0)
        self.assertEqual(self.view.page(20, 2), [""])
        self.assertEqual(self.view.position(2), "rows 1-1/1")
        with mock.patch.object(Path, "stat", side_effect=AssertionError("notice IO")):
            for unused in range(300):
                self.assertEqual(self.view.empty_notice(), "")
        self.view.select("other", self.path)
        self.assertEqual(self.view.empty_notice(), "Loading log")

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

    def test_held_resize_retains_logical_line_and_character_across_round_trips(self):
        self.write(["intro", "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"] + ["padding"] * 30)
        self.view.poll(0)
        self.view.home()
        self.view.scroll(3, 10, 2)
        self.assertEqual(self.view.page(10, 2)[0], "KLMNOPQRST")
        with mock.patch.object(Path, "stat", side_effect=AssertionError("resize IO")):
            self.assertEqual(self.view.page(7, 2)[0], "EFGHIJK")
            self.assertEqual(self.view.page(19, 2)[0], "JKLMNOPQRSTUVWXYZ")
            self.assertEqual(self.view.page(10, 2)[0], "KLMNOPQRST")
        self.assertFalse(self.view.follow)

    def test_height_clamping_is_temporary_until_manual_navigation(self):
        self.write([str(i) for i in range(100)])
        self.view.poll(0)
        self.view.home()
        self.view.scroll(90, 10, 3)
        self.assertEqual(self.view.page(10, 20)[0], "80")
        self.assertEqual(self.view.page(10, 3)[0], "90")
        self.view.page(10, 20)
        self.view.scroll(-1, 10, 20)
        self.assertEqual(self.view.page(10, 3)[0], "79")

    def test_manual_scroll_replaces_resize_anchor_and_home_end_reset_it(self):
        self.write(["intro", "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"] + ["padding"] * 30)
        self.view.poll(0)
        self.view.home()
        self.view.scroll(3, 10, 2)
        self.view.page(7, 2)
        self.view.scroll(1, 7, 2)
        self.assertEqual(self.view.page(7, 2)[0], "LMNOPQR")
        self.view.page(19, 2)
        self.assertEqual(self.view.page(7, 2)[0], "LMNOPQR")
        self.view.home()
        self.assertEqual(self.view.page(19, 2)[0], "intro")
        self.view.end()
        self.write(["new"] * 30 + ["LIVE END"])
        self.view.poll(1)
        self.assertEqual(self.view.page(7, 2)[-1], "D")
        self.assertEqual(self.view.page(19, 2)[-1], "LIVE END")
        self.assertTrue(self.view.follow)

    def test_unicode_empty_lines_and_job_switch_do_not_reuse_old_anchor(self):
        self.write(["", "甲乙丙丁戊己庚辛壬癸"] + ["padding"] * 30)
        self.view.poll(0)
        self.view.home()
        self.view.scroll(3, 4, 2)
        self.assertEqual(self.view.page(4, 2)[0], "戊己")
        self.assertEqual(self.view.page(6, 2)[0], "丁戊己")
        self.assertEqual(self.view.page(4, 2)[0], "戊己")
        self.view.home()
        self.assertEqual(self.view.page(6, 2)[0], "")
        self.write(["", "ae\u0301bcde\u0301fghijk"] + ["padding"] * 30)
        self.view.end()
        self.view.poll(1)
        self.view.home()
        self.view.scroll(2, 3, 2)
        self.assertEqual(self.view.page(3, 2)[0], "cde\u0301")
        self.assertEqual(self.view.page(4, 2)[0], "ae\u0301bc")
        self.assertEqual(self.view.page(3, 2)[0], "cde\u0301")
        self.view.select("other", self.path)
        self.assertIsNone(self.view.anchor)
        self.assertEqual(self.view.page(4, 2), [])
        self.view.poll(1)
        self.assertTrue(self.view.follow)

    def test_selected_search_match_wins_and_clear_keeps_current_position(self):
        self.write(
            ["intro", "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ", "hit target"]
            + ["padding"] * 30
        )
        self.view.poll(0)
        self.view.home()
        self.view.scroll(3, 10, 2)
        self.view.page(7, 2)
        self.view.search("hit", 7, 2)
        self.assertEqual(self.view.page(19, 2)[0], "hit target")
        self.view.search("", 19, 2)
        self.assertEqual(self.view.page(7, 2)[0], "hit tar")
        self.assertEqual(self.view.page(19, 2)[0], "hit target")
        self.assertFalse(self.view.follow)

    def test_idle_held_frames_reuse_wrapping_and_anchor_without_scanning(self):
        self.write(["intro", "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"] + ["padding"] * 30)
        self.view.poll(0)
        self.view.home()
        self.view.scroll(3, 10, 2)
        expected = self.view.page(7, 2)
        with mock.patch(
            "agent_builds.tui_logs.wrap_line",
            side_effect=AssertionError("idle wrapping"),
        ), mock.patch.object(
            self.view, "_capture_anchor", side_effect=AssertionError("idle capture")
        ), mock.patch.object(
            self.view, "_anchor_row", side_effect=AssertionError("idle scan")
        ), mock.patch.object(Path, "stat", side_effect=AssertionError("idle IO")):
            for unused in range(300):
                self.assertEqual(self.view.page(7, 2), expected)

    def test_search_reveals_first_hit_deep_inside_a_wrapped_line(self):
        self.write(
            ["intro", "x" * 30 + "needle" + "z" * 30 + "needle"] + ["padding"] * 20
        )
        self.view.poll(0)
        self.view.search("needle", 10, 3)
        self.assertEqual(self.view.page(10, 3)[0], "needlezzzz")
        self.assertEqual(self.view.matches, [1])
        self.assertEqual(self.view.search_label(), "Find 1/1: needle")
        self.assertEqual(self.view.match_kind(0), 2)
        self.view.page(7, 3)
        self.assertIn("ne", self.view.page(7, 3)[0])
        self.assertEqual(self.view.page(10, 3)[0], "needlezzzz")

    def test_casefold_expansion_maps_hit_to_original_text_before_wrapping(self):
        for prefix, query, suffix, expected in (
            ("ß" * 20, "NEEDLE", "needle", "needlezzzz"),
            ("x" * 30, "SS", "ß", "ßzzzzzzzzz"),
            ("x" * 30, "SX", "ßx", "ßxzzzzzzzz"),
            ("甲乙丙丁戊", "NEEDLE", "needle", "needlezzzz"),
            ("ae\u0301bcdefghij", "NEEDLE", "needle", "jneedlezzz"),
        ):
            self.view.select(prefix + query, self.path)
            self.write([prefix + suffix + "z" * 50] + ["padding"] * 20)
            self.view.poll(0)
            self.view.search(query, 10, 3)
            self.assertEqual(self.view.page(10, 3)[0], expected)

    def test_next_previous_after_scrolling_use_hit_position_within_current_line(self):
        self.write(
            ["hit earlier", "x" * 30 + "hit middle" + "z" * 30, "hit later"]
            + ["padding"] * 20
        )
        self.view.poll(0)
        self.view.search("hit", 10, 3)
        self.view.scroll(2, 10, 3)
        self.assertEqual(self.view.page(10, 3)[0], "xxxxxxxxxx")
        self.view.next_match(1, 10, 3)
        self.assertEqual(self.view.match_index, 1)
        self.assertEqual(self.view.page(10, 3)[0], "hit middle")
        self.view.scroll(1, 10, 3)
        self.view.next_match(-1, 10, 3)
        self.assertEqual(self.view.match_index, 1)
        self.assertEqual(self.view.page(10, 3)[0], "hit middle")
        self.view.next_match(1, 10, 3)
        self.assertEqual(self.view.match_index, 2)
        self.view.next_match(1, 10, 3)
        self.assertEqual(self.view.match_index, 0)

    def test_hit_cache_resets_on_query_content_and_job_changes(self):
        self.write(["x" * 20 + "first" + "y" * 25 + "second"] + ["padding"] * 20)
        self.view.poll(0)
        self.view.search("first", 10, 3)
        self.assertEqual(self.view.match_target, (0, 20))
        self.view.search("second", 10, 3)
        self.assertEqual(self.view.match_target, (0, 50))
        self.view.end()
        self.write(["second moved"] + ["padding"] * 20)
        self.view.poll(1)
        self.assertIsNone(self.view.match_target)
        self.view.next_match(1, 10, 3)
        self.assertEqual(self.view.match_target, (0, 0))
        self.view.select("another", self.path)
        self.assertIsNone(self.view.match_target)

    def test_idle_selected_hits_do_not_rewrap_remap_or_read_files(self):
        self.write(["x" * 100 + "hit target"] + ["padding"] * 20)
        self.view.poll(0)
        self.view.search("hit", 10, 3)
        expected = self.view.page(10, 3)
        with mock.patch.object(
            self.view, "_match_row", side_effect=AssertionError("idle hit scan")
        ), mock.patch(
            "agent_builds.tui_logs.wrap_line", side_effect=AssertionError("idle wrap")
        ), mock.patch.object(Path, "stat", side_effect=AssertionError("idle IO")):
            for unused in range(300):
                self.assertEqual(self.view.page(10, 3), expected)

    def test_real_terminal_can_search_jump_and_quit_without_running_a_job(self):
        import fcntl
        import pty
        import selectors
        import signal
        import struct
        import subprocess
        import sys
        import termios
        import time
        from agent_builds.store import Store
        from tui_terminal import TerminalScreen

        root = Path(self.temp.name)
        store = Store(root / "state")
        try:
            job = store.submit(
                root, ["true"], "local", "quick", 1, 0.25, "fixture", "", 5
            )
            log_contents = (
                "FIRST needle\n"
                + "x" * 99
                + "HELD-ANCHOR"
                + "y" * 100
                + "\n"
                + "padding\n" * 40
                + "LAST needle\n"
            )
            store.log_path(job).write_bytes(b"")
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 20, 100, 0, 0))
            env = dict(
                os.environ, AGENT_BUILDS_STATE=str(store.root), TERM="xterm-256color"
            )
            env.pop("AGENT_BUILDS_JOB_ID", None)
            child = None
            selector = selectors.DefaultSelector()
            selector.register(master, selectors.EVENT_READ)
            terminal = TerminalScreen(20, 100)

            def resize(height, width):
                terminal.resize(height, width)
                fcntl.ioctl(
                    master, termios.TIOCSWINSZ, struct.pack("HHHH", height, width, 0, 0)
                )
                os.kill(child.pid, signal.SIGWINCH)

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

                def wait_for(needle, row=None):
                    output = b""
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        for unused, events in selector.select(0.1):
                            chunk = os.read(master, 65536)
                            output += chunk
                            terminal.feed(chunk)
                        rendered = (
                            terminal.text()
                            if row is None
                            else "".join(terminal.cells[row])
                        )
                        if needle.decode("utf-8") in rendered:
                            return
                        if child.poll() is not None:
                            break
                    self.fail(
                        "Missing terminal output: "
                        + repr(needle)
                        + " in "
                        + repr(terminal.text())
                        + " raw="
                        + repr(output[-2000:])
                    )

                wait_for(b"BUILDR")
                os.write(master, b"V")
                wait_for(b"BUILDR VIEWS")
                os.write(master, b"history\n")
                wait_for(b"No completed jobs")
                os.write(master, b"V")
                wait_for(b"BUILDR VIEWS")
                os.write(master, b"\x1b")
                wait_for(b"HISTORY")
                os.write(master, b"H")
                wait_for(b"JOBS")
                os.write(master, b"w")
                wait_for(b"Runner reason:")
                os.write(master, b"w")
                wait_for(b"JOBS")
                os.write(master, b"c")
                wait_for(b"Checkout:")
                os.write(master, b"\x1b")
                wait_for(b"JOBS")
                os.write(master, b"F")
                wait_for(b"Following first matching row")
                os.write(master, b"F")
                wait_for(b"Selection pinned")
                os.write(master, b"R")
                wait_for(b"Repository focus:")
                os.write(master, b"C")
                wait_for(b"Browsing filters cleared")
                os.write(master, b"i")
                wait_for(b"JOB DETAILS |")
                resize(8, 40)
                wait_for(b"DETAILS 1-")
                os.write(master, b"w")
                wait_for(b"WHY " + job.encode())
                os.write(master, b"i")
                wait_for(b"DETAILS 1-")
                os.write(master, b"V")
                wait_for(b"BUILDR VIEWS")
                os.write(master, b"processes\n")
                wait_for(b"Resize to 65x15")
                os.write(master, b"\x1b")
                wait_for(b"DETAILS 1-")
                os.write(master, b"i")
                wait_for(b"JOBS")
                resize(20, 100)
                os.write(master, b"H")
                wait_for(b"No completed jobs")
                resize(8, 40)
                wait_for(b"HISTORY")
                os.write(master, b"H")
                wait_for(b"JOBS")
                resize(20, 100)
                os.write(master, b"l")
                wait_for(b"LOG LIVE")
                wait_for(b"Empty log snapshot", row=3)
                resize(8, 20)
                wait_for(b"Empty log snapshot", row=3)
                store.log_path(job).write_text(log_contents)
                wait_for(b"LAST needle")
                self.assertNotIn("Empty log snapshot", terminal.text())
                resize(20, 100)
                os.write(master, b"/")
                wait_for(b"Find in loaded log")
                resize(8, 18)
                wait_for(b"Resize to edit")
                os.write(master, b"\x1b")
                wait_for(b"LOG LIVE")
                resize(20, 100)
                wait_for(b"l jobs  ? help  q quit")
                wait_for(b"LAST needle")
                os.write(master, b"/")
                wait_for(b"Find in loaded log")
                os.write(master, b"needle\n")
                wait_for(b"FIRST needle")
                os.write(master, b"n")
                wait_for(b"LAST needle")
                os.write(master, b"/")
                wait_for(b"Find in loaded log")
                os.write(master, b"\x15HELD-ANCHOR\n")
                wait_for(b"Find 1/1: HELD-ANCHOR")
                wait_for(b"HELD-ANCHOR" + b"y" * 88, row=3)
                resize(8, 20)
                wait_for(b"xxxxHELD-ANCHORyyyy", row=3)
                resize(20, 100)
                wait_for(b"HELD-ANCHOR" + b"y" * 88, row=3)
                os.write(master, b"/")
                wait_for(b"Find in loaded log")
                os.write(master, b"\x15\n")
                wait_for(b"Space hold/resume")
                wait_for(b"HELD-ANCHOR" + b"y" * 88, row=3)
                resize(8, 20)
                wait_for(b"xxxxHELD-ANCHORyyyy", row=3)
                resize(8, 40)
                wait_for(b"x" * 21 + b"HELD-ANCHOR" + b"y" * 7, row=3)
                resize(20, 100)
                wait_for(b"HELD-ANCHOR" + b"y" * 88, row=3)
                os.write(master, b"q")
                self.assertEqual(child.wait(timeout=3), 0)
                self.assertIsNone(store.get(job)["started"])
                self.assertFalse(store.get(job)["cancel_requested"])
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
            self.assertEqual(self.cache.read(self.path, 1, 5.5), ["other"])
        self.path.rename(self.root / "old.log")
        self.path.write_text("replacement\n")
        self.assertEqual(self.cache.read(self.path, 1, 6.5), ["replacement"])
        self.path.write_bytes(b"")
        self.assertEqual(self.cache.read(self.path, 1, 7.5), [])

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
