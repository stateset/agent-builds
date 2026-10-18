"""Searchable local help with layout cached across idle frames."""

import textwrap


class HelpView:
    def __init__(self, lines):
        self.lines = tuple(lines)
        self.query = ""
        self.offset, self.room = 0, 1
        self._key, self._wrapped = None, []
        self.matches = len(self.lines)

    def search(self, query):
        query = query[:256]
        if query != self.query:
            self.query = query
            self.offset = 0

    def page(self, width, room):
        self.room = max(1, room)
        key = (max(1, width), self.query)
        if key != self._key:
            needle = self.query.casefold()
            selected = [line for line in self.lines if needle in line.casefold()]
            self.matches = len(selected)
            self._wrapped = [
                part for line in selected for part in textwrap.wrap(line, key[0])
            ]
            self._key = key
        self.offset = min(max(0, self.offset), max(0, len(self._wrapped) - self.room))
        return self._wrapped[self.offset : self.offset + self.room]

    def scroll(self, direction, page=False):
        self.offset = min(
            max(0, self.offset + direction * (self.room if page else 1)),
            max(0, len(self._wrapped) - self.room),
        )

    def home(self):
        self.offset = 0

    def end(self):
        self.offset = max(0, len(self._wrapped) - self.room)

    def position(self):
        if not self._wrapped:
            return "0-0/0"
        return "{}-{}/{}".format(
            self.offset + 1,
            min(len(self._wrapped), self.offset + self.room),
            len(self._wrapped),
        )
