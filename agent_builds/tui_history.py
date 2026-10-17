"""Bounded, asynchronous, read-only history for the dashboard."""

from .tui_reads import SnapshotRead, submit_read
from .tui_text import ReportView

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


class HistoryRead(SnapshotRead):
    def __init__(self, loader, interval=3, submit=None):
        super().__init__(loader, interval, submit or submit_read, label="history")


class HistoryView(ReportView):
    # This formatter already bounds its text and supplies the history-specific notice.
    limit = None

    @staticmethod
    def lines(repository, report):
        lines = [
            "Checkout: " + str(repository),
            "Latest {} jobs inspected; completed jobs grouped by command and reservations.".format(
                report["history_limit"]
            ),
            "Memory suggestions never apply automatically.",
        ]
        remaining = max(0, MAX_CHARS - sum(len(line) + 1 for line in lines))
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
