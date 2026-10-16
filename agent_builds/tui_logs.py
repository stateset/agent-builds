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
    with os.fdopen(descriptor, "rb") as handle:
        stat = os.fstat(handle.fileno())
        if not S_ISREG(stat.st_mode):
            raise NonRegularLog()
        start = max(0, stat.st_size - limit)
        handle.seek(start)
        return handle.read(limit), signature(stat), bool(start)


class TailCache:
    """One selected inline log, polled at most twice a second."""

    def __init__(self):
        self.path, self.signature = None, None
        self.last_poll = float("-inf")
        self.last_content_read = float("-inf")
        self.lines = []

    def read(self, path, lines, now):
        if lines <= 0:
            return []
        path = Path(path)
        if path != self.path:
            self.path, self.signature, self.lines = path, None, []
            self.last_poll = float("-inf")
            self.last_content_read = float("-inf")
        if now - self.last_poll >= 0.5:
            self.last_poll = now
            try:
                current = signature(path.stat())
                if (
                    current != self.signature
                    or now - self.last_content_read >= TAIL_RECHECK_SECONDS
                ):
                    data, self.signature, _ = read_recent(path, TAIL_BYTES)
                    self.last_content_read = now
                    # Preserve inline tail line semantics; ANSI cleaning is
                    # done only for visible rows when painted.
                    self.lines = data.decode("utf-8", errors="replace").splitlines()
            except OSError:
                self.signature, self.lines = None, []
        return self.lines[-lines:]


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
        self.width = None
        self.signature = None
        self.last_read = float("-inf")
        self.clipped = False
        self.error = None

    def poll(self, now):
        if self.path is None or not self.follow or now - self.last_read < 0.5:
            return
        self.last_read = now
        try:
            stat = self.path.stat()
            current = signature(stat)
            if current == self.signature:
                return
            data, self.signature, clipped = read_recent(self.path, MAX_BYTES)
            if clipped and b"\n" in data:
                data = data.split(b"\n", 1)[1]
            self.lines = clean_lines(data)
            self.clipped, self.error = clipped, None
        except OSError as error:
            self.lines = []
            self.signature = None
            self.clipped = False
            self.error = (
                "Log is not a regular file"
                if isinstance(error, NonRegularLog)
                else (
                    "Log not available yet"
                    if isinstance(error, FileNotFoundError)
                    else "Cannot read log (" + type(error).__name__ + ")"
                )
            )
        self._find_matches()
        self.width = None

    def _find_matches(self):
        needle = self.query.casefold()
        self.matches = [
            index
            for index, line in enumerate(self.lines)
            if needle and needle in line.casefold()
        ]
        self.match_set = set(self.matches)
        self.match_index = None

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
        self.follow = False
        self.match_index = index % len(self.matches)
        self.top = min(
            self.starts[self.matches[self.match_index]],
            max(0, len(self.wrapped) - max(1, height)),
        )

    def next_match(self, direction, width, height):
        self.page(width, height)
        if not self.matches:
            return
        if self.match_index is not None:
            index = self.match_index + direction
        else:
            positions = [self.starts[line] for line in self.matches]
            index = (
                bisect_right(positions, self.top)
                if direction > 0
                else bisect_left(positions, self.top) - 1
            )
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

    def page(self, width, height):
        width, height = max(1, width), max(1, height)
        if self.width != width:
            self.wrapped, self.starts = [], []
            for line in self.lines:
                self.starts.append(len(self.wrapped))
                self.wrapped.extend(wrap_line(line, width))
            self.width = width
            if self.match_index is not None:
                self.top = self.starts[self.matches[self.match_index]]
        bottom = max(0, len(self.wrapped) - height)
        self.top = bottom if self.follow else min(max(0, self.top), bottom)
        return self.wrapped[self.top : self.top + height]

    def scroll(self, delta, width, height):
        self.page(width, height)
        self.follow = False
        self.match_index = None
        self.top = min(
            max(0, len(self.wrapped) - max(1, height)), max(0, self.top + delta)
        )

    def home(self):
        self.follow, self.top = False, 0
        self.match_index = None

    def end(self):
        self.follow, self.last_read = True, float("-inf")
        self.match_index = None

    def toggle(self):
        if self.follow:
            self.follow = False
        else:
            self.end()
