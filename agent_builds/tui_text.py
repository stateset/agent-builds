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

    def page(self, repository, report, width, height, error=None):
        height = max(1, height)
        if repository != self.key:
            self.key, self.offset = repository, 0
            self.report, self.width, self.error = None, None, None
            self.logical, self.wrapped, self.locations = [], [], []
            self.anchor = self._last_offset = None
            self._end_requested = False
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
        if not self.logical:
            self.wrapped, self.locations = [], []
            return []
        width = max(1, width)
        if width != self.width:
            changed = True
            self.wrapped, self.locations = [], []
            for line_index, line in enumerate(self.logical):
                for paragraph_index, paragraph in enumerate(
                    clean_lines(line.encode("utf-8", errors="replace")) or [""]
                ):
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
