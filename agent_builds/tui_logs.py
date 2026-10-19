"""Bounded, read-only log snapshots with explicit follow/hold navigation."""

from bisect import bisect_left, bisect_right
import os
from pathlib import Path
from stat import S_ISREG
import unicodedata

MAX_BYTES = 256 * 1024
TAIL_BYTES = 64 * 1024
TAIL_RECHECK_SECONDS = 5


class NonRegularLog(OSError):
    pass


def signature(stat):
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def read_recent(path, limit):
    """Never block on a FIFO or let a growing file turn a tail into a full read."""
    descriptor = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK)
    try:
        # Keep ownership of the descriptor even if fdopen fails before a
        # context manager exists, for example when the path is a directory.
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            stat = os.fstat(handle.fileno())
            if not S_ISREG(stat.st_mode):
                raise NonRegularLog()
            start = max(0, stat.st_size - limit)
            handle.seek(start)
            return handle.read(limit), signature(stat), bool(start)
    finally:
        os.close(descriptor)


class TailCache:
    """UI-owned inline output, with a synchronous convenience reader."""

    def __init__(self):
        self.key = None
        self.select(None, None)

    def select(self, job, path):
        key = (job, str(path) if path is not None else None)
        if key == self.key:
            return
        self.key = key
        self.path = Path(path) if path is not None else None
        self.signature = None
        self.last_poll = float("-inf")
        self.last_content_read = float("-inf")
        self.lines = []
        self.error = None
        self.loaded = False
        self.clipped = False

    def apply(self, result, now):
        """Adopt an accepted immutable result without filesystem access."""
        if result.request.key != ("inline",) + self.key:
            return False
        self.last_poll, self.signature, self.error = now, result.signature, result.error
        if result.lines is not None:
            self.lines = list(result.lines)
            self.clipped = result.clipped
            self.loaded = result.error is None
            if not result.error:
                self.last_content_read = now
        return True

    def page(self, lines):
        return self.lines[-lines:] if lines > 0 else []

    def read(self, path, lines, now):
        if lines <= 0:
            return []
        from .tui_log_reads import LogRequest, load_log

        self.select(None, path)
        if now - self.last_poll >= 0.5:
            self.last_poll = now
            self.apply(
                load_log(
                    LogRequest(
                        0,
                        ("inline",) + self.key,
                        self.signature,
                        now - self.last_content_read >= TAIL_RECHECK_SECONDS,
                    )
                ),
                now,
            )
        return self.page(lines)


def strip_escapes(text):
    # One pass also handles unterminated/repeated control strings without regex
    # backtracking. This is a text viewer, not a terminal emulator.
    result, state = [], "text"
    for ch in text:
        if state == "text":
            if ch == "\x1b":
                state = "escape"
            else:
                result.append(ch)
        elif state == "escape":
            state = "csi" if ch == "[" else "string" if ch in "]P_^" else "text"
        elif state == "csi":
            if "@" <= ch <= "~":
                state = "text"
        elif state == "string":
            if ch == "\x07":
                state = "text"
            elif ch == "\x1b":
                state = "string_escape"
        elif state == "string_escape":
            state = (
                "text" if ch == "\\" else "string_escape" if ch == "\x1b" else "string"
            )
    return "".join(result)


def clean_lines(data):
    text = strip_escapes(data.decode("utf-8", errors="replace"))
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [
        "".join(ch for ch in line.rsplit("\r", 1)[-1].expandtabs(4) if ch.isprintable())
        for line in lines
    ]


def wrap_line(line, width):
    """Wrap in one pass, reserving columns for wide Unicode characters."""
    chunks, chunk, columns = [], [], 0
    for ch in line:
        size = (
            0
            if unicodedata.combining(ch)
            else 2
            if unicodedata.east_asian_width(ch) in "WF"
            else 1
        )
        if chunk and columns + size > width:
            chunks.append("".join(chunk))
            chunk, columns = [], 0
        chunk.append(ch)
        columns += size
    chunks.append("".join(chunk))
    return chunks


class LogView:
    def __init__(self):
        self.select(None, None)

    def select(self, job, path):
        key = (job, str(path) if path is not None else None)
        if getattr(self, "key", None) == key:
            return
        self.key = key
        self.path = Path(path) if path is not None else None
        self.follow = True
        self.top = 0
        self.lines = []
        self.wrapped = []
        self.starts = []
        self.query = ""
        self.matches = []
        self.match_set = set()
        self.match_index = None
        self.match_target = None
        self.width = self.height = None
        self.anchor = None
        self.signature = None
        self.last_read = self.last_content_read = float("-inf")
        self.clipped = False
        self.error = None
        self.loaded = False
        self.observed = None

    def empty_notice(self):
        if self.lines:
            return ""
        if self.path is None:
            return "No job selected"
        if self.error:
            return "Log unavailable"
        if self.loaded:
            return "Empty log snapshot"
        return "Loading log" if self.follow else "No captured output"

    def content_due(self, now):
        return now - self.last_content_read >= TAIL_RECHECK_SECONDS

    def poll(self, now):
        if self.path is None or not self.follow or now - self.last_read < 0.5:
            return
        from .tui_log_reads import LogRequest, load_log

        self.last_read = now
        self.apply(
            load_log(
                LogRequest(
                    0,
                    ("focused",) + self.key,
                    self.signature,
                    self.content_due(now),
                )
            ),
            now,
        )

    def apply(self, result, now):
        """Apply a current live snapshot; held/search state remains UI-owned."""
        if not self.follow or result.request.key != ("focused",) + self.key:
            return False
        self.last_read, self.signature, self.error = now, result.signature, result.error
        self.observed = now
        if result.lines is not None:
            self.lines = list(result.lines)
            self.clipped = result.clipped
            self.loaded = result.error is None
            if not result.error:
                self.last_content_read = now
            self._find_matches()
            self.width = None
        return True

    def _find_matches(self):
        needle = self.query.casefold()
        self.matches = [
            index
            for index, line in enumerate(self.lines)
            if needle and needle in line.casefold()
        ]
        self.match_set = set(self.matches)
        self.match_index = self.match_target = None

    def search(self, query, width, height):
        """Search logical lines in the current snapshot, never the full file."""
        self.query = query[:256]
        self._find_matches()
        if self.query:
            self.follow = False
        self.page(width, height)
        if self.matches:
            self._jump(0, height)

    def _jump(self, index, height):
        self.anchor = None
        self.follow = False
        self.match_index = index % len(self.matches)
        self.top = min(
            self._match_row(self.match_index),
            max(0, len(self.wrapped) - max(1, height)),
        )

    def next_match(self, direction, width, height):
        self.page(width, height)
        if not self.matches:
            return
        if self.match_index is not None:
            index = self.match_index + direction
        else:
            line = bisect_right(self.starts, self.top) - 1
            index = (
                bisect_right(self.matches, line)
                if direction > 0
                else bisect_left(self.matches, line) - 1
            )
            if line in self.match_set:
                current = bisect_left(self.matches, line)
                hit = self._match_row(current)
                if (direction > 0 and hit > self.top) or (
                    direction < 0 and hit < self.top
                ):
                    index = current
        self._jump(index, height)

    def match_kind(self, visible_row):
        """Return 2 for the selected match, 1 for another match, else 0."""
        line = bisect_right(self.starts, self.top + visible_row) - 1
        if line not in self.match_set:
            return 0
        return (
            2
            if self.match_index is not None and line == self.matches[self.match_index]
            else 1
        )

    def position(self, height):
        total = len(self.wrapped)
        return "rows {}-{}/{}".format(
            self.top + 1 if total else 0, min(total, self.top + max(1, height)), total
        )

    def search_label(self):
        current = self.match_index + 1 if self.match_index is not None else 0
        return "Find {}/{}: {}".format(current, len(self.matches), self.query)

    def _capture_anchor(self):
        if not self.starts or not self.wrapped:
            return None
        row = min(max(0, self.top), len(self.wrapped) - 1)
        line = bisect_right(self.starts, row) - 1
        return line, sum(len(part) for part in self.wrapped[self.starts[line] : row])

    def _match_row(self, index):
        line = self.matches[index]
        if self.match_target is None or self.match_target[0] != line:
            text = self.lines[line]
            remaining = text.casefold().index(self.query.casefold())
            for offset, character in enumerate(text):
                size = len(character.casefold())
                if remaining < size:
                    break
                remaining -= size
            self.match_target = line, offset
        return self._text_row(*self.match_target)

    def _anchor_row(self):
        return self._text_row(*self.anchor)

    def _text_row(self, line, offset):
        start = self.starts[line]
        end = (
            self.starts[line + 1] if line + 1 < len(self.starts) else len(self.wrapped)
        )
        for row in range(start, end):
            size = len(self.wrapped[row])
            if offset < size or row == end - 1:
                return row
            offset -= size
        return start

    def page(self, width, height):
        width, height = max(1, width), max(1, height)
        changed = (self.width, self.height) != (width, height)
        if (
            changed
            and not self.follow
            and self.match_index is None
            and self.anchor is None
        ):
            self.anchor = self._capture_anchor()
        if self.width != width:
            self.wrapped, self.starts = [], []
            for line in self.lines:
                self.starts.append(len(self.wrapped))
                self.wrapped.extend(wrap_line(line, width))
            self.width = width
        if changed and not self.follow:
            if self.match_index is not None:
                self.top = self._match_row(self.match_index)
            elif self.anchor is not None:
                self.top = self._anchor_row()
        self.height = height
        bottom = max(0, len(self.wrapped) - height)
        self.top = bottom if self.follow else min(max(0, self.top), bottom)
        return self.wrapped[self.top : self.top + height]

    def scroll(self, delta, width, height):
        self.page(width, height)
        self.follow = False
        self.match_index = self.anchor = None
        self.top = min(
            max(0, len(self.wrapped) - max(1, height)), max(0, self.top + delta)
        )

    def home(self):
        self.follow, self.top = False, 0
        self.match_index = self.anchor = None

    def end(self):
        self.follow, self.last_read = True, float("-inf")
        self.last_content_read = float("-inf")
        self.match_index = self.anchor = None

    def toggle(self):
        if self.follow:
            self.follow = False
        else:
            self.end()
