"""Read-only, wrapped details for one explicitly selected job."""

from .tui_logs import clean_lines, wrap_line
from .waits import summary

MAX_DETAIL_CHARS = 64 * 1024


def bounded_lines(lines):
    remaining = MAX_DETAIL_CHARS
    result = []
    for line in lines:
        if len(line) + 1 > remaining:
            result.append(line[:remaining])
            result.append(
                "Details clipped at 64 Ki characters; buildr show JOB_ID --json has full evidence."
            )
            break
        result.append(line)
        remaining -= len(line) + 1
    return result


def detail_lines(row):
    origin = row["origin"]
    context = row["wait_context"]
    workers = row["worker_budget"]
    lines = [
        "{} | {}".format(row["id"], row["state"]),
        "Job exit: {} | {}".format(
            row.get("exit_code")
            if row.get("exit_code") is not None
            else "not recorded",
            row["state"],
        ),
        "Checkout: " + row["cwd"],
        "Reason: " + (row.get("reason") or "none recorded"),
        "Command: " + row["command_display"],
        "Submitted from pane {} | tab {} | workspace {}".format(
            origin.get("pane_id", "not captured"),
            origin.get("tab_id", "?"),
            origin.get("workspace_id", "?"),
        ),
        "Reserved: {} CPU slots, {} GiB RAM".format(row["slots"], row["memory_gb"]),
        "Resource locks: " + (", ".join(row.get("resource_keys", [])) or "unrecorded"),
        "Queue worker defaults: CARGO_BUILD_JOBS={}, RUST_TEST_THREADS={}".format(
            row["slots"], row["slots"]
        ),
        "Cargo build setting: {} ({})".format(
            workers["cargo_build"]["configured_value"], workers["cargo_build"]["source"]
        ),
        "Rust test setting: {} ({})".format(
            workers["rust_tests"]["configured_value"], workers["rust_tests"]["source"]
        ),
        "Worker settings describe configuration; runtime concurrency is not measured.",
    ]
    batch = row.get("batch", {})
    if "max_parallel" in batch:
        lines += [
            "Batch: up to {} checks | CPU hints {} | one aggregate RAM reservation".format(
                batch["max_parallel"],
                "individual"
                if batch["slots_per_check"] is None
                else batch["slots_per_check"],
            )
        ]
    for check in batch.get("checks", []):
        lines += [
            "Check {}: {} | CPU {} | native exit {}".format(
                check["name"],
                check["state"],
                check.get("cpu_slots", batch.get("slots_per_check", "unrecorded")),
                check["exit_code"],
            )
        ]
    usage = row["usage"]
    if usage:
        lines += [
            "Measured CPU {:.2f}s | sampled peak RSS {:.3f} GiB | {} samples".format(
                usage.get("cpu_seconds", 0),
                usage.get("sampled_peak_rss_gb", 0),
                usage.get("samples", 0),
            ),
            "RSS is sampled, may include shared pages, and may miss short peaks.",
        ]
    else:
        lines += ["Job resource usage has not been measured."]
    lines += [
        "Wait breakdown: " + summary(row["wait_breakdown"]),
        "Observed {:.0%}; gaps and legacy waits are unobserved.".format(
            row["wait_breakdown"]["coverage"]
        ),
    ]
    if row["source"]:
        source = row["source"]
        lines += [
            "Source: submitted {} | started {}".format(
                source.get("submission", {}).get("head", "unavailable"),
                source.get("start", {}).get("head", "not captured"),
            ),
            "Source drift: before start {} | during command {}".format(
                source.get("changed_since_submission", "unknown"),
                source.get("changed_during_run", "unknown"),
            ),
        ]
        observed = source.get(
            "finish", source.get("start", source.get("submission", {}))
        )
        inputs = observed.get("inputs", {})
        if inputs:
            lines += [
                "Declared inputs: {} | {} files (latest observation)".format(
                    inputs.get("status", "unavailable"), len(inputs.get("files", []))
                )
            ]
            for item in inputs.get("files", [])[:16]:
                lines += [
                    "  {} | {} | {}".format(
                        item["path"],
                        item["status"],
                        (item.get("sha256") or "no hash")[:12],
                    )
                ]
    if row.get("scheduling"):
        policy = row["scheduling"]
        lines += [
            "Runner policy: {}".format(policy["policy_version"]),
            "Age priority: {} | threshold {}s | current quick reserve {} CPU / {} jobs / {} GiB".format(
                policy["age_priority"],
                policy["aging_seconds"],
                policy["quick_reserve_slots"],
                policy["quick_reserve_jobs"],
                policy["quick_reserve_memory_gb"],
            ),
            policy["note"],
        ]
    if context:
        lines += [
            "RAM admission: {} GiB job + {} GiB headroom = {} GiB minimum available".format(
                context.get("job_memory_gb", row["memory_gb"]),
                context.get("memory_headroom_gb", "?"),
                context.get("required_memory_gb", "?"),
            ),
            "CPU: {} used + {} requested / {} slots".format(
                context.get(
                    "current_used_cpu_slots", context.get("used_cpu_slots", "?")
                ),
                row["slots"],
                context.get("total_cpu_slots", "?"),
            ),
            "Contributing running reservations (live observation):",
        ]
        lines += [
            "{}: {} CPU / {} GiB | {}".format(
                job["id"], job["slots"], job["memory_gb"], job["command_display"]
            )
            for job in context.get("contributing_jobs", [])
        ]
    return lines


class DetailsView:
    """Reuse formatted evidence until the selected snapshot or width changes."""

    def __init__(self):
        self.job = None
        self.offset = 0
        self.height = 1
        self.clear()

    def clear(self):
        # Release hidden evidence while retaining this job's scroll position.
        self.row = None
        self.logical = []
        self.wrapped = []
        self.width = None

    def page(self, row, width, height):
        self.height = max(1, height)
        if row is None:
            self.clear()
            return []
        if row["id"] != self.job:
            self.job, self.offset = row["id"], 0
        if row is not self.row:
            self.logical = bounded_lines(detail_lines(row))
            self.row, self.width = row, None
        width = max(1, width)
        if width != self.width:
            self.wrapped = [
                part
                for line in self.logical
                for paragraph in (
                    clean_lines(str(line).encode("utf-8", errors="replace")) or [""]
                )
                for part in wrap_line(paragraph, width)
            ]
            self.width = width
        self.offset = min(max(0, self.offset), max(0, len(self.wrapped) - self.height))
        return self.wrapped[self.offset : self.offset + self.height]

    def position(self):
        total = len(self.wrapped)
        if not total:
            return "0/0"
        return "{}-{}/{}".format(
            self.offset + 1, min(total, self.offset + self.height), total
        )

    def scroll(self, pages):
        self.offset += pages * self.height

    def home(self):
        self.offset = 0

    def end(self):
        self.offset = len(self.wrapped)
