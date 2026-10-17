"""Bounded, asynchronous, read-only history for the dashboard."""

from .tui_logs import clean_lines, wrap_line

MAX_CHARS = 64 * 1024


def inspect_history(root, repository):
    from .store import Store
    from .telemetry import statistics

    reader = Store(root, read_only=True)
    try:
        if reader.db is not None:
            reader.db.execute("PRAGMA busy_timeout=1000")
        with reader.read_snapshot():
            return statistics(reader, repository)
    finally:
        reader.close()


class HistoryRead:
    """Keep one in-flight read; never wait for it in the keyboard loop."""

    def __init__(self, pool, loader, interval=3):
        self.pool, self.loader, self.interval = pool, loader, interval
        self.key = self.pending_key = None
        self.future = self.report = None
        self.error = None
        self.observed = None
        self.next_read = 0

    def get(self, key, now):
        if key != self.key:
            self.key = key
            self.report = self.error = self.observed = None
            self.next_read = now
        if self.future is not None and self.future.done():
            try:
                result = self.future.result()
            except Exception as error:
                if key is not None and key == self.pending_key:
                    self.error = str(error) or type(error).__name__
                    self.next_read = now + self.interval
            else:
                if key is not None and key == self.pending_key:
                    self.report, self.error, self.observed = result, None, now
                    self.next_read = now + self.interval
            self.future = None
        if key is not None and self.future is None and now >= self.next_read:
            self.pending_key = key
            self.future = self.pool.submit(self.loader, key)
        return self.report

    def status(self, now):
        if self.error:
            return (
                "Stale history: "
                if self.report is not None
                else "History unavailable: "
            ) + self.error
        if self.report is None:
            return "Loading history; navigation stays available"
        return "Snapshot {:.0f}s ago{}".format(
            max(0, now - self.observed),
            "; refreshing" if self.future is not None else "",
        )


class HistoryView:
    def __init__(self):
        self.key = self.report = self.width = None
        self.logical, self.wrapped = [], []
        self.offset, self.height = 0, 1

    def page(self, repository, report, width, height):
        self.height = max(1, height)
        if repository != self.key:
            self.key, self.offset = repository, 0
            self.report, self.width = None, None
            self.logical, self.wrapped = [], []
        if report is not self.report:
            self.report, self.width = report, None
            self.logical = self.lines(repository, report) if report is not None else []
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

    @staticmethod
    def lines(repository, report):
        lines = [
            "Checkout: " + str(repository),
            "Latest {} jobs inspected; completed jobs grouped by command and reservations.".format(
                report["history_limit"]
            ),
            "Memory suggestions never apply automatically.",
        ]
        remaining = MAX_CHARS
        for entry in report["groups"]:
            line = "{} jobs | p95 wait {:.1f}s | runtime {:.1f}s | {}".format(
                entry["jobs"],
                entry["wait_p95_seconds"],
                entry["average_runtime_seconds"],
                entry["command_display"],
            )
            if len(line) + 1 > remaining:
                lines += [
                    line[:remaining],
                    "History clipped at 64 Ki characters; buildr stats --cwd PATH has full results.",
                ]
                break
            lines.append(line)
            remaining -= len(line) + 1
        if not report["groups"]:
            lines.append("No completed jobs in this history window.")
        return lines

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
        self.offset += amount * (self.height if pages else 1)

    def home(self):
        self.offset = 0

    def end(self):
        self.offset = len(self.wrapped)
