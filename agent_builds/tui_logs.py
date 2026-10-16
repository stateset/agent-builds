"""Bounded, read-only log snapshots with explicit follow/hold navigation."""

import os
from pathlib import Path
from stat import S_ISREG
import unicodedata

MAX_BYTES = 256 * 1024


class NonRegularLog(OSError):
    pass


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
            signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
            if signature == self.signature:
                return
            descriptor = os.open(str(self.path), os.O_RDONLY | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as handle:
                stat = os.fstat(handle.fileno())
                if not S_ISREG(stat.st_mode):
                    raise NonRegularLog()
                start = max(0, stat.st_size - MAX_BYTES)
                handle.seek(start)
                data = handle.read(MAX_BYTES)
            if start and b"\n" in data:
                data = data.split(b"\n", 1)[1]
            self.lines = clean_lines(data)
            self.signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
            self.clipped, self.error = bool(start), None
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
        self.width = None

    def page(self, width, height):
        width, height = max(1, width), max(1, height)
        if self.width != width:
            self.wrapped = [
                part for line in self.lines for part in wrap_line(line, width)
            ]
            self.width = width
        bottom = max(0, len(self.wrapped) - height)
        self.top = bottom if self.follow else min(max(0, self.top), bottom)
        return self.wrapped[self.top : self.top + height]

    def scroll(self, delta, width, height):
        self.page(width, height)
        self.follow = False
        self.top = min(
            max(0, len(self.wrapped) - max(1, height)), max(0, self.top + delta)
        )

    def home(self):
        self.follow, self.top = False, 0

    def end(self):
        self.follow, self.last_read = True, float("-inf")

    def toggle(self):
        if self.follow:
            self.follow = False
        else:
            self.end()
