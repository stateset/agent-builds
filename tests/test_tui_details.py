"""Long selected evidence stays readable without repeated formatting or reads."""

import copy
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_builds import tui_details
from agent_builds.store import Store
from agent_builds.tui_details import DetailsView, detail_lines


class DetailsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "state")
        handle = self.store.submit(
            self.temp.name,
            ["echo", "argument-" * 60],
            "local",
            "quick",
            1,
            0.25,
            "details",
            "",
            5,
        )
        self.row = self.store.get(handle)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_long_command_paths_and_reason_are_preserved_across_wrapped_rows(self):
        self.row.update(cwd="/checkout/" + "folder/" * 30, reason="wait " * 60)
        original, view = copy.deepcopy(self.row), DetailsView()
        view.page(self.row, 19, 2)
        self.assertTrue(all(len(line) <= 19 for line in view.wrapped))
        self.assertEqual("".join(view.wrapped), "".join(detail_lines(self.row)))
        self.assertEqual(self.row, original)

    def test_navigation_clamps_and_resize_rewraps_without_losing_evidence(self):
        view = DetailsView()
        first = view.page(self.row, 19, 2)
        view.scroll(1)
        self.assertNotEqual(view.page(self.row, 19, 2), first)
        view.end()
        last = view.page(self.row, 19, 2)
        self.assertTrue(view.position().endswith("/" + str(len(view.wrapped))))
        self.assertEqual(view.offset, len(view.wrapped) - 2)
        view.scroll(500)
        self.assertEqual(view.page(self.row, 19, 2), last)
        view.page(self.row, 99, 200)
        self.assertEqual(view.offset, 0)
        self.assertLessEqual(len(view.wrapped), 200)
        view.scroll(-500)
        view.page(self.row, 99, 2)
        self.assertEqual(view.offset, 0)
        view.end()
        view.page(self.row, 99, 2)
        view.home()
        self.assertEqual(view.page(self.row, 19, 2), first)

    def test_idle_frames_and_height_changes_reuse_formatted_and_wrapped_evidence(self):
        view = DetailsView()
        with mock.patch.object(
            tui_details, "detail_lines", wraps=detail_lines
        ) as formatted:
            with mock.patch.object(
                tui_details, "wrap_line", wraps=tui_details.wrap_line
            ) as wrapped:
                view.page(self.row, 40, 3)
                calls = wrapped.call_count
                for _ in range(100):
                    view.page(self.row, 40, 5)
                self.assertEqual(formatted.call_count, 1)
                self.assertEqual(wrapped.call_count, calls)
                view.page(self.row, 60, 3)
                self.assertEqual(formatted.call_count, 1)
                self.assertGreater(wrapped.call_count, calls)

    def test_refreshed_snapshot_updates_outcome_and_selected_job_resets_scroll(self):
        view = DetailsView()
        view.page(self.row, 40, 3)
        view.end()
        view.page(self.row, 40, 3)
        refreshed = copy.deepcopy(self.row)
        refreshed.update(state="failed", exit_code=7)
        view.page(refreshed, 40, 3)
        self.assertIn("Job exit: 7 | failed", "\n".join(view.wrapped))
        self.assertGreater(view.offset, 0)
        different = dict(refreshed, id="different", command_display="other job")
        view.page(different, 40, 3)
        self.assertEqual(view.offset, 0)
        self.assertNotIn("argument-", "".join(view.wrapped))

    def test_hidden_or_unavailable_details_release_previous_evidence(self):
        view = DetailsView()
        view.page(self.row, 40, 3)
        view.scroll(1)
        view.page(self.row, 40, 3)
        offset = view.offset
        self.assertEqual(view.page(None, 40, 3), [])
        self.assertIsNone(view.row)
        self.assertEqual(view.logical, [])
        self.assertEqual(view.wrapped, [])
        self.assertEqual(view.position(), "0/0")
        view.page(copy.deepcopy(self.row), 40, 3)
        self.assertEqual(view.offset, offset)

    def test_terminal_controls_and_wide_unicode_are_cleaned_before_wrapping(self):
        view = DetailsView()
        self.row["command_display"] = "\x1b[31m界界e\u0301\x1b[0m\nsecond\tvalue"
        view.page(self.row, 4, 3)
        joined = "".join(view.wrapped)
        self.assertIn("界界e\u0301", joined)
        self.assertIn("second", joined)
        self.assertNotIn("\x1b", joined)
        self.assertNotIn("\t", joined)

    def test_unknown_native_exit_is_explicit_and_not_inferred_from_state(self):
        self.row.update(state="lost", exit_code=None)
        lines = detail_lines(self.row)
        self.assertIn("Job exit: not recorded | lost", lines)
        self.assertIn("Checkout: " + self.temp.name, lines)

    def test_oversized_rendering_is_capped_and_labels_the_missing_evidence(self):
        self.row["command_display"] = "x" * (tui_details.MAX_DETAIL_CHARS * 2)
        view = DetailsView()
        view.page(self.row, 19, 2)
        self.assertLess(sum(map(len, view.logical)), tui_details.MAX_DETAIL_CHARS + 100)
        self.assertIn("Details clipped", "".join(view.wrapped))
        self.assertLess(len(view.wrapped), 4000)


if __name__ == "__main__":
    unittest.main()
