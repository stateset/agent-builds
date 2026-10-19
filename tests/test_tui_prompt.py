"""Prompt editing remains explicit and readable across split-pane resizes."""

import curses
import os
import unittest
import unicodedata
from unittest import mock

from agent_builds import tui
from agent_builds.tui_prompt import MAX_CONTEXT_CHARS, PromptContext, InputBuffer
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
        self.cursor_positions = []
        screen, frame, frames = mock.Mock(), {}, []
        screen.getmaxyx.side_effect = lambda: size[0]
        screen.erase.side_effect = frame.clear

        def draw(y, x, text, count, attr):
            height, width = size[0]
            self.assertTrue(0 <= y < height and 0 <= x < width)
            self.assertLessEqual(len(text), width - 1)
            self.assertLessEqual(
                sum(
                    0
                    if unicodedata.combining(c)
                    else 2
                    if unicodedata.east_asian_width(c) in "WF"
                    else 1
                    for c in text
                ),
                width - 1,
            )
            frame[y] = text[:count]

        def move(y, x):
            height, width = size[0]
            self.assertTrue(0 <= y < height and 0 <= x < width)
            self.cursor_positions.append((y, x))

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

    def test_middle_insert_delete_home_end_and_backspace(self):
        result, frames = self.edit(
            [
                curses.KEY_HOME,
                curses.KEY_RIGHT,
                "X",
                curses.KEY_DC,
                curses.KEY_END,
                curses.KEY_BACKSPACE,
                "\n",
            ],
            initial="abc",
        )
        self.assertEqual(result, "aX")
        self.assertIn("Left/Right Home/End Del", frames[0].values())

    def test_hidden_navigation_and_delete_cannot_edit_during_resize(self):
        result, frames = self.edit(
            [
                curses.KEY_LEFT,
                ((1, 1), curses.KEY_RESIZE),
                curses.KEY_HOME,
                curses.KEY_DC,
                "\n",
                ((8, 20), curses.KEY_RESIZE),
                "X",
                "\n",
            ],
            initial="abc",
        )
        self.assertEqual(result, "abXc")
        self.assertIn("Arrows Home/End Del", frames[-1].values())

    def test_wide_input_fits_cells_and_cursor_survives_resize(self):
        initial = "prefix/" + "界e\u0301" * 50
        result, frames = self.edit(
            [curses.KEY_LEFT, ((8, 20), curses.KEY_RESIZE), "X", "\n"],
            initial=initial,
        )
        self.assertEqual(result, initial[:-1] + "X" + initial[-1:])
        self.assertTrue(all(x <= 18 for y, x in self.cursor_positions[-2:]))
        self.assertIn("界", frames[-1][5])

    def test_cursor_navigation_and_edits_still_require_explicit_acceptance(self):
        result, _ = self.edit(
            [curses.KEY_HOME, "X", curses.KEY_DC, "\x1b"], initial="yes"
        )
        self.assertIsNone(result)

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
        self.assertIn("> " + initial[-16:], frames[0].values())
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

    def test_acceptance_hint_fits_at_the_full_hint_width_boundary(self):
        for width, expected in (
            (40, "Enter OK Esc cancel"),
            (41, "Enter accept | Esc cancel | Ctrl+U clear"),
        ):
            with self.subTest(width=width):
                result, frames = self.edit(["\x1b"], dimensions=(12, width))
                self.assertIsNone(result)
                self.assertEqual(frames[0][11], expected)

    def test_context_resize_retains_reviewed_text_and_typed_response(self):
        context = (
            "prefix " * 100
            + "\n"
            + "\n".join("TARGET-{:02d}".format(i) for i in range(80))
        )
        result, frames = self.edit(
            [
                curses.KEY_NPAGE,
                ((12, 40), curses.KEY_RESIZE),
                ((100, 100), curses.KEY_RESIZE),
                ((24, 100), curses.KEY_RESIZE),
                "x",
                "\n",
            ],
            dimensions=(24, 100),
            context=context,
            initial="safe",
        )

        def first_target(frame):
            return next(
                (
                    line
                    for y, line in sorted(frame.items())
                    if line.startswith("TARGET-")
                ),
                None,
            )

        target = first_target(frames[1])
        self.assertIsNotNone(target)
        self.assertEqual(first_target(frames[2]), target)
        self.assertEqual(first_target(frames[4]), target)
        self.assertEqual(result, "safex")

    def test_context_scroll_is_clamped_after_resize_without_losing_input(self):
        result, frames = self.edit(
            [curses.KEY_NPAGE, curses.KEY_NPAGE, ((20, 70), curses.KEY_RESIZE), "\n"],
            context="\n".join("Row " + str(i) for i in range(6)),
            initial="safe",
        )
        self.assertEqual(result, "safe")
        self.assertIn("Snapshot 1-6/6", frames[-1].values())


@unittest.skipUnless(os.name == "posix", "requires a private pseudo-terminal")
class PromptTerminalTests(unittest.TestCase):
    def test_native_cursor_edit_accept_and_cancel_in_full_and_minimum_panes(self):
        import errno
        import fcntl
        import json
        from pathlib import Path
        import pty
        import selectors
        import struct
        import subprocess
        import sys
        import tempfile
        import termios
        import time
        from tui_terminal import TerminalScreen

        code = (
            "import curses,json,sys; from pathlib import Path; "
            "from agent_builds.tui import prompt; "
            "result=curses.wrapper(lambda screen: prompt(screen,'Edit command',sys.argv[2])); "
            "Path(sys.argv[1]).write_text(json.dumps(result))"
        )
        initial = "cargo test " + "界" * 20
        expected = "cargo check " + "界" * 20 + " --release"
        for height, width in ((24, 100), (8, 20)):
            for accept in (True, False):
                with self.subTest(
                    size=(height, width), accept=accept
                ), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    output, state = root / "result.json", root / "missing-state"
                    master, slave = pty.openpty()
                    selector = selectors.DefaultSelector()
                    terminal = TerminalScreen(height, width)
                    child = None
                    try:
                        fcntl.ioctl(
                            slave,
                            termios.TIOCSWINSZ,
                            struct.pack("HHHH", height, width, 0, 0),
                        )
                        env = dict(
                            os.environ,
                            TERM="xterm-256color",
                            PYTHONUTF8="1",
                            AGENT_BUILDS_STATE=str(state),
                        )
                        child = subprocess.Popen(
                            [sys.executable, "-c", code, str(output), initial],
                            cwd=str(Path(__file__).resolve().parent.parent),
                            env=env,
                            stdin=slave,
                            stdout=slave,
                            stderr=slave,
                            start_new_session=True,
                        )
                        os.close(slave)
                        slave = None
                        selector.register(master, selectors.EVENT_READ)

                        def wait_screen(predicate):
                            deadline = time.monotonic() + 20
                            while time.monotonic() < deadline:
                                for unused, events in selector.select(0.05):
                                    try:
                                        data = os.read(master, 65536)
                                    except OSError as error:
                                        if error.errno != errno.EIO:
                                            raise
                                        data = b""
                                    terminal.feed(data)
                                if predicate(terminal.text()):
                                    return
                                if child.poll() is not None:
                                    break
                            self.fail(
                                "Private prompt did not render expected text: "
                                + repr(terminal.text())
                            )

                        wait_screen(
                            lambda text: (
                                "BUILDR INPUT" in text and "Edit command" in text
                            )
                        )
                        self.assertFalse(output.exists())
                        # Real xterm Home, Right, Delete and End sequences.
                        os.write(
                            master,
                            b"\x1bOH"
                            + b"\x1bOC" * 6
                            + b"\x1b[3~" * 4
                            + b"check\x1bOF --release",
                        )
                        wait_screen(lambda text: "--release" in text)
                        self.assertTrue(0 <= terminal.column <= width - 2)
                        self.assertEqual(terminal.row, height - 3)
                        self.assertFalse(output.exists())
                        os.write(master, b"\n" if accept else b"\x1b")
                        self.assertEqual(child.wait(timeout=10), 0)
                        self.assertEqual(
                            json.loads(output.read_text()), expected if accept else None
                        )
                        self.assertFalse(state.exists())
                    finally:
                        if child is not None and child.poll() is None:
                            child.kill()
                            child.wait(timeout=10)
                        selector.close()
                        os.close(master)
                        if slave is not None:
                            os.close(slave)


class InputBufferTests(unittest.TestCase):
    def test_boundaries_shortcuts_and_clear_preserve_exact_text(self):
        buffer = InputBuffer("abc")
        for key in (curses.KEY_RIGHT, curses.KEY_DC):
            buffer.handle(key)
        self.assertEqual((buffer.value, buffer.cursor), ("abc", 3))
        buffer.handle("\x01")
        buffer.handle(curses.KEY_LEFT)
        buffer.handle(curses.KEY_BACKSPACE)
        self.assertEqual((buffer.value, buffer.cursor), ("abc", 0))
        buffer.handle("start ")
        buffer.handle("\x05")
        buffer.handle(" end")
        self.assertEqual(buffer.value, "start abc end")
        buffer.handle("\x15")
        self.assertEqual((buffer.value, buffer.cursor), ("", 0))

    def test_cell_window_tracks_beginning_middle_and_end_without_truncating_value(self):
        original = "prefix/" + "界e\u0301" * 100 + "/tail"
        buffer = InputBuffer(original)
        for cursor in (len(original), 0, 27):
            buffer.cursor = cursor
            for width in (1, 16, 56):
                text, caret = buffer.window(width)
                cells = sum(
                    0
                    if unicodedata.combining(c)
                    else 2
                    if unicodedata.east_asian_width(c) in "WF"
                    else 1
                    for c in text
                )
                self.assertLessEqual(cells, width)
                self.assertTrue(0 <= caret <= width)
                self.assertEqual(buffer.value, original)
                self.assertEqual(buffer.cursor, cursor)
        buffer.cursor = 0
        self.assertTrue(buffer.window(16)[0].startswith("prefix/"))

    def test_display_work_is_bounded_for_long_zero_width_text_and_controls(self):
        original = "e" + "\u0301" * 100000
        buffer = InputBuffer(original)
        text, caret = buffer.window(16)
        self.assertLessEqual(len(text), 72)
        self.assertEqual(buffer.value, original)
        self.assertEqual(caret, 0)
        buffer = InputBuffer("a\tb\nc")
        self.assertEqual(buffer.window(16), ("a b c", 5))
        self.assertEqual(buffer.value, "a\tb\nc")


class PromptContextTests(unittest.TestCase):
    def test_long_unicode_context_retains_character_position_across_resize(self):
        text = "Captured command:\n" + "".join(
            "界{:03d}e\u0301 ".format(i) for i in range(300)
        )
        context = PromptContext(text)
        context.page(39, 2)
        context.scroll(10)
        first = context.page(39, 2)[0]
        context.page(19, 1)
        context.page(99, 1000)
        self.assertEqual(context.top, 0)
        self.assertEqual(context.page(39, 2)[0], first)
        self.assertEqual(context.lines[0], "Captured command:")

    def test_manual_paging_after_height_clamp_replaces_the_old_position(self):
        context = PromptContext("\n".join("row-{:02d}".format(i) for i in range(60)))
        context.page(39, 2)
        context.scroll(15)
        self.assertEqual(context.page(39, 2)[0], "row-30")
        context.page(99, 1000)
        context.scroll(1)
        context.page(99, 1000)
        self.assertEqual(context.page(19, 2)[0], "row-00")
        context.scroll(1000)
        self.assertEqual(context.page(19, 2)[-1], "row-59")

    def test_idle_and_height_only_context_frames_do_not_resanitize_or_rewrap(self):
        context = PromptContext("long captured command " * 100)
        first = context.page(39, 2)
        with mock.patch(
            "agent_builds.tui_text.clean_lines",
            side_effect=AssertionError("resanitized"),
        ), mock.patch(
            "agent_builds.tui_text.wrap_line", side_effect=AssertionError("rewrapped")
        ):
            for unused in range(300):
                self.assertEqual(context.page(39, 2), first)
            context.page(39, 1000)
            self.assertEqual(context.page(39, 2), first)

    def test_context_is_bounded_cleaned_and_clipping_is_explicit(self):
        context = PromptContext(
            "bad\udcffname\n\x1b[31mred\x1b[0m\n" + "x" * MAX_CONTEXT_CHARS
        )
        rows = context.page(40, MAX_CONTEXT_CHARS)
        self.assertIn("red", rows)
        self.assertIn("bad?name", rows)
        self.assertNotIn("\x1b", "\n".join(rows))
        self.assertIn("[Context clipped at 16 Ki characters]", rows)
        self.assertLessEqual(sum(map(len, rows)), MAX_CONTEXT_CHARS + 40)

    def test_wrapping_is_reused_until_width_changes(self):
        context = PromptContext("one two three four")
        with mock.patch("agent_builds.tui_text.wrap_line", wraps=wrap_line) as wrap:
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
