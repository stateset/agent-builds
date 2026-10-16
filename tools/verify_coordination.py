"""Compare paused admission overhead in private queues; never executes job argv.

Run this driver once through Buildr. Workers simulate waiter loops using the real
Store methods: multiple connections per process, not 100 independent agent OS
processes. Results measure coordination overhead, not build/coding throughput.
"""

import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time


def worker(args):
    from agent_builds.store import Store

    counts = dict(
        open_write_transactions=0,
        polling_write_transactions=0,
        config_file_reads=0,
        claims=0,
        recovery_sweeps=0,
    )
    phase = ["open"]
    connect, read_text = sqlite3.connect, Path.read_text

    def trace(sql):
        if sql.startswith("BEGIN IMMEDIATE"):
            counts[phase[0] + "_write_transactions"] += 1

    def observed_connect(*values, **options):
        db = connect(*values, **options)
        db.set_trace_callback(trace)
        return db

    def observed_read(path, *values, **options):
        if path == Path(args.state) / "config.json":
            counts["config_file_reads"] += 1
        return read_text(path, *values, **options)

    sqlite3.connect, Path.read_text = observed_connect, observed_read
    stores = []
    try:
        jobs = json.loads(read_text(Path(args.state) / "benchmark-jobs.json"))
        jobs = jobs[args.index :: args.processes]
        stores = [Store(args.state) for _ in jobs]
        if args.mode == "current":
            from agent_builds.polling import AdmissionPoll

            polls = [AdmissionPoll(store) for store in stores]
        else:
            polls = [None for _ in stores]
        Path(args.state, "ready-" + str(args.index)).touch()
        deadline = time.monotonic() + 60
        while not Path(args.state, "go").exists():
            if time.monotonic() > deadline:
                raise RuntimeError("Benchmark start barrier timed out")
            time.sleep(0.01)
        phase[0] = "polling"
        started = time.monotonic()
        for _ in range(args.cycles):
            for store, poll, job in zip(stores, polls, jobs):
                if poll is None:
                    store.recover()
                    counts["recovery_sweeps"] += 1
                elif store.recover_if_due():
                    counts["recovery_sweeps"] += 1
                if poll is None or poll.due():
                    counts["claims"] += 1
                    if store.claim(job):
                        raise RuntimeError("Paused fixture unexpectedly admitted a job")
            time.sleep(0.3)
        counts["elapsed_seconds"] = time.monotonic() - started
        print(json.dumps(counts))
    finally:
        for store in stores:
            store.close()
        sqlite3.connect, Path.read_text = connect, read_text


def measure(args, checkout, mode, root):
    from agent_builds.store import Store

    state = root / mode
    store = Store(state)
    children = []
    try:
        store.update_config(
            dict(
                paused=True,
                cpu_slots=2,
                memory_gb=4,
                max_running=2,
                max_heavy=1,
                min_free_gb=0,
                min_available_gb=0,
                quick_reserve_slots=0,
                quick_reserve_jobs=0,
                quick_reserve_memory_gb=0,
                nice=0,
            )
        )
        jobs = [
            store.submit(
                root, ["true"], "local", "quick", 1, 0.25, str(root / str(index)), "", 5
            )
            for index in range(args.waiters)
        ]
        (state / "benchmark-jobs.json").write_text(json.dumps(jobs))
        env = dict(os.environ, PYTHONPATH=str(checkout), AGENT_BUILDS_STATE=str(state))
        env.pop("AGENT_BUILDS_JOB_ID", None)  # private paused fixture only
        for index in range(args.processes):
            children.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--worker",
                        "--mode",
                        mode,
                        "--state",
                        str(state),
                        "--index",
                        str(index),
                        "--processes",
                        str(args.processes),
                        "--cycles",
                        str(args.cycles),
                    ],
                    cwd=str(checkout),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        deadline = time.monotonic() + 60
        while len(list(state.glob("ready-*"))) != args.processes:
            if (
                any(child.poll() is not None for child in children)
                or time.monotonic() > deadline
            ):
                raise RuntimeError("Worker failed before benchmark start")
            time.sleep(0.01)
        (state / "go").touch()
        results = []
        for child in children:
            output, errors = child.communicate(timeout=120)
            if child.returncode:
                raise RuntimeError("Benchmark worker failed: " + errors[-2000:])
            results.append(json.loads(output))
        rows = store.rows()
        if len(rows) != args.waiters or any(
            row["state"] != "queued" or row["child_pid"] for row in rows
        ):
            raise RuntimeError("Benchmark altered a native job outcome")
        result = {
            key: sum(value[key] for value in results)
            for key in results[0]
            if key != "elapsed_seconds"
        }
        result["elapsed_seconds"] = max(value["elapsed_seconds"] for value in results)
        result["jobs_remained_queued"] = len(rows)
        return result
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.communicate()
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-checkout", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--waiters", type=int, default=100)
    parser.add_argument("--processes", type=int, default=4)
    parser.add_argument("--cycles", type=int, default=8)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--mode", choices=["legacy", "current"])
    parser.add_argument("--state")
    parser.add_argument("--index", type=int)
    args = parser.parse_args()
    if args.worker:
        worker(args)
        return 0
    if not os.environ.get("AGENT_BUILDS_JOB_ID"):
        parser.error("Run this driver once through Buildr")
    if (
        not args.baseline_checkout
        or not (args.baseline_checkout / "agent_builds/store.py").is_file()
    ):
        parser.error("--baseline-checkout must contain the prior Buildr release")
    if (
        not 1 <= args.processes <= 8
        or not args.processes <= args.waiters <= 200
        or not 2 <= args.cycles <= 20
    ):
        parser.error("Use 1–8 processes, up to 200 waiters and 2–20 cycles")
    checkout = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(checkout))
    with tempfile.TemporaryDirectory(prefix="buildr-coordination-") as directory:
        root = Path(directory)
        baseline = measure(args, args.baseline_checkout.resolve(), "legacy", root)
        current = measure(args, checkout, "current", root)
    result = dict(
        waiters=args.waiters,
        processes=args.processes,
        cycles=args.cycles,
        baseline=baseline,
        current=current,
        limitations="Paused synthetic wait loops; several connections per process. "
        "No builds execute. Counts do not establish end-to-end agent productivity or 100-process capacity.",
    )
    if args.output:
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
