"""Bounded, read-only context captured when a dashboard prompt opens."""

from .tui_logs import clean_lines, wrap_line

MAX_CONTEXT_CHARS = 16 * 1024


class PromptContext:
    def __init__(self, text=""):
        clipped = len(text) > MAX_CONTEXT_CHARS
        self.lines = clean_lines(text[:MAX_CONTEXT_CHARS].encode("utf-8"))
        if clipped:
            self.lines.append("[Context clipped at 16 Ki characters]")
        self.width, self.wrapped = None, []
        self.top, self.room = 0, 1

    def __bool__(self):
        return bool(self.lines)

    def page(self, width, room):
        width = max(1, width)
        self.room = max(1, room)
        if width != self.width:
            self.wrapped = [
                part for line in self.lines for part in wrap_line(line, width)
            ]
            self.width = width
        self.top = min(max(0, self.top), max(0, len(self.wrapped) - self.room))
        return self.wrapped[self.top : self.top + self.room]

    def scroll(self, direction):
        self.top = min(
            max(0, self.top + direction * self.room),
            max(0, len(self.wrapped) - self.room),
        )

    def position(self):
        if not self.wrapped:
            return "0-0/0"
        return "{}-{}/{}".format(
            self.top + 1,
            min(len(self.wrapped), self.top + self.room),
            len(self.wrapped),
        )


def job_context(row):
    return "\n".join(
        [
            "State at prompt open: " + row["state"],
            "Checkout: " + row["cwd"],
            "Command: " + row["command_display"],
            "Job request: {} CPU slots / {} GiB RAM".format(
                row["slots"], row["memory_gb"]
            ),
        ]
    )


def recipient_context(recipients):
    return "\n".join(
        [
            "Only explicitly selected idle recipients receive native agent input.",
            "Pane IDs: "
            + (", ".join(agent["pane_id"] for agent in recipients) or "none"),
        ]
        + [
            "{} | {} | {} | {}".format(
                agent["pane_id"],
                agent.get("agent_status", ""),
                agent.get("agent", ""),
                agent.get("cwd", ""),
            )
            for agent in recipients
        ]
        + ([] if recipients else ["No idle recipients in this preview."])
    )
