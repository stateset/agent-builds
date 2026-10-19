"""Bounded, read-only context captured when a dashboard prompt opens."""

import curses
import textwrap
import time
import unicodedata

from .tui_logs import clean_lines
from .tui_text import ReportView

MAX_CONTEXT_CHARS = 16 * 1024


def cell_width(char):
    if not char.isprintable():
        return 1  # paint renders control characters as spaces.
    return (
        0
        if unicodedata.combining(char)
        else 2
        if unicodedata.east_asian_width(char) in "WF"
        else 1
    )


class InputBuffer:
    """Codepoint editing with a bounded, terminal-cell-aware display window."""

    def __init__(self, value):
        self.value = value
        self.cursor = len(value)

    def handle(self, key):
        if key == curses.KEY_LEFT:
            self.cursor = max(0, self.cursor - 1)
        elif key == curses.KEY_RIGHT:
            self.cursor = min(len(self.value), self.cursor + 1)
        elif key in (curses.KEY_HOME, "\x01"):
            self.cursor = 0
        elif key in (curses.KEY_END, "\x05"):
            self.cursor = len(self.value)
        elif key in ("\b", "\x7f", curses.KEY_BACKSPACE):
            if self.cursor:
                self.value = self.value[: self.cursor - 1] + self.value[self.cursor :]
                self.cursor -= 1
        elif key == curses.KEY_DC:
            self.value = self.value[: self.cursor] + self.value[self.cursor + 1 :]
        elif key == "\x15":
            self.value, self.cursor = "", 0
        elif isinstance(key, str) and key.isprintable():
            self.value = self.value[: self.cursor] + key + self.value[self.cursor :]
            self.cursor += len(key)

    def window(self, columns):
        columns = max(1, columns)
        # Bound work even for an unusually long run of zero-width marks.
        limit = columns * 4 + 8
        left = columns if self.cursor == len(self.value) else columns // 2
        start, used, count = self.cursor, 0, 0
        while start and count < limit:
            size = cell_width(self.value[start - 1])
            if used + size > left:
                break
            start -= 1
            used += size
            count += 1
        chars, used, caret = [], 0, 0
        for index in range(start, min(len(self.value), start + limit)):
            char = self.value[index]
            size = cell_width(char)
            if used + size > columns:
                break
            chars.append(char if char.isprintable() else " ")
            used += size
            if index < self.cursor:
                caret = used
        return "".join(chars), caret


class _ContextPage(ReportView):
    # PromptContext bounds and sanitizes the captured text before rendering.
    limit = None

    def lines(self, scope, report):
        return report


class PromptContext:
    def __init__(self, text=""):
        clipped = len(text) > MAX_CONTEXT_CHARS
        self.lines = clean_lines(
            text[:MAX_CONTEXT_CHARS].encode("utf-8", errors="replace")
        )
        if clipped:
            self.lines.append("[Context clipped at 16 Ki characters]")
        self._page = _ContextPage()

    @property
    def width(self):
        return self._page.width

    @property
    def wrapped(self):
        return self._page.wrapped

    @property
    def top(self):
        return self._page.offset

    @property
    def room(self):
        return self._page.height

    def __bool__(self):
        return bool(self.lines)

    def page(self, width, room):
        return self._page.page("prompt", self.lines, width, room)

    def scroll(self, direction):
        self._page.scroll(direction, pages=True)
        self._page.offset = min(max(0, self.top), max(0, len(self.wrapped) - self.room))

    def position(self):
        return self._page.position() if self.wrapped else "0-0/0"


def job_context(row):
    return "\n".join(
        [
            "State at prompt open: " + row["state"],
            "Checkout: " + row["cwd"],
            "Command: " + row["command_display"],
            "Job request: {} CPU slots / {} GiB RAM".format(
                row["slots"], row["memory_gb"]
            ),
        ]
    )


def recipient_context(recipients):
    return "\n".join(
        [
            "Only explicitly selected idle recipients receive native agent input.",
            "Pane IDs: "
            + (", ".join(agent["pane_id"] for agent in recipients) or "none"),
        ]
        + [
            "{} | {} | {} | {}".format(
                agent["pane_id"],
                agent.get("agent_status", ""),
                agent.get("agent", ""),
                agent.get("cwd", ""),
            )
            for agent in recipients
        ]
        + ([] if recipients else ["No idle recipients in this preview."])
    )


def edit(screen, title, initial, context, paint):
    buffer = InputBuffer(initial)
    context = PromptContext(context)
    # Periodic reads let ncurses process resizes even without another key.
    screen.timeout(200)
    try:
        while True:
            height, width = screen.getmaxyx()
            question = textwrap.wrap(
                "".join(ch if ch.isprintable() else " " for ch in title),
                max(1, width - 1),
            ) or ["Input"]
            editable = width >= 20 and height >= max(
                8, len(question) + (8 if context else 6)
            )
            try:
                curses.curs_set(1 if editable else 0)
            except curses.error:
                pass  # Some terminals do not support cursor visibility.
            try:
                screen.erase()
                if editable:
                    paint(screen, 0, 0, "BUILDR INPUT", curses.A_BOLD)
                    paint(
                        screen,
                        1,
                        0,
                        "Left/Right Home/End Del"
                        if width >= 40
                        else "Arrows Home/End Del",
                    )
                    for index, line in enumerate(question, 2):
                        paint(screen, index, 0, line)
                    if context:
                        page = context.page(width - 1, height - len(question) - 7)
                        paint(
                            screen,
                            len(question) + 3,
                            0,
                            "Snapshot " + context.position(),
                        )
                        for index, line in enumerate(page, len(question) + 4):
                            paint(screen, index, 0, line)
                        paint(screen, height - 2, 0, "PgUp/PgDn context")
                    visible, caret = buffer.window(max(1, width - 4))
                    paint(screen, height - 3, 0, "> " + visible)
                    paint(
                        screen,
                        height - 1,
                        0,
                        "Enter accept | Esc cancel | Ctrl+U clear"
                        if width >= 41
                        else "Enter OK Esc cancel",
                    )
                    screen.move(height - 3, min(width - 2, 2 + caret))
                else:
                    paint(screen, 0, 0, "Resize to edit")
                    paint(screen, height - 1, 0, "Esc cancels")
                screen.refresh()
                key = screen.get_wch()
            except curses.error as error:
                # Retry an interrupted resize, not a disconnected terminal
                # that would otherwise spin on a permanently failing read.
                if screen.getmaxyx() != (height, width):
                    continue
                if str(error) == "no input":
                    time.sleep(
                        0.05
                    )  # Also bound retries on an immediately failing tty.
                    continue
                raise
            if key == "\x1b":
                return None
            if (
                screen.getmaxyx() != (height, width)
                or not editable
                or key == curses.KEY_RESIZE
            ):
                continue
            if key in ("\n", "\r", curses.KEY_ENTER):
                return buffer.value
            if context and key in (curses.KEY_PPAGE, curses.KEY_NPAGE):
                context.scroll(1 if key == curses.KEY_NPAGE else -1)
                continue
            buffer.handle(key)
    finally:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        screen.timeout(200)
