"""Prompt editing remains explicit and readable across split-pane resizes."""

import curses
import unittest
from unittest import mock

from agent_builds import tui
from agent_builds.tui_prompt import MAX_CONTEXT_CHARS, PromptContext
from agent_builds.tui_logs import wrap_line


class PromptTests(unittest.TestCase):
    def edit(
        self,
        keys,
        dimensions=(10, 60),
        title="Search jobs",
        initial="",
        cursor_error=False,
        context="",
    ):
        size = [dimensions]
        screen, frame, frames = mock.Mock(), {}, []
        screen.getmaxyx.side_effect = lambda: size[0]
        screen.erase.side_effect = frame.clear

        def draw(y, x, text, count, attr):
            height, width = size[0]
            self.assertTrue(0 <= y < height and 0 <= x < width)
            self.assertLessEqual(len(text), width - 1)
            frame[y] = text[:count]

        def move(y, x):
            height, width = size[0]
            self.assertTrue(0 <= y < height and 0 <= x < width)

        actions = iter(keys)

        def read():
            action = next(actions)
            if isinstance(action, tuple):
                size[0], action = action
            if isinstance(action, Exception):
                raise action
            return action

        screen.addnstr.side_effect = draw
        screen.move.side_effect = move
        screen.refresh.side_effect = lambda: frames.append(dict(frame))
        screen.get_wch.side_effect = read
        with mock.patch(
            "agent_builds.tui.curses.curs_set",
            side_effect=curses.error("unsupported") if cursor_error else None,
        ):
            try:
                result = tui.prompt(screen, title, initial, context=context)
            finally:
                screen.timeout.assert_called_with(200)
        return result, frames

    def test_question_help_and_editing_are_visible_before_acceptance(self):
        result, frames = self.edit(
            ["a", "b", curses.KEY_BACKSPACE, "z", "\x15", "y", curses.KEY_ENTER],
            initial="cached",
        )
        self.assertEqual(result, "y")
        first = "\n".join(frames[0].values())
        self.assertIn("BUILDR INPUT", first)
        self.assertIn("Search jobs", first)
        self.assertIn("> cached", first)
        self.assertIn("Enter accept | Esc cancel | Ctrl+U clear", first)

    def test_shrinking_ignores_hidden_input_and_enter_then_restores_edit(self):
        result, frames = self.edit(
            [
                "x",
                ((1, 1), curses.KEY_RESIZE),
                "y",
                "\n",
                curses.KEY_BACKSPACE,
                ((10, 60), curses.KEY_RESIZE),
                "\n",
            ],
            initial="safe",
        )
        self.assertEqual(result, "safex")
        self.assertTrue(any(not frame for frame in frames))
        self.assertIn("> safex", "\n".join(frames[-1].values()))

    def test_escape_cancels_even_when_no_text_can_fit(self):
        result, _ = self.edit(["\x1b"], dimensions=(1, 1), initial="yes")
        self.assertIsNone(result)

    def test_enter_during_resize_cannot_accept_a_previously_visible_question(self):
        result, frames = self.edit(
            [((1, 1), "\n"), ((10, 60), curses.KEY_RESIZE), "\x15", "n", "\n"],
            initial="yes",
        )
        self.assertEqual(result, "n")
        self.assertTrue(any(not frame for frame in frames))

    def test_entire_question_must_fit_before_accepting_a_confirmation(self):
        result, frames = self.edit(
            ["y", "e", "s", "\n", "\x1b"],
            dimensions=(8, 20),
            title="Cancel important job abcdef123456 in repository /checkout/project? type yes",
        )
        self.assertIsNone(result)
        self.assertIn("Resize to edit", frames[0].values())
        self.assertIn("Esc cancels", frames[0].values())

    def test_long_initial_value_is_preserved_with_only_the_tail_displayed(self):
        initial = "/repo/" + "long/" * 60
        result, frames = self.edit(["\n"], dimensions=(8, 20), initial=initial)
        self.assertEqual(result, initial)
        self.assertIn("> " + initial[-17:], frames[0].values())
        self.assertIn("Enter OK Esc cancel", frames[0].values())

    def test_unsupported_cursor_visibility_does_not_break_editing(self):
        result, _ = self.edit(["x", "\n"], cursor_error=True)
        self.assertEqual(result, "x")

    def test_resize_interrupt_retries_without_losing_text(self):
        result, _ = self.edit(
            [((8, 20), curses.error("resized")), "x", "\n"], initial="safe"
        )
        self.assertEqual(result, "safex")

    def test_permanent_terminal_error_propagates_instead_of_spinning(self):
        with self.assertRaises(curses.error):
            self.edit([curses.error("disconnected")])

    def test_idle_read_retries_with_bounded_polling(self):
        with mock.patch("agent_builds.tui.time.sleep") as pause:
            result, _ = self.edit([curses.error("no input"), "x", "\n"])
        self.assertEqual(result, "x")
        pause.assert_called_once_with(0.05)

    def test_context_pages_do_not_change_entered_text(self):
        result, frames = self.edit(
            ["x", curses.KEY_NPAGE, curses.KEY_PPAGE, "\n"],
            context="\n".join("Context row " + str(i) for i in range(12)),
            initial="safe",
        )
        self.assertEqual(result, "safex")
        self.assertIn("Snapshot 1-2/12", frames[0].values())
        self.assertIn("Snapshot 3-4/12", frames[2].values())
        self.assertIn("Snapshot 1-2/12", frames[3].values())
        self.assertIn("PgUp/PgDn context", frames[0].values())

    def test_context_needs_room_and_escape_still_cancels(self):
        result, frames = self.edit(
            ["\n", "\x1b"], dimensions=(8, 20), context="Selected job", initial="yes"
        )
        self.assertIsNone(result)
        self.assertIn("Resize to edit", frames[0].values())

    def test_context_scroll_is_clamped_after_resize_without_losing_input(self):
        result, frames = self.edit(
            [curses.KEY_NPAGE, curses.KEY_NPAGE, ((20, 70), curses.KEY_RESIZE), "\n"],
            context="\n".join("Row " + str(i) for i in range(6)),
            initial="safe",
        )
        self.assertEqual(result, "safe")
        self.assertIn("Snapshot 1-6/6", frames[-1].values())


class PromptContextTests(unittest.TestCase):
    def test_context_is_bounded_cleaned_and_clipping_is_explicit(self):
        context = PromptContext("\x1b[31mred\x1b[0m\n" + "x" * MAX_CONTEXT_CHARS)
        rows = context.page(40, MAX_CONTEXT_CHARS)
        self.assertIn("red", rows)
        self.assertNotIn("\x1b", "\n".join(rows))
        self.assertIn("[Context clipped at 16 Ki characters]", rows)
        self.assertLessEqual(sum(map(len, rows)), MAX_CONTEXT_CHARS + 40)

    def test_wrapping_is_reused_until_width_changes(self):
        context = PromptContext("one two three four")
        with mock.patch("agent_builds.tui_prompt.wrap_line", wraps=wrap_line) as wrap:
            context.page(10, 1)
            wrapped = context.wrapped
            context.scroll(1)
            context.page(10, 2)
            self.assertIs(context.wrapped, wrapped)
            self.assertEqual(wrap.call_count, 1)
            context.page(20, 2)
            self.assertEqual(wrap.call_count, 2)

    def test_empty_context_has_no_page_and_large_scrolls_stay_in_bounds(self):
        context = PromptContext()
        self.assertFalse(context)
        self.assertEqual(context.page(20, 2), [])
        self.assertEqual(context.position(), "0-0/0")
        context = PromptContext("one\ntwo\nthree")
        context.page(20, 2)
        context.scroll(100)
        self.assertEqual(context.page(20, 2), ["two", "three"])
        context.scroll(-100)
        self.assertEqual(context.page(20, 2), ["one", "two"])


if __name__ == "__main__":
    unittest.main()
