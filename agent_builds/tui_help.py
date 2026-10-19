"""Searchable local help with layout cached across idle frames."""

from bisect import bisect_right
import textwrap


class HelpView:
    def __init__(self, lines):
        self.lines = tuple(lines)
        self.query = ""
        self.offset, self.room = 0, 1
        self._key, self._wrapped = None, []
        self.matches = len(self.lines)
        self._locations = []
        self.anchor = self._last_offset = None
        self._end_requested = False

    def search(self, query):
        query = query[:256]
        if query != self.query:
            self.query = query
            self.offset = 0
            self.anchor = self._last_offset = None
            self._end_requested = False
            self._key, self._wrapped, self._locations = None, [], []

    def page(self, width, room):
        room = max(1, room)
        if (
            self._locations
            and not self._end_requested
            and (self.anchor is None or self.offset != self._last_offset)
        ):
            row = min(max(0, self.offset), max(0, len(self._wrapped) - self.room))
            self.anchor = self._locations[row]
        changed = room != self.room
        self.room = room
        key = (max(1, width), self.query)
        if key != self._key:
            changed = True
            needle = self.query.casefold()
            selected = [
                (index, line)
                for index, line in enumerate(self.lines)
                if needle in line.casefold()
            ]
            self.matches = len(selected)
            self._wrapped, self._locations = [], []
            for index, line in selected:
                # Match textwrap's default tab expansion/whitespace replacement.
                # Store source character positions, never wrapped-row numbers.
                normalized = line.expandtabs().translate(
                    str.maketrans("\t\n\v\f\r", "     ")
                )
                cursor = 0
                for part in textwrap.wrap(line, key[0]):
                    cursor = normalized.index(part, cursor)
                    self._locations.append((index, cursor))
                    self._wrapped.append(part)
                    cursor += len(part)
            self._key = key
        if not self._wrapped:
            self.offset = 0
            self.anchor = self._last_offset = None
            self._end_requested = False
            return []
        if self._end_requested:
            self.offset = max(0, len(self._wrapped) - self.room)
            self.anchor = self._locations[self.offset]
            self._end_requested = False
        elif changed and self.anchor is not None:
            self.offset = max(0, bisect_right(self._locations, self.anchor) - 1)
        self.offset = min(max(0, self.offset), max(0, len(self._wrapped) - self.room))
        if self.anchor is None:
            self.anchor = self._locations[self.offset]
        self._last_offset = self.offset
        return self._wrapped[self.offset : self.offset + self.room]

    def scroll(self, direction, page=False):
        self.anchor = None
        self._end_requested = False
        self.offset = min(
            max(0, self.offset + direction * (self.room if page else 1)),
            max(0, len(self._wrapped) - self.room),
        )

    def home(self):
        self.offset = 0
        self.anchor = None
        self._end_requested = False

    def end(self):
        self.offset = max(0, len(self._wrapped) - self.room)
        self.anchor = None
        self._end_requested = True

    def position(self):
        if not self._wrapped:
            return "0-0/0"
        return "{}-{}/{}".format(
            self.offset + 1,
            min(len(self._wrapped), self.offset + self.room),
            len(self._wrapped),
        )
