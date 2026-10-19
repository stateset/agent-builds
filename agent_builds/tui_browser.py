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

    def get(self, loaded, mode="all", query="", order="queue", repository=None):
        controls = (mode, query, order, repository)
        if loaded is not self.loaded or controls != self.controls:
            rows = sort_rows(filter_rows(loaded, mode, query, repository), order)
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


def filter_rows(rows, mode="all", query="", repository=None):
    query = query.casefold()
    if mode == "all" and not query and repository is None:
        return list(rows)
    result = []
    for row in rows:
        if repository is not None and row.get("cwd") != repository:
            continue
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


def label(
    mode,
    query,
    shown,
    loaded,
    order="queue",
    short=False,
    repository=None,
    follow=False,
):
    scope = " @" + (Path(repository).name or repository) if repository else ""
    if short:
        if follow:
            return "FOLLOW {}/{} {} {}{}".format(shown, loaded, order, mode, scope)
        if scope:
            return "JOBS {}/{}{} {} {}".format(shown, loaded, scope, mode, order)
        return "JOBS {} {} {}/{}".format(order, mode, shown, loaded)
    text = (
        "JOBS{} {} | {}/{} loaded".format(
            " FOLLOW" if follow else "", mode, shown, loaded
        )
        + scope
    )
    if order != "queue":
        text += " | sort: " + order
    return text + (" | search: " + query if query else "")


HELP = (
    "HELP: / searches these instructions literally, ignoring case; blank clears. Esc/?/q returns.",
    "In help: j/k or arrows scroll; PgUp/PgDn page; Home/End first/last. Query and position survive reopening.",
    "FIND A VIEW",
    "J: open a log by full job ID from any view (20x8 minimum); Esc/blank cancels",
    "Job lookup ignores browser filters/history limits. Navigation abandons pending lookup; l returns to jobs.",
    "V: searchable view picker in full/narrow panes; type to filter names and descriptions",
    "In picker: arrows/Tab or PgUp/PgDn select; Home/End first/last; Enter opens; Esc returns",
    "Backspace edits the query; Ctrl+U clears. ! marks a size/job requirement; * is current.",
    "Picker consumes action keys as search text. Cancelling preserves following and inspection.",
    "BROWSE JOBS",
    "j/k or arrows: select a job",
    "F: follow the first matching displayed job as snapshots refresh; F again pins it",
    "Following is opt-in. Other input pins selection, except Tab, resize and help.",
    "Opening details/logs pins that job. Help suspends following until you return.",
    "PgUp/PgDn: page through jobs; Home/End: first/last",
    "f: all / active / waiting / problems",
    "R: focus the selected checkout; R again removes repository focus, even with no matches",
    "C: clear repository focus, search and state filter; keep display order and selection",
    "Repository focus uses the exact recorded checkout path; other worktrees stay separate.",
    "s: display order queue / wait / runtime / repository; selected job stays selected",
    "Tab: expand jobs into a table; Tab again restores the split with output",
    "Wait sort shows waiting jobs first; runtime sort shows running jobs first.",
    "Display order never changes scheduling priority. Table times are observed, not estimates.",
    "/: search ID, repository, command, reason or Herdr pane; blank clears",
    "i or Enter: toggle wrapped job details in full or narrow panes",
    "In details: PgUp/PgDn page; Home/End first/last; j/k still select jobs",
    "w: why waiting? Recorded reason, observation age, capacity, disk and blocker chain",
    "In why: j/k or PgUp/PgDn scroll; Home/End first/last; w jobs; i details; l log; g blocker log",
    "Inspection stays on its job across snapshot changes; only explicit navigation changes it.",
    "Wait reasons are observations, not an ETA or a fresh admission decision.",
    "g: jump to the selected job's recorded blocker",
    "Filters search at most 200 loaded jobs. Queue health and reservations remain global.",
    "READ-ONLY VIEWS",
    "l: focused logs; j/k or PgUp/PgDn scroll, Home shows oldest loaded output",
    "In logs: b returns to the source view; l returns to jobs; q quits (jobs continue)",
    "In logs: Space holds/resumes output; End returns live. Jumping J from a log keeps its original source.",
    "In logs: / finds literal text (blank clears); n/N jump matching lines with wraparound",
    "Search holds the loaded snapshot, highlights matching lines, and ignores case.",
    "Holding logs never pauses the queue. Logs load only the recent 256 KiB.",
    "H: selected-checkout history; j/k or PgUp/PgDn scroll; Home/End first/last; H returns",
    "History refreshes in the background every 3 seconds while visible; no automatic tuning.",
    "u: queue health; r: selected-job workbench; h: advisor",
    "Health, why and workbench read in the background with one shared in-flight read.",
    "Hidden views/help suspend refreshes. Read errors label retained results as stale.",
    "d: cache inventory (no deletion); v: cycle views",
    "PgUp/PgDn scroll detail views. Narrow panes support jobs, plans, watches, history, health and workbench.",
    "DELIBERATE ACTIONS (FULL VIEW)",
    "n: new job; o: submit plan; t: saved task",
    "U: this dashboard's submission receipts; starting is not confirmed job creation.",
    "In submissions: Tab/Shift+Tab cycles overview and receipts; Enter opens the selected confirmed job log.",
    "j/k, paging and Home/End scroll the current report. Receipt selection survives updates; unconfirmed attempts never open or retry work.",
    "Unconfirmed submissions are never retried automatically; inspect their receipt and transcript.",
    "c: request cancellation after typing yes",
    "Input prompts: Enter accepts, Esc cancels, Ctrl+U clears; resize preserves entered text.",
    "PgUp/PgDn pages prompt context without editing input; context is a snapshot.",
    "p: toggle new-start pause; running jobs continue",
    "b: edit budgets; a: explicitly select idle announcement recipients",
    "These affect the shared queue. Search and filters only change this dashboard.",
    "q: close dashboard; jobs continue",
)
