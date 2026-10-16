"""Read-only dashboard browsing; never changes queue or admission state."""

MODES = ("all", "active", "waiting", "problems")


def filter_rows(rows, mode="all", query=""):
    query = query.casefold()
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
        origin = row.get("origin") or {}
        fields = [
            str(row.get(key) or "")
            for key in ("id", "cwd", "command_display", "reason", "state")
        ]
        fields += [
            str(origin.get(key) or "") for key in ("pane_id", "tab_id", "workspace_id")
        ]
        if query and query not in "\n".join(fields).casefold():
            continue
        result.append(row)
    return result


def label(mode, query, shown, loaded):
    text = "JOBS {} | {}/{} loaded".format(mode, shown, loaded)
    return text + (" | search: " + query if query else "")


HELP = (
    "BROWSE JOBS",
    "j/k or arrows: select a job",
    "PgUp/PgDn: page through jobs; Home/End: first/last",
    "f: all / active / waiting / problems",
    "/: search ID, repository, command, reason or Herdr pane; blank clears",
    "i or Enter: job details (full view)",
    "g: jump to the selected job's recorded blocker",
    "Filters search at most 200 loaded jobs. Queue health and reservations remain global.",
    "READ-ONLY VIEWS",
    "l: focused logs; j/k or PgUp/PgDn scroll, Home shows oldest loaded output",
    "In logs: Space holds/resumes output; End returns live; l returns to jobs",
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
