"""Bounded, read-only context captured when a dashboard prompt opens."""

import curses
import textwrap
import time

from .tui_logs import clean_lines, wrap_line

MAX_CONTEXT_CHARS = 16 * 1024


class PromptContext:
    def __init__(self, text=""):
        clipped = len(text) > MAX_CONTEXT_CHARS
        self.lines = clean_lines(
            text[:MAX_CONTEXT_CHARS].encode("utf-8", errors="replace")
        )
        if clipped:
            self.lines.append("[Context clipped at 16 Ki characters]")
        self.width, self.wrapped = None, []
        self.top, self.room = 0, 1

    def __bool__(self):
        return bool(self.lines)

    def page(self, width, room):
        width = max(1, width)
        self.room = max(1, room)
        if width != self.width:
            self.wrapped = [
                part for line in self.lines for part in wrap_line(line, width)
            ]
            self.width = width
        self.top = min(max(0, self.top), max(0, len(self.wrapped) - self.room))
        return self.wrapped[self.top : self.top + self.room]

    def scroll(self, direction):
        self.top = min(
            max(0, self.top + direction * self.room),
            max(0, len(self.wrapped) - self.room),
        )

    def position(self):
        if not self.wrapped:
            return "0-0/0"
        return "{}-{}/{}".format(
            self.top + 1,
            min(len(self.wrapped), self.top + self.room),
            len(self.wrapped),
        )


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
    value = initial
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
                    visible = value[-max(1, width - 3) :]
                    paint(screen, height - 3, 0, "> " + visible)
                    paint(
                        screen,
                        height - 1,
                        0,
                        "Enter accept | Esc cancel | Ctrl+U clear"
                        if width >= 40
                        else "Enter OK Esc cancel",
                    )
                    screen.move(height - 3, min(width - 2, 2 + len(visible)))
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
                return value
            if context and key in (curses.KEY_PPAGE, curses.KEY_NPAGE):
                context.scroll(1 if key == curses.KEY_NPAGE else -1)
                continue
            if key in ("\b", "\x7f", curses.KEY_BACKSPACE):
                value = value[:-1]
            elif key == "\x15":
                value = ""
            elif isinstance(key, str) and key.isprintable():
                value += key
    finally:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        screen.timeout(200)
