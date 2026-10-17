"""Pinned job context and read-only diagnosis for dashboard inspections."""

from .tui_text import ReportView
from .tui_browser import duration


class InspectionFocus:
    def __init__(self):
        self.row = None

    def select(self, row):
        self.row = row

    def get(self, view, browser_row, loaded):
        if view not in (3, 12, 14):
            self.row = None
            return None
        if self.row is None:
            self.row = browser_row
        elif self.row:
            self.row = next(
                (row for row in loaded if row["id"] == self.row["id"]), self.row
            )
        return self.row

    def move(self, rows, direction):
        index = next(
            (
                i
                for i, row in enumerate(rows)
                if self.row and row["id"] == self.row["id"]
            ),
            None,
        )
        if index is None:
            index = 0 if direction > 0 else max(0, len(rows) - 1)
        else:
            index = min(max(0, index + direction), max(0, len(rows) - 1))
        self.row = rows[index] if rows else None
        return index


def inspect_report(root, key):
    from .store import Store
    from . import diagnostics, workbench

    kind, handle = key
    reader = Store(root, read_only=True)
    try:
        if reader.db is not None:
            reader.db.execute("PRAGMA busy_timeout=1000")
        if kind == "workbench":
            return workbench.report(reader, handle)
        if kind == "health":
            return diagnostics.report(reader)
        if kind == "why":
            return diagnostics.report(reader, job=handle, limit=1)
        raise ValueError("Unknown inspection kind")
    finally:
        reader.close()


class HealthView(ReportView):
    def lines(self, scope, report):
        from .diagnostics import lines

        return lines(report)


class WorkbenchView(ReportView):
    def lines(self, scope, report):
        from .workbench import lines

        return lines(report, scope[1])


class WhyView(ReportView):
    def lines(self, scope, report):
        rows = report["jobs"]
        if not rows:
            yield "No recorded evidence for this job."
            return
        row = rows[0]
        if row["state"] == "queued":
            yield "Runner reason: " + (row["reason"] or "not recorded")
        yield "{} | {} | waited {} | ran {}".format(
            row["id"],
            row["state"],
            duration(row["wait_seconds"]),
            duration(row["elapsed_seconds"]),
        )
        yield "Checkout: " + row["cwd"]
        yield (
            "Command: "
            + row["command_display"]
            + (" [truncated; use job details]" if row["command_truncated"] else "")
        )
        if row["state"] == "queued":
            observed = row["observation"]
            yield "Observation: " + (
                "unrecorded"
                if observed["age_seconds"] is None
                else "{} old{}".format(
                    duration(observed["age_seconds"]),
                    " [STALE]" if observed["stale"] else "",
                )
            )
            yield "Reasons are observations, not an ETA or a new admission decision."
            pool = row["capacity"]
            if not pool["fits_alone"]:
                yield "REQUEST CANNOT FIT ALONE under the current reservation limits."
            yield "Reservation snapshot (local slots/RAM are estimates, not hard limits):"
            for item in pool["dimensions"]:
                yield "{}: {} used / {} limit | {} requested | {} remaining | deficit {}".format(
                    item["name"],
                    item["used"],
                    item["limit"],
                    item["requested"],
                    item["remaining"],
                    item["deficit"],
                )
            yield pool["note"]
            machine, config = report["machine"], report["config"]
            required = config["min_available_gb"] + row["memory_gb"]
            yield "Host RAM: {:.2f} GiB available / {:.2f} required | deficit {:.2f} GiB".format(
                machine["available_gb"],
                required,
                max(0, required - machine["available_gb"]),
            )
            yield "RAM requirement: {:.2f} GiB headroom + {:.2f} GiB job request".format(
                config["min_available_gb"], row["memory_gb"]
            )
            disk = row["disk"]
            yield "Disk: {}".format(
                "unknown (" + disk.get("error", "unavailable") + ")"
                if disk["available_gb"] is None
                else "{:.3f} GiB free / {:.3f} required | deficit {:.3f} GiB".format(
                    disk["available_gb"], disk["required_gb"], disk["deficit_gb"]
                )
            )
            parts = disk["components"]
            yield "Disk requirement: max({:.3f}, {:.3f}) floor + {:.3f} job scratch + {:.3f} running scratch GiB".format(
                parts["machine_floor_gb"],
                parts["job_floor_gb"],
                parts["job_scratch_gb"],
                parts["running_scratch_gb"],
            )
            for item in disk["contributing_jobs"]:
                yield "{} reserves {:.3f} GiB scratch".format(
                    item["job_id"], item["scratch_gb"]
                )
            if disk["contributors_truncated"]:
                yield "More scratch contributors exist; inspect full diagnosis JSON."
        else:
            yield "This job is not queued. Recorded reason: " + (
                row["reason"] or "none"
            )
            yield "Recorded job exit: " + (
                str(row["exit_code"])
                if row["exit_code"] is not None
                else "not recorded"
            )
            yield "No retry, cancellation or reservation change is performed."
        links = row["blocker_chain"]
        yield "Observed blocker chain: " + links["end"]
        for index, item in enumerate(links["jobs"]):
            yield "{}. {} {}{} | {}".format(
                index + 1,
                item["id"],
                item["state"],
                " [STALE]"
                if item["state"] == "queued" and item["observation"]["stale"]
                else "",
                item["command_display"],
            )
        if not links["complete"]:
            yield "Blocker chain is incomplete; do not infer that capacity is available."
        yield "g opens the recorded blocker log; i shows full job evidence; l shows this job log."
        yield report["note"]
