"""Small rendered-screen accumulator for curses PTY tests, not a full terminal.

Unsupported controls are ignored. Nothing received is executed or forwarded to
a real terminal; only the screen cells and cursor position are updated.
"""

import codecs
import unicodedata


class TerminalScreen:
    def __init__(self, height, width):
        self.height, self.width = max(1, height), max(1, width)
        self.cells = [self._blank() for _ in range(self.height)]
        self.row = self.column = 0
        self.top, self.bottom = 0, self.height - 1
        self.saved = (0, 0)
        self.wrap = True
        self.pending_wrap = False
        self.state, self.sequence = "text", ""
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def _blank(self):
        return [" "] * self.width

    def resize(self, height, width):
        height, width = max(1, height), max(1, width)
        self.cells = [
            (self.cells[index][:width] if index < self.height else [])
            + [" "] * max(0, width - (self.width if index < self.height else 0))
            for index in range(height)
        ]
        self.height, self.width = height, width
        self.top, self.bottom = 0, height - 1
        self._move(self.row, self.column)
        self.saved = (min(self.saved[0], height - 1), min(self.saved[1], width - 1))

    def text(self):
        return "\n".join("".join(row).rstrip() for row in self.cells)

    def _move(self, row, column):
        self.row = max(0, min(self.height - 1, row))
        self.column = max(0, min(self.width - 1, column))
        self.pending_wrap = False

    def _scroll(self, count):
        size = self.bottom - self.top + 1
        count = max(-size, min(size, count))
        region = self.cells[self.top : self.bottom + 1]
        if count > 0:
            region = region[count:] + [self._blank() for _ in range(count)]
        elif count < 0:
            region = [self._blank() for _ in range(-count)] + region[:count]
        self.cells[self.top : self.bottom + 1] = region

    def _linefeed(self, reverse=False):
        if reverse and self.row == self.top:
            self._scroll(-1)
        elif not reverse and self.row == self.bottom:
            self._scroll(1)
        else:
            self.row = max(0, min(self.height - 1, self.row + (-1 if reverse else 1)))
        self.pending_wrap = False

    def _write(self, char):
        if unicodedata.combining(char):
            column = self.column if self.pending_wrap else max(0, self.column - 1)
            while column > 0 and not self.cells[self.row][column]:
                column -= 1
            self.cells[self.row][column] += char
            return
        width = 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        if self.wrap and (self.pending_wrap or self.column + width > self.width):
            self.column = 0
            self._linefeed()
        self.cells[self.row][self.column] = char
        if width == 2 and self.column + 1 < self.width:
            self.cells[self.row][self.column + 1] = ""
        end = self.column + width
        self.column = min(self.width - 1, end)
        self.pending_wrap = self.wrap and end >= self.width

    def _csi(self, command):
        raw = self.sequence
        if raw.startswith("?"):
            if raw == "?7" and command in ("h", "l"):
                self.wrap = command == "h"
                self.pending_wrap = False
            return
        if any(char not in "0123456789;" for char in raw):
            return
        try:
            params = [int(value) if value else 0 for value in raw.split(";")]
        except ValueError:
            return
        count = params[0] or 1
        mode = params[0]
        if command in ("H", "f"):
            self._move(count - 1, ((params[1] if len(params) > 1 else 0) or 1) - 1)
        elif command == "G":
            self._move(self.row, count - 1)
        elif command == "d":
            self._move(count - 1, self.column)
        elif command in ("A", "B", "C", "D", "E", "F"):
            row = self.row + (
                count
                if command in ("B", "E")
                else -count
                if command in ("A", "F")
                else 0
            )
            column = (
                0
                if command in ("E", "F")
                else self.column
                + (count if command == "C" else -count if command == "D" else 0)
            )
            self._move(row, column)
        elif command in ("J", "K"):
            if command == "J" and mode == 2:
                self.cells = [self._blank() for _ in range(self.height)]
            elif mode in (0, 1, 2):
                line = self.cells[self.row]
                start, stop = (
                    (self.column, self.width)
                    if mode == 0
                    else (0, self.column + 1)
                    if mode == 1
                    else (0, self.width)
                )
                line[start:stop] = [" "] * (stop - start)
                if command == "J":
                    rows = (
                        range(self.row + 1, self.height)
                        if mode == 0
                        else range(self.row)
                    )
                    for row in rows:
                        self.cells[row] = self._blank()
        elif command in ("@", "P", "X"):
            count = min(count, self.width - self.column)
            line, column = self.cells[self.row], self.column
            if command == "@":
                line[column:] = [" "] * count + line[column : self.width - count]
            elif command == "P":
                line[column:] = line[column + count :] + [" "] * count
            else:
                line[column : column + count] = [" "] * count
        elif command in ("L", "M") and self.top <= self.row <= self.bottom:
            count = min(count, self.bottom - self.row + 1)
            region = self.cells[self.row : self.bottom + 1]
            blank = [self._blank() for _ in range(count)]
            self.cells[self.row : self.bottom + 1] = (
                blank + region[:-count] if command == "L" else region[count:] + blank
            )
        elif command in ("S", "T"):
            self._scroll(count if command == "S" else -count)
        elif command == "r":
            top = count - 1
            bottom = ((params[1] if len(params) > 1 else 0) or self.height) - 1
            if 0 <= top <= bottom < self.height:
                self.top, self.bottom = top, bottom
                self._move(0, 0)
        elif command == "s":
            self.saved = (self.row, self.column)
        elif command == "u":
            self._move(*self.saved)

    def feed(self, data):
        for char in self.decoder.decode(data):
            if self.state in ("osc", "string"):
                if char == "\x1b":
                    self.state += "_escape"
                elif char == "\x07" and self.state == "osc":
                    self.state = "text"
            elif self.state in ("osc_escape", "string_escape"):
                if char == "\\" or (char == "\x07" and self.state == "osc_escape"):
                    self.state = "text"
                elif char != "\x1b":
                    self.state = self.state.split("_")[0]
            elif self.state == "charset":
                self.state = "text"
            elif self.state == "csi":
                if "@" <= char <= "~":
                    self._csi(char)
                    self.state = "text"
                elif char == "\x1b":
                    self.state = "escape"
                elif len(self.sequence) < 128:
                    self.sequence += char
            elif self.state == "escape":
                self.state = "text"
                if char == "[":
                    self.state, self.sequence = "csi", ""
                elif char in ("]", "P", "^", "_"):
                    self.state = "osc" if char == "]" else "string"
                elif char in "()*+-./#%":
                    self.state = "charset"
                elif char == "7":
                    self.saved = (self.row, self.column)
                elif char == "8":
                    self._move(*self.saved)
                elif char in ("D", "M", "E"):
                    self._linefeed(reverse=char == "M")
                    if char == "E":
                        self.column = 0
                elif char == "\x1b":
                    self.state = "escape"
            elif char == "\x1b":
                self.state = "escape"
            elif char == "\r":
                self._move(self.row, 0)
            elif char in ("\n", "\v", "\f"):
                self._linefeed()
            elif char == "\b":
                self._move(self.row, self.column - 1)
            elif char == "\t":
                self._move(self.row, (self.column // 8 + 1) * 8)
            elif char.isprintable():
                self._write(char)
