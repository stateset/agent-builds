"""Read-only presentation of this dashboard's detached submission attempts."""

import re

from .tui_text import ReportView

HANDLE = re.compile(r"[0-9a-f]{12}\Z")


def confirmed(status):
    return (
        status is not None
        and status.phase == "submitted"
        and status.kind in ("job", "plan")
        and isinstance(status.handle, str)
        and HANDLE.fullmatch(status.handle) is not None
    )


def notice(status):
    if confirmed(status):
        prefix = "Created plan " if status.kind == "plan" else "Submitted job "
        return prefix + status.handle + " | U submissions"
    if status.phase in ("unconfirmed", "submitted"):
        return (
            "Outcome unconfirmed: "
            + status.label
            + "; no automatic retry | U submissions"
        )
    return "Starting " + status.label + " | U submissions"


def compact_notice(status):
    if confirmed(status):
        return ("Plan " if status.kind == "plan" else "Job ") + status.handle + " U"
    return (
        "Unconfirmed | U"
        if status.phase in ("unconfirmed", "submitted")
        else "Starting | U"
    )


class SubmissionView(ReportView):
    def __init__(self):
        super().__init__()
        self.selected_key = None
        self._source, self._positions = None, {}
        self._keys = [None]
        self._display_key, self._display_report = object(), ()

    def current(self, report):
        if report is not self._source:
            self._source = report
            self._positions = {
                status.key: (index + 1, status)
                for index, status in enumerate(reversed(report))
            }
            self._keys = [None] + [status.key for status in reversed(report)]
            self._display_key = object()
        if self.selected_key not in self._positions:
            self.selected_key = None
        return self._positions.get(self.selected_key, (0, None))[1]

    def choose(self, report, direction):
        self.current(report)
        index = self._keys.index(self.selected_key)
        self.selected_key = self._keys[(index + direction) % len(self._keys)]
        self.home()

    def selection_label(self, report):
        status = self.current(report)
        return (
            "{}/{}".format(self._positions[status.key][0], len(report))
            if status
            else ""
        )

    def log_target(self, report):
        status = self.current(report)
        return status.handle if confirmed(status) and status.kind == "job" else None

    def action_note(self, report):
        status = self.current(report)
        if status is None:
            return "Tab selects receipt"
        target = self.log_target(report)
        if target:
            return "Enter " + target
        if confirmed(status) and status.kind == "plan":
            return "Plan: V opens views"
        return "No confirmed log"

    def page(self, scope, report, width, height, error=None):
        status = self.current(report)
        if self._display_key != self.selected_key:
            self._display_report = (status,) if status is not None else report
            self._display_key = self.selected_key
        return super().page(
            self.selected_key, self._display_report, width, height, error
        )

    def lines(self, scope, report):
        if not report:
            return [
                "No submissions from this dashboard yet.",
                "Use n for a new job or t for a saved task in the full jobs view.",
            ]
        lines = []
        for status in reversed(report):
            heading = (
                ("Created plan " if status.kind == "plan" else "Submitted job ")
                + status.handle
                if confirmed(status)
                else "Invalid receipt identity"
                if status.phase == "submitted"
                else "Outcome unconfirmed"
                if status.phase == "unconfirmed"
                else "Starting"
            )
            lines.extend(
                [
                    heading,
                    "Task/command: " + status.label,
                    "Attempt: " + status.key,
                    status.detail,
                    "Receipt: " + str(status.receipt_path),
                    "Transcript: " + str(status.transcript_path),
                    "",
                ]
            )
        lines.extend(
            [
                "This dashboard's selected attempt."
                if scope is not None
                else "This dashboard's attempts, newest first.",
                "Closing the dashboard leaves helpers and queued work running.",
                "Unconfirmed outcomes are never retried automatically.",
            ]
        )
        return lines
