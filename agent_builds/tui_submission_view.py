"""Read-only presentation of this dashboard's detached submission attempts."""

from .tui_text import ReportView


def notice(status):
    if status.phase == "submitted":
        prefix = "Created plan " if status.kind == "plan" else "Submitted job "
        return prefix + status.handle + " | U submissions"
    if status.phase == "unconfirmed":
        return (
            "Outcome unconfirmed: "
            + status.label
            + "; no automatic retry | U submissions"
        )
    return "Starting " + status.label + " | U submissions"


def compact_notice(status):
    if status.phase == "submitted":
        return ("Plan " if status.kind == "plan" else "Job ") + status.handle + " U"
    return "Unconfirmed | U" if status.phase == "unconfirmed" else "Starting | U"


class SubmissionView(ReportView):
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
                "This dashboard's attempts, newest first.",
                "Closing the dashboard leaves helpers and queued work running.",
                "Unconfirmed outcomes are never retried automatically.",
            ]
        )
        return lines
