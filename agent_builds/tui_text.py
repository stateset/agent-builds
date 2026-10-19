"""Bounded, cached text pages for read-only dashboard reports."""

from bisect import bisect_right

from .tui_logs import clean_lines, wrap_line


class ReportView:
    limit = 64 * 1024
    clip_notice = "Report clipped at 64 Ki characters; inspect the full CLI report."

    def __init__(self):
        self.key = self.report = self.width = self.error = None
        self.logical, self.wrapped = [], []
        self.offset, self.height = 0, 1
        self.anchor = None
        self.locations = []
        self._last_offset = None
        self._end_requested = False
        self.query = ""
        self.matches = None
        self.paragraphs, self.visible = [], []
        self._selection_dirty = True

    def page(self, repository, report, width, height, error=None):
        height = max(1, height)
        if repository != self.key:
            self.key, self.offset = repository, 0
            self.report, self.width, self.error = None, None, None
            self.logical, self.wrapped, self.locations = [], [], []
            self.anchor = self._last_offset = None
            self._end_requested = False
            self.query = ""
            self.matches = None
            self.paragraphs, self.visible = [], []
            self._selection_dirty = True
        if (
            self.locations
            and not self._end_requested
            and (self.anchor is None or self.offset != self._last_offset)
        ):
            # Explicit navigation replaces a retained anchor, including when a
            # taller pane temporarily clamped the visible row before navigation.
            row = min(max(0, self.offset), max(0, len(self.wrapped) - self.height))
            self.anchor = self.locations[row]
        changed = self.height != height
        self.height = height
        if report is not self.report or error != self.error:
            changed = True
            self.report, self.width, self.error = report, None, error
            content = (
                self.lines(repository, report)
                if report is not None
                else ([error] if error else [])
            )
            self.logical = self.bounded(content)
            self.paragraphs = [
                (line_index, paragraph_index, paragraph)
                for line_index, line in enumerate(self.logical)
                for paragraph_index, paragraph in enumerate(
                    clean_lines(line.encode("utf-8", errors="replace")) or [""]
                )
            ]
            self._selection_dirty = True
        if self._selection_dirty:
            self._select_paragraphs()
            self.width = None
            self._selection_dirty = False
        if not self.visible:
            self.wrapped, self.locations = [], []
            return []
        width = max(1, width)
        if width != self.width:
            changed = True
            self.wrapped, self.locations = [], []
            for line_index, paragraph_index, paragraph in self.visible:
                offset = 0
                for part in wrap_line(paragraph, width):
                    self.locations.append((line_index, paragraph_index, offset))
                    self.wrapped.append(part)
                    offset += len(part)
            self.width = width
        if self._end_requested:
            self.offset = max(0, len(self.wrapped) - self.height)
            self.anchor = self.locations[self.offset]
            self._end_requested = False
        elif changed and self.anchor is not None:
            self.offset = max(0, bisect_right(self.locations, self.anchor) - 1)
        self.offset = min(max(0, self.offset), max(0, len(self.wrapped) - self.height))
        if self.anchor is None:
            self.anchor = self.locations[self.offset]
        self._last_offset = self.offset
        return self.wrapped[self.offset : self.offset + self.height]

    def display_page(self, repository, report, width, height, error=None):
        """Add display-only empty states without making them report evidence."""
        rows = self.page(repository, report, width, height, error)
        if rows:
            return rows
        if report is None:
            message = ["Report unavailable" if error else "Loading report"]
        elif self.query:
            message = ["No matching lines", "/: blank clears"]
        else:
            message = ["Empty report"]
        return [part for line in message for part in wrap_line(line, max(1, width))][
            : max(1, height)
        ]

    def _select_paragraphs(self):
        if self.report is None:
            self.visible = self.paragraphs
            self.matches = None
        else:
            needle = self.query.casefold()
            self.visible = [
                row for row in self.paragraphs if needle in row[2].casefold()
            ]
            self.matches = len(self.visible)

    def search(self, query):
        query = query[:256]
        if query == self.query:
            return
        self.query = query
        self.matches = None
        self.offset = 0
        self.anchor = self._last_offset = None
        self._end_requested = False
        self.wrapped, self.locations = [], []
        self.width = None
        self._selection_dirty = True

    def search_status(self):
        if not self.query:
            return ""
        label = (
            str(self.matches)
            if self.matches is not None
            else "unavailable"
            if self.error
            else "pending"
        )
        return "Find {}: {}".format(label, self.query)

    def status_label(self, status):
        if not self.query:
            return status
        stale = self.report is not None and (
            self.error or status.casefold().startswith("stale ")
        )
        return ("STALE | " if stale else "") + self.search_status() + " | " + status

    def bounded(self, content):
        if self.limit is None:
            return list(content)
        remaining, lines = self.limit, []
        for line in content:
            line = str(line)
            if len(line) + 1 > remaining:
                lines.extend((line[:remaining], self.clip_notice))
                break
            lines.append(line)
            remaining -= len(line) + 1
        return lines

    def lines(self, scope, report):
        raise NotImplementedError

    def position(self):
        total = len(self.wrapped)
        return (
            "{}-{}/{}".format(
                self.offset + 1, min(total, self.offset + self.height), total
            )
            if total
            else "0/0"
        )

    def scroll(self, amount, pages=False):
        if self.wrapped:
            self.anchor = None
            self._end_requested = False
            self.offset += amount * (self.height if pages else 1)

    def home(self):
        self.offset = 0
        self.anchor = None
        self._end_requested = False

    def end(self):
        self.offset = len(self.wrapped)
        self.anchor = None
        self._end_requested = True
