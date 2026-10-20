"""Literal filtering of accepted plan snapshots, without database access."""


def searchable(plan):
    fields = [str(plan.get(key) or "") for key in ("id", "name", "state", "reason")]
    for stage in plan.get("stages", []):
        fields.extend(
            str(stage.get(key) or "") for key in ("name", "state", "reason", "job_id")
        )
        job = stage.get("job")
        if job and job.get("id") == stage.get("job_id"):
            fields.extend(
                str(job.get(key) or "") for key in ("command_display", "reason", "cwd")
            )
    # A query must match one recorded field, never an invented boundary.
    return tuple(field.casefold() for field in fields)


class PlanFilter:
    def __init__(self):
        self.query = ""
        self._source = None
        self._indexed = None
        self._controls = None
        self._rows = []

    def search(self, query):
        self.query = query[:256]

    def get(self, report):
        if report is None:
            return []
        if report is not self._source:
            self._source = report
            self._indexed = None
            self._controls = None
        if self.query != self._controls:
            if self.query:
                if self._indexed is None:
                    self._indexed = [searchable(plan) for plan in report]
                needle = self.query.casefold()
                self._rows = [
                    plan
                    for plan, fields in zip(report, self._indexed)
                    if any(needle in field for field in fields)
                ]
            else:
                self._rows = report
            self._controls = self.query
        return self._rows

    def label(self, status, report, compact=False):
        if not self.query:
            return status
        if report is None:
            return status + " / find pending"
        count = "{}/{}".format(len(self.get(report)), len(report))
        if compact:
            prefix = (
                "STALE PLANS"
                if status.casefold().startswith("stale")
                else "PLANS~"
                if "~" in status
                else "PLANS"
            )
            return "{} {} / {}".format(prefix, count, self.query)
        return "{} | Plans {} loaded / {}".format(status, count, self.query)

    def empty(self, report, error=None):
        if report is None:
            return ["Plans unavailable" if error else "Loading plans"]
        return ["No matching plans", "/: blank clears"]
