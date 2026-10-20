"""Bounded, wrapped plan evidence from the dashboard's accepted snapshot."""

from .tui_text import ReportView


def stage_details(stage):
    job = stage.get("job")
    lines = ["Stage {} | {}".format(stage["name"], stage["state"])]
    valid_job = job and job.get("id") == stage.get("job_id")
    reason = stage.get("reason") or (job.get("reason") if valid_job else None)
    lines.append("Reason: " + (reason or "none recorded"))
    if not job:
        lines.append(
            "Job not submitted"
            if not stage.get("job_id")
            else "Job evidence unavailable"
        )
        return lines
    if job.get("id") != stage.get("job_id"):
        lines.append("Job identity mismatch; evidence unavailable")
        return lines
    lines.extend(
        [
            "Job {} | {} | exit {}".format(
                job["id"],
                job["state"],
                job["exit_code"] if job["exit_code"] is not None else "not recorded",
            ),
            "Waited {:.1f}s | ran {:.1f}s".format(
                job["wait_seconds"], job["elapsed_seconds"]
            ),
            "Command: " + job["command_display"],
        ]
    )
    source = job.get("source") or {}
    if source.get("changed_since_submission") or source.get("changed_during_run"):
        lines.append(
            "Source changes observed; inspect plan evidence before relying on the result."
        )
    elif job.get("started") and source.get("start", {}).get("status") != "captured":
        lines.append("Source identity unavailable or not yet captured.")
    return lines


class PlanDetails(ReportView):
    clip_notice = (
        "Plan details clipped at 64 Ki characters; export the plan for full evidence."
    )

    def __init__(self, selection):
        super().__init__()
        self.selection = selection

    def page_plan(self, plan, width, height):
        selected = self.selection.current(plan)
        scope = (
            (
                plan["id"],
                self.selection.name,
                selected.get("job_id") if selected else None,
            )
            if plan
            else None
        )
        return self.page(scope, plan, width, height)

    def lines(self, scope, plan):
        selected = self.selection.current(plan)
        lines = stage_details(selected) + [""] if selected else []
        lines.extend(
            [
                "{} | {} | {}".format(plan["id"], plan["state"], plan["name"]),
                "Stages: {} succeeded / {} total | elapsed {:.1f}s".format(
                    sum(s["state"] == "succeeded" for s in plan["stages"]),
                    len(plan["stages"]),
                    plan["elapsed_seconds"],
                ),
            ]
        )
        if plan["state"] not in ("succeeded", "failed", "cancelled"):
            if plan["controller_alive"] is False:
                lines.append(
                    "Controller is not running; r resumes monitoring without retrying submitted stages."
                )
            elif plan["controller_alive"] is None:
                lines.append(
                    "Controller liveness is not verified by read-only inspection."
                )
        if plan["reason"]:
            lines.append(plan["reason"])
        if selected is None:
            for stage in plan["stages"]:
                lines.extend(stage_details(stage) + [""])
        return lines
