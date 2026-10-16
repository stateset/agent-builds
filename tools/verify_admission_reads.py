"""Measure Store admission in private queues with bulky, unused evidence.

Run once through Buildr. No fixture runner or job command is launched. Python
allocation measurements exclude SQLite's native cache and are not host RSS.
"""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc

ROOT = Path(__file__).resolve().parent.parent


def identity(checkout):
    def git(*args):
        return subprocess.check_output(["git"] + list(args), cwd=str(checkout))

    return dict(
        head=git("rev-parse", "HEAD").decode().strip(),
        tracked_diff_sha256=hashlib.sha256(git("diff", "HEAD", "--")).hexdigest(),
        untracked=git("ls-files", "--others", "--exclude-standard")
        .decode()
        .splitlines(),
    )


def worker(args):
    sys.path.insert(0, str(Path(args.checkout).resolve()))
    from agent_builds import __version__
    from agent_builds.store import Store

    store = Store(args.state)
    jobs = json.loads(Path(args.state, "jobs.json").read_text())
    results = {}

    def measure(name, job, expected):
        # Warm SQLite/Python caches, then separate timings from allocation tracing.
        assert store.claim(job) is expected
        samples = []
        for _ in range(8):
            started = time.perf_counter()
            assert store.claim(job) is expected
            samples.append((time.perf_counter() - started) * 1000)
        tracemalloc.start()
        try:
            assert store.claim(job) is expected
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        results[name] = dict(
            samples_ms=samples,
            median_ms=statistics.median(samples),
            peak_python_bytes=peak,
            expected_admitted=expected,
        )

    try:
        measure("paused", jobs[-1], False)
        store.update_config(dict(paused=False))
        measure("priority_wait", jobs[-1], False)
        assert store.claim(jobs[0])
        measure("lock_wait", jobs[1], False)
        assert store.get(jobs[1])["wait_context"]["blocking_job"]["id"] == jobs[0]
        measure("already_running", jobs[0], False)
        counts = store.counts()
        assert counts["running"] == 1 and counts["queued"] == len(jobs) - 1
        assert not any(
            counts[key] for key in ("succeeded", "failed", "cancelled", "lost")
        )
        print(
            json.dumps(
                dict(
                    version=__version__,
                    scenarios=results,
                    fixture_jobs=len(jobs),
                    counts=counts,
                    job_commands_launched=0,
                )
            )
        )
    finally:
        store.close()


def run(args):
    sys.path.insert(0, str(ROOT))
    from agent_builds.experiments import parent_budget
    from agent_builds.store import Store

    budget = parent_budget(1)
    output = Path(args.output).resolve()
    if output.exists():
        raise ValueError("Refusing to overwrite an existing report")
    checkouts = dict(candidate=ROOT)
    if args.baseline_checkout:
        checkouts["baseline"] = Path(args.baseline_checkout).resolve()
    before = {name: identity(path) for name, path in checkouts.items()}
    with tempfile.TemporaryDirectory(prefix="buildr-admission-reads-") as temporary:
        root = Path(temporary)
        template = root / "template"
        store = Store(template)
        try:
            store.update_config(
                dict(
                    paused=True,
                    cpu_slots=2,
                    memory_gb=2,
                    max_running=2,
                    max_heavy=1,
                    min_free_gb=0,
                    min_available_gb=0,
                    quick_reserve_slots=0,
                    quick_reserve_jobs=0,
                    quick_reserve_memory_gb=0,
                    nice=0,
                    aging_seconds=3600,
                )
            )
            evidence = {"unused_fixture_evidence": "x" * 65536}
            jobs = [
                store.submit(
                    root,
                    ["true"],
                    "local",
                    "quick",
                    1,
                    0.25,
                    "output-" + str(index // 2),
                    "",
                    5,
                    source=evidence,
                )
                for index in range(300)
            ]
            # Identical creation ordering across both copies, including ties.
            store.db.executemany(
                "UPDATE jobs SET created=? WHERE id=?",
                [(time.time() + index / 1000, job) for index, job in enumerate(jobs)],
            )
            store.db.execute(
                "UPDATE jobs SET usage=?,batch=?",
                (json.dumps(evidence), json.dumps(evidence)),
            )
        finally:
            store.close()
        results = {}
        # Retain both orders to expose warm-up/order effects, rather than picking a win.
        orders = [list(checkouts), list(reversed(checkouts))]
        for pair, order in enumerate(orders):
            for name in order:
                state = root / (name + str(pair))
                state.mkdir()
                for filename in ("queue.sqlite3", "config.json"):
                    shutil.copyfile(str(template / filename), str(state / filename))
                (state / "jobs.json").write_text(json.dumps(jobs))
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--worker",
                        "--checkout",
                        str(checkouts[name]),
                        "--state",
                        str(state),
                    ],
                    cwd=str(checkouts[name]),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=120,
                    check=True,
                )
                results.setdefault(name, []).append(json.loads(completed.stdout))
        after = {name: identity(path) for name, path in checkouts.items()}
        assert before == after, "Checkout changed during the measurement"
        report = dict(
            schema="buildr-admission-reads-v1",
            result="passed",
            outer_reservation=budget,
            source=before,
            source_changed=False,
            fixture_jobs=300,
            evidence_bytes_per_job=3 * 65536,
            orders=orders,
            measurements=results,
            limitations="Private Store calls with synthetic evidence, not real runner contention or build throughput. Timings include host probes and writes. Peak Python allocations exclude SQLite native memory and are not RSS. No global tuning.",
        )
        # Exclusive creation prevents accidentally replacing earlier evidence.
        with output.open("x") as handle:
            json.dump(report, handle, indent=2)
            handle.write("\n")
        print(json.dumps(dict(result="passed", output=str(output), fixture_jobs=300)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--checkout", help=argparse.SUPPRESS)
    parser.add_argument("--state", help=argparse.SUPPRESS)
    parser.add_argument("--baseline-checkout")
    parser.add_argument("--output")
    options = parser.parse_args()
    if options.worker:
        worker(options)
    elif not options.output:
        parser.error("--output is required")
    else:
        run(options)
