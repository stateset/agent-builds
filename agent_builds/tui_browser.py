"""Read-only dashboard browsing; never changes queue or admission state."""

import math
from pathlib import Path
import sqlite3

MODES = ("all", "active", "waiting", "problems")
ORDERS = ("queue", "wait", "runtime", "repository")


class BrowserCache:
    """Reuse display filtering/sorting until the snapshot or controls change."""

    def __init__(self):
        self.loaded, self.controls, self.rows = None, None, []

    def get(self, loaded, mode="all", query="", order="queue"):
        controls = (mode, query, order)
        if loaded is not self.loaded or controls != self.controls:
            rows = sort_rows(filter_rows(loaded, mode, query), order)
            self.loaded, self.controls, self.rows = loaded, controls, rows
        return self.rows


class SelectedJobRead:
    """Cache one explicitly selected read; never retain another job on failure."""

    def __init__(self, load):
        self.load = load
        self.job, self.value, self.error = None, None, None
        self.updated = float("-inf")

    def get(self, job, now):
        if job != self.job or now - self.updated >= 1:
            self.job, self.value, self.error, self.updated = job, None, None, now
            if job is not None:
                try:
                    self.value = self.load(job)
                except (ValueError, OSError, sqlite3.Error) as error:
                    self.error = str(error)
        return self.value


def seconds(value):
    try:
        value = float(value)
        return max(0, value) if math.isfinite(value) else 0
    except (TypeError, ValueError, OverflowError):
        return 0


def sort_rows(rows, order="queue"):
    """Sort only the loaded display rows, never scheduler admission order."""
    if order == "queue":
        return list(rows)

    def key(row):
        tie = (seconds(row.get("created")), row["id"])
        if order == "wait":
            return (row["state"] != "queued", -seconds(row.get("wait_seconds")), tie)
        if order == "runtime":
            return (
                row["state"] not in ("running", "cancelling"),
                -seconds(row.get("elapsed_seconds")),
                tie,
            )
        return (str(row.get("cwd", "")).casefold(), tie)

    if order not in ORDERS:
        raise ValueError("Unknown display order")
    return sorted(rows, key=key)


def duration(value):
    value = int(seconds(value))
    if value < 60:
        return str(value) + "s"
    if value < 3600:
        return "{}m{:02}s".format(value // 60, value % 60)
    if value < 86400:
        return "{}h{:02}m".format(value // 3600, value % 3600 // 60)
    return "{}d{:02}h".format(value // 86400, value % 86400 // 3600)


def page_size(height, compact=False, expanded=False):
    if expanded:
        return max(1, height - (7 if compact else 10))
    return 1 if compact else min(8, max(3, (height - 12) // 2))


def columns(width):
    if width < 40:
        return "{:12} STATE".format("JOB")
    if width < 65:
        return "{:12} {:9} {:>5} {:>5} REPOSITORY".format("JOB", "STATE", "WAIT", "RUN")
    return "{:12} {:9} {:>2} {:>4} {:>5} {:>5} REPOSITORY / COMMAND".format(
        "JOB", "STATE", "CPU", "GiB", "WAIT", "RUN"
    )


def job_line(row, width):
    if width < 40:
        return "{} {}".format(row["id"], row["state"][:6])
    prefix = "{} {:9} ".format(row["id"], row["state"])
    if width >= 65:
        prefix += "{:>2} {:>4g} ".format(row["slots"], row["memory_gb"])
    text = prefix + "{:>5} {:>5} {}".format(
        duration(row.get("wait_seconds")),
        duration(row.get("elapsed_seconds")),
        Path(row["cwd"]).name,
    )
    return text + (" / " + row.get("command_display", "") if width >= 65 else "")


def filter_rows(rows, mode="all", query=""):
    query = query.casefold()
    if mode == "all" and not query:
        return list(rows)
    result = []
    for row in rows:
        state = row["state"]
        if mode == "active" and state not in ("queued", "running", "cancelling"):
            continue
        if mode == "waiting" and state != "queued":
            continue
        if mode == "problems" and not (
            state in ("failed", "lost")
            or (state == "cancelled" and row.get("exit_code") == 124)
        ):
            continue
        if query:
            origin = row.get("origin") or {}
            fields = [
                str(row.get(key) or "")
                for key in ("id", "cwd", "command_display", "reason", "state")
            ]
            fields += [
                str(origin.get(key) or "")
                for key in ("pane_id", "tab_id", "workspace_id")
            ]
            if query not in "\n".join(fields).casefold():
                continue
        result.append(row)
    return result


def label(mode, query, shown, loaded, order="queue", short=False):
    if short:
        return "JOBS {} {} {}/{}".format(order, mode, shown, loaded)
    text = "JOBS {} | {}/{} loaded".format(mode, shown, loaded)
    if order != "queue":
        text += " | sort: " + order
    return text + (" | search: " + query if query else "")


HELP = (
    "BROWSE JOBS",
    "j/k or arrows: select a job",
    "PgUp/PgDn: page through jobs; Home/End: first/last",
    "f: all / active / waiting / problems",
    "s: display order queue / wait / runtime / repository; selected job stays selected",
    "Tab: expand jobs into a table; Tab again restores the split with output",
    "Wait sort shows waiting jobs first; runtime sort shows running jobs first.",
    "Display order never changes scheduling priority. Table times are observed, not estimates.",
    "/: search ID, repository, command, reason or Herdr pane; blank clears",
    "i or Enter: job details (full view)",
    "g: jump to the selected job's recorded blocker",
    "Filters search at most 200 loaded jobs. Queue health and reservations remain global.",
    "READ-ONLY VIEWS",
    "l: focused logs; j/k or PgUp/PgDn scroll, Home shows oldest loaded output",
    "In logs: Space holds/resumes output; End returns live; l returns to jobs",
    "In logs: / finds literal text (blank clears); n/N jump matching lines with wraparound",
    "Search holds the loaded snapshot, highlights matching lines, and ignores case.",
    "Holding logs never pauses the queue. Logs load only the recent 256 KiB.",
    "u: queue health; r: selected-job workbench; h: advisor",
    "d: cache inventory (no deletion); v: cycle views",
    "PgUp/PgDn scroll detail views. Narrow panes support jobs, plans, watches, health and workbench.",
    "DELIBERATE ACTIONS (FULL VIEW)",
    "n: new job; o: submit plan; t: saved task",
    "c: request cancellation after typing yes",
    "p: toggle new-start pause; running jobs continue",
    "b: edit budgets; a: explicitly select idle announcement recipients",
    "These affect the shared queue. Search and filters only change this dashboard.",
    "q: close dashboard; jobs continue",
)
