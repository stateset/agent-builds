"""Read-only stage selection from an accepted plan snapshot."""

import re

_HANDLE = re.compile(r"[0-9a-f]{12}\Z")


class PlanStages:
    def __init__(self):
        self.plan_id = None
        self.name = None

    def current(self, plan):
        if plan is None:
            self.plan_id, self.name = None, None
            return None
        if plan["id"] != self.plan_id:
            self.plan_id, self.name = plan["id"], None
        stage = next((s for s in plan["stages"] if s["name"] == self.name), None)
        if stage is None:
            self.name = None
        return stage

    def choose(self, plan, direction):
        self.current(plan)
        if plan is None:
            return
        names = [None] + [s["name"] for s in plan["stages"]]
        self.name = names[(names.index(self.name) + direction) % len(names)]

    def log_target(self, plan):
        stage = self.current(plan)
        if stage is None:
            return None
        handle, job = stage.get("job_id"), stage.get("job")
        if (
            isinstance(handle, str)
            and _HANDLE.fullmatch(handle)
            and job
            and job.get("id") == handle
        ):
            return handle
        return None

    def lines(self, plan):
        stage = self.current(plan)
        if stage is None:
            return []
        return [
            "> " + stage["name"],
            "Enter " + self.log_target(plan)
            if self.log_target(plan)
            else "No recorded log",
            stage["state"],
        ]
