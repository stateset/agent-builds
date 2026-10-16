"""Prompt editing remains explicit and readable across split-pane resizes."""

import curses
import unittest
from unittest import mock

from agent_builds import tui


class PromptTests(unittest.TestCase):
    def edit(
        self,
        keys,
        dimensions=(10, 60),
        title="Search jobs",
        initial="",
        cursor_error=False,
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
                result = tui.prompt(screen, title, initial)
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


if __name__ == "__main__":
    unittest.main()
