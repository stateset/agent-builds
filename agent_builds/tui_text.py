"""Bounded, cached text pages for read-only dashboard reports."""

from .tui_logs import clean_lines, wrap_line


class ReportView:
    limit = 64 * 1024
    clip_notice = "Report clipped at 64 Ki characters; inspect the full CLI report."

    def __init__(self):
        self.key = self.report = self.width = self.error = None
        self.logical, self.wrapped = [], []
        self.offset, self.height = 0, 1

    def page(self, repository, report, width, height, error=None):
        self.height = max(1, height)
        if repository != self.key:
            self.key, self.offset = repository, 0
            self.report, self.width, self.error = None, None, None
            self.logical, self.wrapped = [], []
        if report is not self.report or error != self.error:
            self.report, self.width, self.error = report, None, error
            content = (
                self.lines(repository, report)
                if report is not None
                else ([error] if error else [])
            )
            self.logical = self.bounded(content)
        if not self.logical:
            self.wrapped = []
            return []
        width = max(1, width)
        if width != self.width:
            self.wrapped = [
                part
                for line in self.logical
                for paragraph in (
                    clean_lines(line.encode("utf-8", errors="replace")) or [""]
                )
                for part in wrap_line(paragraph, width)
            ]
            self.width = width
        self.offset = min(max(0, self.offset), max(0, len(self.wrapped) - self.height))
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
            self.offset += amount * (self.height if pages else 1)

    def home(self):
        self.offset = 0

    def end(self):
        self.offset = len(self.wrapped)
