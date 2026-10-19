"""Bounded read-only queue overview, separate from admission decisions."""

import contextlib
import json
import math
from pathlib import Path
import shutil
import time

from . import waits

MAX_WAIT_SAMPLE = 200
MAX_CONTEXT = 8192
LABELS = {
    "repository_lock": "repository locks",
    "lock_compatibility": "older lock protocol",
    "priority": "queue priority",
    "disk": "disk space",
    "ram_headroom": "available RAM",
    "cpu_capacity": "CPU capacity",
    "memory_capacity": "reserved RAM",
    "job_capacity": "job capacity",
    "heavy_capacity": "heavy job capacity",
    "quick_reserve": "protected quick capacity",
    "oversized": "oversized requests",
    "paused": "pause",
    "scheduling": "scheduling",
}
NOTE = (
    "Disk measurements cover the shared state filesystem; job filesystems may impose "
    "additional limits. Requirements include current running scratch reservations. "
    "Runner reasons are sampled observations, not admission decisions or completion "
    "estimates. No jobs, reservations, settings or files are changed."
)


def report(store, repo=None, config=None, now=None):
    now = time.time() if now is None else now
    context = (
        contextlib.nullcontext()
        if store.db is not None and store.db.in_transaction
        else store.read_snapshot()
    )
    with context:
        config = store.config() if config is None else config
        result = dict(
            schema="buildr-queue-health-v1",
            initialized=store.db is not None,
            measured_at=now,
            repository=str(Path(repo).expanduser().resolve()) if repo else None,
            queued_jobs=0,
            waiting_jobs=0,
            queued_cancellations=0,
            oldest_wait_seconds=0,
            reservations=dict(jobs=0, cpu_slots=0, memory_gb=0, scratch_gb=0),
            reservation_scope="global",
            observations=dict(
                sampled_jobs=0,
                truncated=False,
                fresh=[],
                stale_jobs=0,
                missing_jobs=0,
                unreadable_jobs=0,
            ),
            disk=dict(
                available_gb=None,
                required_min_gb=None,
                required_max_gb=None,
                shortfall_min_gb=None,
                shortfall_max_gb=None,
                limited_jobs=None,
            ),
            status="uninitialized" if store.db is None else "idle",
            changes_applied=False,
            note=NOTE,
        )
        if store.db is None:
            return finish(result, config)
        columns = {row[1] for row in store.db.execute("PRAGMA table_info(jobs)")}
        floor = "min_free_gb" if "min_free_gb" in columns else "0"
        scratch = "scratch_gb" if "scratch_gb" in columns else "0"
        context_column = "wait_context" if "wait_context" in columns else "'{}'"
        running = store.db.execute(
            "SELECT COUNT(*),COALESCE(SUM(slots),0),COALESCE(SUM(memory_gb),0),"
            "COALESCE(SUM(" + scratch + "),0) FROM jobs "
            "WHERE state IN ('running','cancelling')"
        ).fetchone()
        result["reservations"] = dict(
            zip(("jobs", "cpu_slots", "memory_gb", "scratch_gb"), running)
        )
        where, params = store.filters(repo=repo)
        where += (" AND " if where else " WHERE ") + "state IN ('queued','held')"
        need = "MAX(?," + floor + ")+" + scratch
        aggregate = store.db.execute(
            "SELECT COUNT(*),COALESCE(SUM(cancel_requested=0),0),"
            "MIN(CASE WHEN cancel_requested=0 THEN created END),"
            "MIN(CASE WHEN cancel_requested=0 THEN " + need + " END),"
            "MAX(CASE WHEN cancel_requested=0 THEN " + need + " END) FROM jobs" + where,
            (config["min_free_gb"], config["min_free_gb"]) + params,
        ).fetchone()
        total, waiting, oldest, minimum, maximum = aggregate
        result.update(
            queued_jobs=total,
            waiting_jobs=waiting,
            queued_cancellations=total - waiting,
            oldest_wait_seconds=max(0, now - oldest) if oldest is not None else 0,
        )
        if waiting:
            disk = result["disk"]
            reserved = running[3]
            disk.update(
                required_min_gb=minimum + reserved, required_max_gb=maximum + reserved
            )
            try:
                free = shutil.disk_usage(store.root).free / 1024**3
                disk.update(
                    available_gb=free,
                    shortfall_min_gb=max(0, minimum + reserved - free),
                    shortfall_max_gb=max(0, maximum + reserved - free),
                )
                disk["limited_jobs"] = store.db.execute(
                    "SELECT COUNT(*) FROM jobs"
                    + where
                    + " AND cancel_requested=0 AND "
                    + need
                    + "+?>?",
                    params + (config["min_free_gb"], reserved, free),
                ).fetchone()[0]
            except OSError as error:
                disk["error"] = type(error).__name__
            sample = store.db.execute(
                "SELECT substr(reason,1,512),substr("
                + context_column
                + ",1,?) FROM jobs"
                + where
                + " AND cancel_requested=0 ORDER BY created,id LIMIT ?",
                (MAX_CONTEXT + 1,) + params + (MAX_WAIT_SAMPLE,),
            ).fetchall()
            observed = result["observations"]
            observed.update(sampled_jobs=len(sample), truncated=len(sample) < waiting)
            categories = {}
            for reason, encoded in sample:
                try:
                    if not encoded or len(encoded) > MAX_CONTEXT:
                        raise ValueError("Missing or oversized observation")
                    observation = json.loads(encoded)
                    if not isinstance(observation, dict):
                        raise ValueError("Invalid observation")
                    stamp = observation.get("measured_at")
                    if stamp is None:
                        observed["missing_jobs"] += 1
                        continue
                    if type(stamp) not in (int, float) or not math.isfinite(stamp):
                        raise ValueError("Invalid observation timestamp")
                except (ValueError, TypeError, RecursionError, OverflowError):
                    observed["unreadable_jobs"] += 1
                    continue
                if not 0 <= now - stamp <= waits.MAX_GAP_SECONDS:
                    observed["stale_jobs"] += 1
                    continue
                kind = waits.category(reason or "")
                categories[kind] = categories.get(kind, 0) + 1
            observed["fresh"] = [
                dict(category=kind, jobs=count, label=LABELS[kind])
                for kind, count in sorted(
                    categories.items(), key=lambda item: (-item[1], item[0])
                )
            ]
        return finish(result, config)


def finish(result, config):
    waiting = result["waiting_jobs"]
    running = result["reservations"]["jobs"]
    if not result["initialized"]:
        result["summary"] = "Queue is not initialized"
    elif waiting and config["paused"]:
        result.update(
            status="paused",
            summary="Queue paused | {} waiting; running jobs continue".format(waiting),
        )
    elif waiting and result["disk"]["limited_jobs"] == waiting:
        disk = result["disk"]
        result.update(
            status="disk_blocked",
            summary=(
                "All {} waiting jobs need disk space | {:.1f} GiB free / at least {:.1f} required | short by {:.1f} GiB"
            ).format(
                waiting,
                disk["available_gb"],
                rounded_up(disk["required_min_gb"]),
                rounded_up(disk["shortfall_min_gb"]),
            ),
        )
    elif waiting:
        result.update(
            status="busy" if running else "waiting",
            summary="{} waiting | {} running".format(waiting, running),
        )
        reasons = result["observations"]["fresh"][:3]
        if reasons:
            result["summary"] += " | observed: " + ", ".join(
                "{} {}".format(item["jobs"], item["label"]) for item in reasons
            )
        else:
            result["summary"] += " | no fresh runner reasons"
    else:
        result.update(
            status="running" if running else "idle",
            summary="{} running | no jobs waiting".format(running),
        )
    if result["queued_cancellations"]:
        result["summary"] += " | {} awaiting cancellation".format(
            result["queued_cancellations"]
        )
    if result["repository"]:
        result["summary"] += " | running count is global"
    return result


def rounded_up(value):
    """Never show a positive shortfall as zero or recommend too little space."""
    # Ignore byte-scale conversion noise at exact decimal boundaries.
    return max(0.1, math.ceil(value * 10 - 1e-7) / 10) if value > 0 else 0


def banner(result, compact=False):
    if not compact:
        if result["status"] == "disk_blocked":
            disk = result["disk"]
            return "DISK: all {} waiting | +{:.1f} GiB needed | {:.1f} free / {:.1f} required".format(
                result["waiting_jobs"],
                rounded_up(disk["shortfall_min_gb"]),
                disk["available_gb"],
                rounded_up(disk["required_min_gb"]),
            )
        return result["summary"]
    if result["status"] == "disk_blocked":
        return "DISK: +{:.1f} GiB needed".format(
            rounded_up(result["disk"]["shortfall_min_gb"])
        )
    if result["status"] == "paused":
        return "PAUSED: {} waiting".format(result["waiting_jobs"])
    return "{} waiting | {} running".format(
        result["waiting_jobs"], result["reservations"]["jobs"]
    )


def dashboard_banner(result, width, stale=False, status=""):
    """Fit existing observations to the pane; never recompute admission or do I/O."""
    waiting = result["waiting_jobs"]
    running = result["reservations"]["jobs"]
    disk = result.get("disk", {})
    limited = disk.get("limited_jobs")
    if result["status"] == "paused":
        choices = [
            result.get("summary")
            or "PAUSED: {} waiting; running jobs continue".format(waiting),
            "PAUSED: {} waiting".format(waiting),
            "PAUSED W{}".format(waiting),
            "PAUSED",
        ]
    elif result["status"] == "disk_blocked":
        gap = rounded_up(disk["shortfall_min_gb"])
        choices = (
            ["STATE " + banner(result)]
            if disk.get("available_gb") is not None
            and disk.get("required_min_gb") is not None
            else []
        ) + [
            "DISK: +{:.1f} GiB needed".format(gap),
            "DISK: +{:.1f}GiB".format(gap),
            "DISK: +{:.1f}G".format(gap),
            "DISK blocked",
            "DISK",
        ]
    elif limited is not None and 0 < limited < waiting:
        gap = rounded_up(disk["shortfall_max_gb"])
        choices = [
            "STATE DISK: {}/{} waiting limited | +{:.1f} GiB for largest request".format(
                limited, waiting, gap
            ),
            "DISK: {}/{} +{:.1f}G".format(limited, waiting, gap),
            "DISK: {}/{}".format(limited, waiting),
            "DISK: some jobs",
            "DISK",
        ]
    else:
        choices = [
            result.get("summary") or banner(result, compact=True),
            banner(result, compact=True),
            "{} wait | {} run".format(waiting, running),
            "W{} R{}".format(waiting, running),
            "QUEUE",
        ]
    room = max(0, width - 1)
    prefix = "STALE " if stale else ""
    text = next(
        (prefix + item for item in choices if len(prefix + item) <= room),
        "STALE" if stale and room >= 5 else "",
    )
    if text and status and len(text + " | " + status) <= room:
        text += " | " + status
    return text


def lines(result):
    yield result["summary"]
    disk = result["disk"]
    if result["waiting_jobs"] and disk["available_gb"] is None:
        yield "Shared state disk: unknown (" + disk.get("error", "not measured") + ")"
    if result["waiting_jobs"] and disk["available_gb"] is not None:
        yield (
            "Shared state disk: {:.1f} GiB free | waiting requirements {:.1f}-{:.1f} GiB | "
            "{} currently limited by this filesystem"
        ).format(
            disk["available_gb"],
            disk["required_min_gb"],
            disk["required_max_gb"],
            disk["limited_jobs"],
        )
        if disk["shortfall_max_gb"]:
            yield "Shortfall: {:.1f} GiB for the smallest request; {:.1f} GiB for the largest (includes running scratch)".format(
                rounded_up(disk["shortfall_min_gb"]),
                rounded_up(disk["shortfall_max_gb"]),
            )
    observed = result["observations"]
    yield "Runner observations: {} sampled | {} stale | {} missing | {} unreadable{}".format(
        observed["sampled_jobs"],
        observed["stale_jobs"],
        observed["missing_jobs"],
        observed["unreadable_jobs"],
        " | sample truncated" if observed["truncated"] else "",
    )
    if result["waiting_jobs"]:
        yield "Inspect a job with buildr diagnose JOB --json; inspect cache sizes with buildr disk --repo PATH --json."
