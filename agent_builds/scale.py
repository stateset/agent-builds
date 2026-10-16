"""Explicit, queued process-scale checks using only private synthetic workloads."""

import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def payload(args):
    """A bounded stand-in command: verify hints/locks, record attempts, exit."""
    root = Path(args.fixture)
    descriptors = []
    try:
        assert os.environ["CARGO_BUILD_JOBS"] == os.environ["RUST_TEST_THREADS"] == "1"
        for key in sorted(json.loads(args.keys)):
            name = hashlib.sha256(key.encode()).hexdigest()
            descriptor = os.open(
                str(root / ("lock-" + name)), os.O_CREAT | os.O_RDWR, 0o600
            )
            descriptors.append(descriptor)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def event(kind):
            row = (
                json.dumps(
                    dict(
                        index=args.index,
                        kind=kind,
                        at=time.monotonic(),
                        pid=os.getpid(),
                    )
                )
                + "\n"
            )
            descriptor = os.open(
                str(root / "attempts.jsonl"),
                os.O_CREAT | os.O_WRONLY | os.O_APPEND,
                0o600,
            )
            try:
                data = row.encode()
                assert os.write(descriptor, data) == len(data)
            finally:
                os.close(descriptor)

        event("start")
        time.sleep(0.04)
        event("end")
        return 7 if args.index % 17 == 2 else 0
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def worker(args):
    """Instrument an actual selected-release CLI runner, not a simulated loop."""
    sys.path.insert(0, str(Path(args.checkout).resolve()))
    from agent_builds import __version__
    from agent_builds.store import Store
    from agent_builds.__main__ import main

    counters = dict(
        full_job_reads=0,
        control_reads=0,
        claims=0,
        recovery_sweeps=0,
        write_transactions=0,
        config_file_reads=0,
        busy_admissions=0,
    )
    output = Path(args.metrics)

    def snapshot(exit_code=None):
        now = time.monotonic()
        write_json(
            output,
            dict(
                counters,
                cpu_seconds=time.process_time(),
                at=now,
                version=__version__,
                exit_code=exit_code,
            ),
        )

    def observed(method, field):
        def call(self, *values, **options):
            if field != "recovery_sweeps":
                counters[field] += 1
            result = method(self, *values, **options)
            if field == "recovery_sweeps":
                counters[field] += 1  # completed sweeps, excluding a busy BEGIN
            return result

        return call

    for method, field in (
        ("get", "full_job_reads"),
        ("get_control", "control_reads"),
        ("claim", "claims"),
        ("recover", "recovery_sweeps"),
    ):
        if hasattr(Store, method):
            setattr(Store, method, observed(getattr(Store, method), field))
    transaction, read_text = Store.transaction, Path.read_text

    @contextlib.contextmanager
    def observed_transaction(self, *values, **options):
        with transaction(self, *values, **options):
            counters["write_transactions"] += 1
            yield

    if hasattr(Store, "poll_claim"):
        poll_claim = Store.poll_claim

        def observed_poll_claim(self, *values, **options):
            result = poll_claim(self, *values, **options)
            if result is None:
                counters["busy_admissions"] += 1
            return result

        Store.poll_claim = observed_poll_claim

    def read(path, *values, **options):
        if path == Path(os.environ["AGENT_BUILDS_STATE"]) / "config.json":
            counters["config_file_reads"] += 1
        return read_text(path, *values, **options)

    Store.transaction, Path.read_text = observed_transaction, read
    sys.argv = ["buildr"] + args.command
    # Sample independently of SQLite calls: a blocked claim must not leave a
    # stale starting CPU snapshot that includes startup in the idle interval.
    stop = threading.Event()
    errors = []

    def sample():
        try:
            while not stop.wait(0.2):
                snapshot()
        except BaseException as error:
            errors.append(error)

    snapshot()
    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    code = 1
    try:
        code = main()
        return code
    finally:
        stop.set()
        sampler.join()
        snapshot(exit_code=code)
        if errors:
            raise RuntimeError("Runner metrics sampling failed") from errors[0]


def metrics(root, agents):
    values = {}
    for index in range(agents):
        path = root / ("metrics-" + str(index) + ".json")
        if path.exists():
            values[index] = json.loads(path.read_text())
    return values


def idle_delta(before, after):
    if before.keys() != after.keys():
        raise AssertionError("Idle measurement has incomplete runner coverage")
    fields = (
        "cpu_seconds",
        "full_job_reads",
        "control_reads",
        "claims",
        "recovery_sweeps",
        "write_transactions",
        "config_file_reads",
        "busy_admissions",
    )
    spans = [after[key]["at"] - before[key]["at"] for key in before]
    if not spans or min(spans) <= 0:
        raise AssertionError("Idle measurement has stale runner snapshots")
    return dict(
        coverage=len(before),
        sample_span_min_seconds=min(spans),
        sample_span_max_seconds=max(spans),
        cpu_cores_observed=sum(
            (after[key]["cpu_seconds"] - before[key]["cpu_seconds"])
            / (after[key]["at"] - before[key]["at"])
            for key in before
        ),
        **{
            field: sum(after[key][field] - before[key][field] for key in before)
            for field in fields
        },
    )


def check_events(events, agents):
    expected = set(range(2, agents))
    active, started, ended, peak = set(), set(), set(), 0

    def keys(index):
        return {"repo-" + str(index % 8), "cache-" + str(index % 5)}

    for event in sorted(events, key=lambda row: row["at"]):
        index = event["index"]
        assert index in expected, "A cancelled or lost command executed"
        if event["kind"] == "start":
            assert index not in started, "A command executed more than once"
            assert all(not keys(index).intersection(keys(other)) for other in active), (
                "Native lock overlap"
            )
            started.add(index)
            active.add(index)
            peak = max(peak, len(active))
            assert len(active) <= 2 and sum(other % 4 == 0 for other in active) <= 1, (
                "Native concurrency exceeded reservation budgets"
            )
        else:
            assert event["kind"] == "end" and index in active and index not in ended, (
                "Invalid native completion ledger"
            )
            active.remove(index)
            ended.add(index)
    assert not active and started == ended == expected, (
        "Missing native attempt or completion"
    )
    return peak


def observations(store):
    # Test-only trigger records every reservation transition atomically, so
    # short budget or lock violations cannot disappear between samples.
    store.db.execute(
        "CREATE TABLE scale_admissions (job_id TEXT, jobs INTEGER, slots INTEGER, memory REAL, heavy INTEGER, heavy_slots INTEGER, conflicts INTEGER)"
    )
    store.db.execute("""CREATE TRIGGER scale_observe AFTER UPDATE OF state ON jobs
        WHEN NEW.state='running' AND OLD.state!='running' BEGIN
        INSERT INTO scale_admissions SELECT NEW.id,
        (SELECT COUNT(*) FROM jobs WHERE state IN ('running','cancelling')),
        (SELECT COALESCE(SUM(slots),0) FROM jobs WHERE state IN ('running','cancelling')),
        (SELECT COALESCE(SUM(memory_gb),0) FROM jobs WHERE state IN ('running','cancelling')),
        (SELECT COUNT(*) FROM jobs WHERE state IN ('running','cancelling') AND lane='heavy'),
        (SELECT COALESCE(SUM(slots),0) FROM jobs WHERE state IN ('running','cancelling') AND lane='heavy'),
        (SELECT COUNT(*) FROM job_locks a JOIN job_locks b ON a.resource_key=b.resource_key AND a.job_id<b.job_id
         JOIN jobs ja ON ja.id=a.job_id JOIN jobs jb ON jb.id=b.job_id
         WHERE ja.state IN ('running','cancelling') AND jb.state IN ('running','cancelling'));
        END""")


def wait_until(condition, children, deadline, description):
    while time.monotonic() < deadline:
        if condition():
            return
        if any(child.poll() is not None for child in children):
            raise AssertionError("A runner exited before " + description)
        time.sleep(0.05)
    raise TimeoutError("Timed out waiting for " + description)


def measure(args, checkout, root):
    from agent_builds.store import Store, alive
    from agent_builds.runner import cancel_job
    from agent_builds.source import capture, changed
    from agent_builds.queue_health import report as queue_report

    before_source = capture(checkout)
    root.mkdir(mode=0o700)
    state = root / "queue"
    store = Store(state)
    children, streams, receipts, backlog = [], [], [], []
    try:
        store.update_config(
            dict(
                cpu_slots=2,
                max_running=2,
                max_heavy=1,
                memory_gb=2,
                quick_jobs=1,
                heavy_jobs=1,
                quick_reserve_slots=1,
                quick_reserve_jobs=1,
                quick_reserve_memory_gb=0.25,
                min_free_gb=0,
                min_available_gb=0,
                nice=0,
                paused=True,
                protected_quick=True,
                aging_seconds=2,
            )
        )
        for index in range(args.history):
            job = store.submit(
                root, ["true"], "local", "quick", 1, 0.1, "history-" + str(index), "", 5
            )
            store.finish(
                job, "succeeded", 0, "synthetic historical record; no native execution"
            )
        observations(store)
        env = dict(
            os.environ,
            AGENT_BUILDS_STATE=str(state),
            PYTHONPATH=str(checkout),
            BUILDR_ALLOWED_ROOTS=json.dumps([str(root)]),
        )
        env.pop("AGENT_BUILDS_JOB_ID", None)  # private fixture only
        started = time.monotonic()
        for index in range(args.agents):
            receipt = state / ("receipt-scale-" + str(index) + ".json")
            receipts.append(receipt)
            keys = ["repo-" + str(index % 8), "cache-" + str(index % 5)]
            native = [
                sys.executable,
                "-I",
                str(Path(__file__).resolve()),
                "payload",
                "--fixture",
                str(root),
                "--index",
                str(index),
                "--keys",
                json.dumps(keys),
            ]
            command = [
                "run",
                "--cwd",
                str(root),
                "--resource-key",
                keys[0],
                "--lock",
                keys[1],
                "--lane",
                "heavy" if index % 4 == 0 else "quick",
                "--jobs",
                "1",
                "--memory-gb",
                ".1",
                "--timeout",
                "15",
                "--receipt-file",
                str(receipt),
                "--",
            ] + native
            log = (root / ("runner-" + str(index) + ".log")).open("wb")
            streams.append(log)
            children.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-I",
                        str(Path(__file__).resolve()),
                        "worker",
                        "--checkout",
                        str(checkout),
                        "--metrics",
                        str(root / ("metrics-" + str(index) + ".json")),
                        "--",
                    ]
                    + command,
                    env=env,
                    cwd=str(checkout),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                )
            )
        wait_until(
            lambda: (
                all(path.exists() for path in receipts)
                and len(metrics(root, args.agents)) == args.agents
            ),
            children,
            time.monotonic() + 120,
            "all paused runner receipts and metrics",
        )
        handles = [json.loads(path.read_text())["job_id"] for path in receipts]
        for index in range(args.backlog):
            backlog.append(
                store.submit(
                    root,
                    ["true"],
                    "local",
                    "heavy",
                    1,
                    0.1,
                    "backlog-" + str(index),
                    "",
                    5,
                )
            )
        peak_queued = store.counts()["queued"]
        assert peak_queued == args.agents + args.backlog, (
            "Paused runner exited before measurement; inspect retained runner logs",
            store.counts(),
        )
        before = metrics(root, args.agents)
        probe_seconds = []
        hold_start = time.monotonic()
        while time.monotonic() - hold_start < args.hold:
            probe_start = time.monotonic()
            reader = Store(state, read_only=True)
            try:
                assert reader.counts()["queued"] == peak_queued
                assert len(reader.rows(limit=5, active=True)) == min(5, peak_queued)
                health = queue_report(reader)
                assert (
                    health["waiting_jobs"] == peak_queued
                    and health["status"] == "paused"
                )
                assert health["observations"]["sampled_jobs"] <= 200
            finally:
                reader.close()
            probe_seconds.append(time.monotonic() - probe_start)
            time.sleep(0.2)
        idle = idle_delta(before, metrics(root, args.agents))
        idle["wall_seconds"] = time.monotonic() - hold_start
        # Exercise cancellation and recovery only while these two commands are
        # known queued, with no native child. Never retry the killed runner.
        assert all(
            store.get_control(job)["state"] in ("queued", "held") for job in handles
        )
        cancel_job(store, handles[0])
        assert children[0].wait(timeout=15) == 130
        assert store.get(handles[0])["child_pid"] is None
        assert store.get(handles[1])["child_pid"] is None
        children[1].kill()
        assert children[1].wait(timeout=5) == -9
        store.recover()
        assert store.get(handles[1])["state"] == "lost"
        resumed = time.monotonic()
        store.update_config(dict(paused=False))
        deadline = resumed + 180
        while any(child.poll() is None for child in children):
            if time.monotonic() > deadline:
                raise TimeoutError(
                    "Real runners did not drain; no attempts were retried"
                )
            time.sleep(0.05)
        events = [
            json.loads(line)
            for line in (root / "attempts.jsonl").read_text().splitlines()
        ]
        expected = set(range(2, args.agents))
        native_peak = check_events(events, args.agents)
        starts = [event["index"] for event in events if event["kind"] == "start"]
        ends = [event["index"] for event in events if event["kind"] == "end"]
        assert set(starts) == set(ends) == expected and len(starts) == len(ends) == len(
            expected
        )
        for index in expected:
            row = store.get(handles[index])
            code = 7 if index % 17 == 2 else 0
            assert row["exit_code"] == children[index].returncode == code, (
                index,
                row["state"],
                row["exit_code"],
            )
            assert row["state"] == ("failed" if code else "succeeded")
        admitted = [
            dict(row) for row in store.db.execute("SELECT * FROM scale_admissions")
        ]
        assert len(admitted) == len(expected) and {
            row["job_id"] for row in admitted
        } == {handles[index] for index in expected}
        assert all(
            row["jobs"] <= 2
            and row["slots"] <= 2
            and row["memory"] <= 2
            and row["heavy"] <= 1
            and row["heavy_slots"] <= 1
            and row["conflicts"] == 0
            for row in admitted
        ), admitted
        assert all(
            store.get_control(job)["state"] == "queued"
            and store.get(job)["child_pid"] is None
            for job in backlog
        )
        for job in backlog:
            store.cancel(job)
            store.finish(job, "cancelled", 130, "synthetic backlog never launched")
        after_source = capture(checkout)
        stable = changed(before_source, after_source)
        assert stable is False, "Source changed or unavailable; comparison not accepted"
        return dict(
            checkout=str(checkout),
            source=before_source,
            source_changed=False,
            actual_runner_processes=args.agents,
            peak_queued=peak_queued,
            synthetic_backlog=args.backlog,
            synthetic_history=args.history,
            idle=idle,
            inspection_p95_seconds=sorted(probe_seconds)[
                max(0, math.ceil(len(probe_seconds) * 0.95) - 1)
            ],
            inspection_includes_queue_health=True,
            native_attempts=len(starts),
            peak_native_commands=native_peak,
            runner_versions=sorted({value["version"] for value in before.values()}),
            native_outcomes_verified=True,
            expected_failures=sum(index % 17 == 2 for index in expected),
            queued_cancel_exit=130,
            killed_runner_state="lost",
            unknown_attempts_retried=0,
            peak_reserved_jobs=max(row["jobs"] for row in admitted),
            peak_reserved_slots=max(row["slots"] for row in admitted),
            lock_conflicts=0,
            drain_seconds=time.monotonic() - resumed,
            total_seconds=time.monotonic() - started,
            metrics_coverage=len(before),
            ledger=events,
        )
    finally:
        # Stop only fixture runners. If any native orphan remains uncertain,
        # keep its private state for inspection instead of deleting evidence.
        for child in children:
            if child.poll() is None:
                child.terminate()
        uncertain = []
        for index, child in enumerate(children):
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
            if index < len(receipts) and receipts[index].exists():
                job = json.loads(receipts[index].read_text())["job_id"]
                row = store.get(job)
                if row["state"] not in ("succeeded", "failed", "cancelled", "lost"):
                    cancel_job(store, job)
                deadline = time.monotonic() + 3
                while (
                    alive(row["child_pid"], row["child_start"])
                    and time.monotonic() < deadline
                ):
                    time.sleep(0.02)
                if alive(row["child_pid"], row["child_start"]):
                    uncertain.append(job)
        for stream in streams:
            stream.close()
        store.close()
        if uncertain:
            raise RuntimeError(
                "Fixture native outcomes remain uncertain; state retained at "
                + str(root)
            )


def run(args):
    from .experiments import parent_budget
    from .plans import write_export

    parent = parent_budget(2)
    if (
        not 4 <= args.agents <= 100
        or not 0 <= args.backlog <= 500
        or not 0 <= args.history <= 10000
        or not 2 <= args.hold <= 20
    ):
        raise ValueError(
            "Use 4–100 agents, up to 500 backlog/10000 history records and 2–20 idle seconds"
        )
    required_memory = 2 if args.agents <= 30 else 4
    if parent["memory_gb"] < required_memory:
        raise ValueError(
            "This process count requires an outer reservation of at least {} GiB".format(
                required_memory
            )
        )
    output = Path(args.output).expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("Choose a new evidence output path")
    checkout = Path(__file__).resolve().parent.parent
    baseline = (
        Path(args.baseline_checkout).expanduser().resolve()
        if args.baseline_checkout
        else None
    )
    if baseline and not (baseline / "agent_builds/store.py").is_file():
        raise ValueError("Baseline must be an existing Buildr source checkout")
    root = Path(tempfile.mkdtemp(prefix="buildr-process-scale-"))
    report = dict(
        schema="buildr-process-scale-v1",
        parent_reservation=parent,
        fixture_directory=str(root),
        limitations="Synthetic commands and metadata backlog, not coding agents or real builds. "
        "Idle metrics include instrumentation; one ordered comparison is not a statistical productivity study.",
        global_configuration_changed=False,
        automatic_retries=False,
    )
    try:
        if baseline:
            report["baseline"] = measure(args, baseline, root / "baseline")
        report["candidate"] = measure(args, checkout, root / "candidate")
    except BaseException as error:
        report.update(
            result="failed",
            error=type(error).__name__ + ": " + str(error),
            fixture_retained=True,
        )
        write_export(output, report)
        raise
    report["result"] = "passed"
    write_export(output, report)
    shutil.rmtree(str(root))  # only this newly created, fully settled fixture
    print(
        json.dumps(
            {
                key: value
                for key, value in report.items()
                if key not in ("baseline", "candidate")
            },
            indent=2,
        )
    )
    print(
        json.dumps(
            {
                name: {
                    key: value
                    for key, value in arm.items()
                    if key not in ("source", "ledger")
                }
                for name, arm in report.items()
                if name in ("baseline", "candidate")
            },
            indent=2,
        )
    )
    return report


def add_parser(sub):
    command = sub.add_parser(
        "benchmark", help="Explicit private-queue load checks; run through Buildr once"
    )
    commands = command.add_subparsers(dest="benchmark_action", required=True)
    scale = commands.add_parser(
        "scale", help="Exercise real runners, locks, budgets, cancellation and recovery"
    )
    scale.add_argument("--agents", type=int, default=30)
    scale.add_argument("--backlog", type=int, default=60)
    scale.add_argument("--history", type=int, default=1000)
    scale.add_argument("--hold", type=float, default=4)
    scale.add_argument("--baseline-checkout")
    scale.add_argument("--output", required=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    work = commands.add_parser("worker")
    work.add_argument("--checkout", required=True)
    work.add_argument("--metrics", required=True)
    work.add_argument("command", nargs=argparse.REMAINDER)
    native = commands.add_parser("payload")
    native.add_argument("--fixture", required=True)
    native.add_argument("--index", type=int, required=True)
    native.add_argument("--keys", required=True)
    args = parser.parse_args()
    if args.action == "worker":
        if args.command and args.command[0] == "--":
            args.command.pop(0)
        return worker(args)
    return payload(args)


if __name__ == "__main__":
    sys.exit(main())
