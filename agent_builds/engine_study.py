"""Paired native runner comparisons with one identical, frozen Buildr workload."""

import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import signal
import statistics
import subprocess
import uuid
import zipfile

from . import __version__
from .experiments import arm, parent_budget
from .metrics import snapshot
from .plans import write_export
from .source import capture, changed
from .verification import MAX_BYTES, prepare, snapshot as freeze

ROOT = Path(__file__).resolve().parent.parent
# The accepted runner immediately before the telemetry optimization. Fixed by
# commit, not a user-supplied command or a mutable branch/tag.
BASELINE = "c0aaabc0c7abed34d95988ba0c84cd8d9cf746ab"
SAMPLER_INTERVAL = 0.1


def baseline(destination):
    data = subprocess.check_output(
        ["git", "archive", "--format=zip", BASELINE, "agent_builds"], cwd=str(ROOT)
    )
    if len(data) > MAX_BYTES:
        raise ValueError("Baseline runner archive exceeds 8 MiB")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for name in archive.namelist():
            path = PurePosixPath(name)
            if (
                not path.parts
                or path.is_absolute()
                or ".." in path.parts
                or path.parts[0] != "agent_builds"
            ):
                raise ValueError("Invalid baseline runner archive member")
        destination.mkdir(mode=0o700)
        archive.extractall(destination)
    return dict(
        commit=BASELINE, sha256=hashlib.sha256(data).hexdigest(), bytes=len(data)
    )


def summarize(records, pairs, agents, unchanged):
    expected = {
        (pair, engine) for pair in range(pairs) for engine in ("baseline", "candidate")
    }
    valid = (
        unchanged is True
        and len(records) == pairs * 2
        and {(r["pair"], r["engine"]) for r in records} == expected
        and all(
            r["jobs"] == r["verified_jobs"] == agents
            and len(r["results"]) == agents
            and {job["id"] for job in r["results"]}
            == {"agent-" + str(i) for i in range(agents)}
            and all(
                job["verified"] is True
                and type(job["exit_code"]) is int
                and job["exit_code"] == 0
                for job in r["results"]
            )
            for r in records
        )
    )
    if not valid:
        return False, [], None
    comparisons = []
    for pair in range(pairs):
        matched = {r["engine"]: r for r in records if r["pair"] == pair}
        old, new = matched["baseline"], matched["candidate"]
        comparisons.append(
            dict(
                pair=pair,
                candidate_to_baseline_wall_ratio=new["wall_seconds"]
                / old["wall_seconds"],
                verified_throughput_ratio=new["verified_completions_per_second"]
                / old["verified_completions_per_second"],
                candidate_to_baseline_child_cpu_ratio=new["child_cpu_seconds"]
                / old["child_cpu_seconds"]
                if old["child_cpu_seconds"] > 0
                else None,
            )
        )
    keys = (
        "wall_seconds",
        "setup_seconds",
        "end_to_end_seconds",
        "child_cpu_seconds",
        "controller_cpu_seconds",
        "sampled_process_tree_peak_rss_gb",
    )
    summary = {
        engine: {
            key: statistics.median(r[key] for r in records if r["engine"] == engine)
            for key in keys
        }
        for engine in ("baseline", "candidate")
    }
    return True, comparisons, summary


def run(output, pairs=4, agents=4, cpus=2):
    if (
        type(pairs) is not int
        or not 1 <= pairs <= 4
        or type(agents) is not int
        or not 2 <= agents <= 6
    ):
        raise ValueError("Use one to four pairs and two to six workload agents")
    budget = parent_budget(cpus)
    output = Path(output).expanduser().absolute()
    progress = output.with_name(output.name + ".progress.json")
    if (
        any(p.exists() or p.is_symlink() for p in (output, progress))
        or ROOT in output.parents
    ):
        raise ValueError("Use new evidence paths outside the source checkout")
    source = capture(ROOT)
    if source.get("status") != "captured":
        raise ValueError("Complete source identity is required")
    before = snapshot(output.parent)
    directory = output.parent / ("buildr-engine-study-" + uuid.uuid4().hex[:12])
    bundle = directory / "workload.zip"
    workload = freeze(ROOT, bundle)  # Reject dirty source/missing tools before work.
    report = dict(
        schema="buildr-runner-cost-study-v1",
        application_version=__version__,
        result="running",
        pairs=pairs,
        agents=agents,
        source_before=source,
        outer_reservation=budget,
        workload=workload,
        before_machine=before,
        fixture_directory=str(directory),
        records=[],
        comparisons=[],
        summary=None,
        automatic_retries=False,
        observer_sample_interval_seconds=SAMPLER_INTERVAL,
        note="Paired before/after runner comparison. Both private queues execute the identical frozen package/browser/prompt workload with the same interpreter, CPU affinity, allocation and worker hints. The controller is the same candidate code in every arm. Setup and outer queue wait are excluded from execution time. Host activity, caches and sample size affect observations; no autonomous coding productivity is measured.",
    )
    write_export(progress, report)
    affinity = os.sched_getaffinity(0)
    chosen = sorted(affinity)[-cpus:]
    previous, cancelled = {}, [False]

    def interrupted(signum, frame):
        cancelled[0] = True

    try:
        report["baseline"] = baseline(directory / "baseline-engine")
        candidate = directory / "candidate-engine"
        candidate.mkdir(mode=0o700)
        packages = dict(
            baseline=directory / "baseline-engine", candidate=prepare(bundle, candidate)
        )
        os.sched_setaffinity(0, chosen)
        report["cpu_affinity"] = chosen
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, interrupted)
        for pair in range(pairs):
            order = (
                ("baseline", "candidate")
                if pair % 2 == 0
                else ("candidate", "baseline")
            )
            for engine in order:
                if cancelled[0]:
                    raise ValueError("Study interrupted; work is not retried")
                report["active_arm"] = dict(pair=pair, engine=engine)
                record = arm(
                    directory / str(pair) / engine,
                    "buildr_verification",
                    "queued",
                    agents,
                    cpus,
                    cancelled,
                    bundle=bundle,
                    runner_package=packages[engine],
                    sampler_interval=SAMPLER_INTERVAL,
                )
                record.update(pair=pair, engine=engine)
                report["records"].append(record)
                print(
                    "{} pair {}: {}/{} verified, {:.3f}s".format(
                        engine,
                        pair,
                        record["verified_jobs"],
                        record["jobs"],
                        record["wall_seconds"],
                    ),
                    flush=True,
                )
        del report["active_arm"]
        report["source_after"] = capture(ROOT)
        report["source_changed"] = changed(source, report["source_after"])
        valid, comparisons, summary = summarize(
            report["records"], pairs, agents, report["source_changed"] is False
        )
        report.update(
            result="passed" if valid else "failed",
            verification_valid=valid,
            comparisons=comparisons,
            summary=summary,
            after_machine=snapshot(directory),
        )
    except BaseException as error:
        report.update(
            result="failed",
            error=type(error).__name__ + ": " + str(error),
            verification_valid=False,
        )
        raise
    finally:
        try:
            try:
                os.sched_setaffinity(0, affinity)
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
        finally:
            write_export(output, report)
    return report
