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

    def issue(self, plan, direction):
        """Select an observed failed/lost stage, without inferring job outcomes."""
        self.current(plan)
        if plan is None:
            return False
        stages = plan["stages"]
        issues = [
            index
            for index, stage in enumerate(stages)
            if stage["state"] in ("failed", "lost")
        ]
        if not issues:
            return False
        position = next(
            (index for index, stage in enumerate(stages) if stage["name"] == self.name),
            -1 if direction > 0 else len(stages),
        )
        target = (
            next((index for index in issues if index > position), issues[0])
            if direction > 0
            else next(
                (index for index in reversed(issues) if index < position), issues[-1]
            )
        )
        name = stages[target]["name"]
        changed = name != self.name
        self.name = name
        return changed

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
