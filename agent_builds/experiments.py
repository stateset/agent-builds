"""Bounded, paired local experiments. Fixture results are not agent-productivity claims."""

import hashlib
import json
import math
import os
from pathlib import Path
import resource
import signal
import subprocess
import sys
import time
import uuid

from .metrics import snapshot
from .plans import write_export
from .store import Store, identity
from .telemetry import JobSampler
from . import __version__

SCENARIOS = ("shared_output", "resource_contention", "python_verification")
VERIFICATION_SCENARIOS = ("python_verification", "buildr_verification")
SUPPORTED_SCENARIOS = SCENARIOS + ("buildr_verification",)
NOTE = (
    "Controlled local benchmark with fixed fixtures, not a measurement of autonomous coding productivity. "
    "All arms inherit the same CPU affinity; worker hints follow the recorded strategy. The independent arm bypasses only the isolated "
    "inner scheduler; the entire experiment remains inside its outer shared-queue reservation. "
    "Host background activity, caches and sample size can affect results. No historical command is executed."
)


def parent_budget(cpus):
    if type(cpus) is not int or cpus < 1 or cpus > 6:
        raise ValueError("Experiment CPU budget must be between 1 and 6")
    handle = os.environ.get("AGENT_BUILDS_JOB_ID")
    if not handle:
        raise ValueError(
            "Run the whole experiment through the shared queue with a matching --jobs reservation"
        )
    reader = Store(read_only=True)
    try:
        row = reader.get(handle)
    finally:
        reader.close()
    if row["state"] != "running" or row["slots"] < cpus or row["memory_gb"] < 2:
        raise ValueError(
            "Experiment requires a running outer reservation of the requested CPUs and at least 2 GiB"
        )
    if not hasattr(os, "sched_getaffinity") or len(os.sched_getaffinity(0)) < cpus:
        raise ValueError(
            "Experiment requires Linux CPU affinity and enough available CPUs"
        )
    return dict(job_id=handle, slots=row["slots"], memory_gb=row["memory_gb"])


def worker(scenario, path, identifier):
    if scenario == "buildr_verification":
        from .verification import run_worker

        return run_worker(path, identifier)
    root = Path(path)
    marker = root / "shared-output.txt"
    if scenario == "shared_output":
        marker.write_text(identifier)
    data = (
        bytearray(96 * 1024**2)
        if scenario == "resource_contention"
        else bytearray(65536)
    )
    digest = hashlib.sha256()
    for _ in range(2048):
        digest.update(memoryview(data)[:65536])
    time.sleep(0.2)
    ok = scenario != "shared_output" or marker.read_text() == identifier
    result = dict(id=identifier, verified=ok, digest=digest.hexdigest())
    report = root / ("result-" + identifier + ".json")
    report.write_text(json.dumps(result) + "\n")
    report.chmod(0o600)
    return 0 if ok else 7


def percentile(values):
    return sorted(values)[math.ceil(len(values) * 0.95) - 1] if values else None


def arm(
    directory,
    scenario,
    mode,
    agents,
    cpus,
    cancelled,
    bundle=None,
    runner_package=None,
    sampler_interval=0.02,
):
    if (
        type(sampler_interval) not in (int, float)
        or not math.isfinite(sampler_interval)
        or not 0.02 <= sampler_interval <= 1
    ):
        raise ValueError("Sampler interval must be between 0.02 and 1 second")
    if runner_package is not None and (
        scenario != "buildr_verification" or mode != "queued"
    ):
        raise ValueError(
            "Separate runner packages require the fixed queued Buildr workload"
        )
    setup_started = time.monotonic()
    directory.mkdir(parents=True, mode=0o700)
    state = directory / "queue"
    source = Path(__file__).resolve().parent.parent
    base_env = dict(
        os.environ,
        PYTHONPATH=str(source) + os.pathsep + os.environ.get("PYTHONPATH", ""),
        CARGO_BUILD_JOBS="1",
        RUST_TEST_THREADS="1",
        OMP_NUM_THREADS="1",
    )
    batched = mode in ("serial_batch", "parallel_batch")
    if batched and scenario not in VERIFICATION_SCENARIOS:
        raise ValueError(
            "Batched experiment arms support only the independent Python verification fixture"
        )
    if mode == "queued" or batched:
        queue = Store(state)
        queue.update_config(
            dict(
                cpu_slots=cpus,
                max_running=cpus,
                max_heavy=cpus,
                memory_gb=2,
                quick_reserve_slots=0,
                quick_reserve_jobs=0,
                quick_reserve_memory_gb=0,
                min_free_gb=0,
                min_available_gb=0,
                nice=0,
            )
        )
        queue.close()
    workload_commands = []
    if scenario == "buildr_verification":
        from .verification import prepare

        for index in range(agents):
            identifier = "agent-" + str(index)
            work = directory / identifier
            work.mkdir(mode=0o700)
            prepare(bundle, work)
            workload_commands.append(
                [
                    sys.executable,
                    "-m",
                    "agent_builds.experiments",
                    "_worker",
                    scenario,
                    str(work),
                    identifier,
                ]
            )
    setup_seconds = time.monotonic() - setup_started
    processes = []
    results = []
    sampler = JobSampler(os.getpid(), identity(os.getpid()))
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    controller_cpu_started = time.process_time()
    wall_started = time.time()
    started = time.monotonic()
    deadline = started + (240 if scenario == "buildr_verification" else 120)
    last_tick = started
    next_sample = started
    lag = []
    try:
        for index in range(1 if batched else agents):
            identifier = "agent-" + str(index)
            work = directory / ("shared" if scenario == "shared_output" else identifier)
            work.mkdir(exist_ok=True, mode=0o700)
            command = [
                sys.executable,
                "-m",
                "agent_builds.experiments",
                "_worker",
                scenario,
                str(work),
                identifier,
            ]
            if scenario == "python_verification":
                command = [
                    sys.executable,
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    str(source / "tests"),
                    "-p",
                    "test_budgets.py",
                    "-v",
                ]
            elif scenario == "buildr_verification":
                command = workload_commands[index]
                if runner_package is not None:
                    from .verification import worker_command

                    command = worker_command(work, identifier)
            env = dict(base_env)
            if scenario == "buildr_verification":
                env["PYTHONPATH"] = str(runner_package or work / "checkout")
            if mode == "queued":
                # This is an explicit isolated scheduler experiment, not a
                # nested submission to the production queue.
                env.pop("AGENT_BUILDS_JOB_ID", None)
                env["AGENT_BUILDS_STATE"] = str(state)
                command = [
                    sys.executable,
                    "-m",
                    "agent_builds",
                    "run",
                    "--cwd",
                    str(work),
                    "--lane",
                    "quick",
                    "--jobs",
                    "1",
                    "--memory-gb",
                    ".25",
                    "--resource-key",
                    str(work),
                    "--timeout",
                    str(210 if workload_commands else 90),
                    "--",
                ] + command
            if batched:
                env.pop("AGENT_BUILDS_JOB_ID", None)  # this private fixture queue only
                env["AGENT_BUILDS_STATE"] = str(state)
                manifest = directory / "checks.json"
                manifest.write_text(
                    json.dumps(
                        dict(
                            max_parallel=cpus if mode == "parallel_batch" else 1,
                            checks=[
                                dict(
                                    name="agent-" + str(i),
                                    command=workload_commands[i]
                                    if workload_commands
                                    else command,
                                )
                                for i in range(agents)
                            ],
                        )
                    )
                )
                manifest.chmod(0o600)
                command = (
                    [sys.executable, "-m", "agent_builds"]
                    if workload_commands
                    else [sys.executable, str(source / "buildr")]
                ) + [
                    "batch",
                    "run",
                    str(manifest),
                    "--cwd",
                    str(work),
                    "--jobs",
                    str(cpus),
                    "--memory-gb",
                    "2",
                    "--timeout",
                    str(210 if workload_commands else 90),
                ]
            log = directory / (identifier + ".log")
            with log.open("xb") as output:
                log.chmod(0o600)
                child = subprocess.Popen(
                    command,
                    cwd=str(work),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=output,
                    start_new_session=True,
                )
            processes.append(dict(id=identifier, child=child, work=work, log=log))
        while processes:
            now = time.monotonic()
            lag.append(max(0, now - last_tick - 0.02))
            last_tick = now
            if now >= next_sample:
                sampler.sample()
                next_sample = time.monotonic() + sampler_interval
            for item in list(processes):
                code = item["child"].poll()
                if code is None:
                    continue
                verified = code == 0
                verification = None
                verification_error = None
                if scenario == "buildr_verification" and not batched:
                    from .verification import read_result, verify

                    try:
                        verification = read_result(item["work"], item["id"])
                        verify(item["work"], item["id"], verification)
                    except (OSError, ValueError, KeyError) as error:
                        verified = False
                        verification_error = str(error)
                if scenario not in VERIFICATION_SCENARIOS:
                    try:
                        record = json.loads(
                            (
                                item["work"] / ("result-" + item["id"] + ".json")
                            ).read_text()
                        )
                        verified = (
                            verified
                            and record["verified"] is True
                            and record["id"] == item["id"]
                        )
                    except (OSError, ValueError, KeyError):
                        verified = False
                if batched:
                    reader = Store(state, read_only=True)
                    try:
                        rows = reader.rows(limit=2)
                    finally:
                        reader.close()
                    if len(rows) != 1:
                        raise ValueError(
                            "Batch fixture has no unique queue receipt; work was not retried"
                        )
                    job = rows[0]
                    checks = job["batch"].get("checks", [])
                    if len(checks) != agents:
                        raise ValueError(
                            "Batch fixture did not retain every check result; work was not retried"
                        )
                    for check in checks:
                        check_verified = (
                            check["state"] == "succeeded" and check["exit_code"] == 0
                        )
                        check_evidence = None
                        check_error = None
                        if scenario == "buildr_verification":
                            from .verification import read_result, verify

                            try:
                                check_evidence = read_result(
                                    directory / check["name"], check["name"]
                                )
                                verify(
                                    directory / check["name"],
                                    check["name"],
                                    check_evidence,
                                )
                            except (OSError, ValueError, KeyError) as error:
                                check_verified = False
                                check_error = str(error)
                        results.append(
                            dict(
                                id=check["name"],
                                exit_code=check["exit_code"],
                                verified=check_verified,
                                verification=check_evidence,
                                verification_error=check_error,
                                completion_seconds=max(
                                    0, check.get("finished", time.time()) - wall_started
                                ),
                                log_path=str(item["log"]),
                                wrapper_exit_code=code,
                                queue_job_id=job["id"],
                                slots_per_check=job["batch"]["slots_per_check"],
                            )
                        )
                else:
                    results.append(
                        dict(
                            id=item["id"],
                            exit_code=code,
                            verified=verified,
                            completion_seconds=now - started,
                            log_path=str(item["log"]),
                            verification=verification,
                            verification_error=verification_error,
                        )
                    )
                processes.remove(item)
            if cancelled[0] or now >= deadline:
                raise ValueError(
                    "Experiment cancelled or timed out; incomplete work is not retried"
                )
            if processes:
                time.sleep(0.02)
    finally:
        for item in processes:
            try:
                os.killpg(item["child"].pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        # Fixture children exit on SIGTERM; let isolated runners settle their
        # command groups. No global queue job is cancelled by this driver.
        for item in processes:
            try:
                item["child"].wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(item["child"].pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                item["child"].wait()
    ended = time.monotonic()
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    verified = [r for r in results if r["verified"]]
    return dict(
        mode=mode,
        scenario=scenario,
        wall_seconds=ended - started,
        setup_seconds=setup_seconds,
        end_to_end_seconds=ended - setup_started,
        jobs=len(results),
        verified_jobs=len(verified),
        failed_jobs=len(results) - len(verified),
        verified_completions_per_second=len(verified) / (ended - started),
        completion_p95_seconds=percentile([r["completion_seconds"] for r in results]),
        verified_completion_p95_seconds=percentile(
            [r["completion_seconds"] for r in verified]
        ),
        sampled_process_tree_peak_rss_gb=sampler.peak / 1024**3,
        sampler_interval_seconds=sampler_interval,
        sampler_samples=sampler.samples,
        child_cpu_seconds=after.ru_utime
        + after.ru_stime
        - before.ru_utime
        - before.ru_stime,
        controller_cpu_seconds=time.process_time() - controller_cpu_started,
        driver_loop_lag_p95_seconds=percentile(lag),
        results=sorted(results, key=lambda r: r["id"]),
    )


def run(output, pairs=3, agents=4, cpus=2, scenarios=None, include_batches=False):
    if (
        type(pairs) is not int
        or not 1 <= pairs <= 5
        or type(agents) is not int
        or not 2 <= agents <= 6
    ):
        raise ValueError("Use 1 to 5 pairs and 2 to 6 fixture agents")
    selected = list(scenarios or SCENARIOS)
    if (
        not selected
        or len(set(selected)) != len(selected)
        or any(s not in SUPPORTED_SCENARIOS for s in selected)
    ):
        raise ValueError("Select unique supported benchmark scenarios")
    if type(include_batches) is not bool or (
        include_batches and not any(s in VERIFICATION_SCENARIOS for s in selected)
    ):
        raise ValueError("Batch comparison requires the Python verification scenario")
    budget = parent_budget(cpus)
    output = Path(output).expanduser().absolute()
    progress = output.with_name(output.name + ".progress.json")
    if (
        output.exists()
        or output.is_symlink()
        or progress.exists()
        or progress.is_symlink()
    ):
        raise ValueError("Experiment output must be a new path")
    affinity = os.sched_getaffinity(0)
    chosen = sorted(affinity)[-cpus:]
    directory = output.parent / ("buildr-experiment-" + uuid.uuid4().hex[:12])
    previous = {}
    cancelled = [False]
    records = []
    before = snapshot(output.parent)
    from .source import capture, changed

    source_before = capture(Path(__file__).resolve().parent.parent)
    bundle = None
    workload = None
    if "buildr_verification" in selected:
        from .verification import snapshot as freeze

        if (
            source_before["status"] != "captured"
            or Path(__file__).resolve().parent.parent in output.parents
        ):
            raise ValueError(
                "Buildr verification requires captured source and an output outside the checkout"
            )
        bundle = directory / "source.zip"
        workload = freeze(Path(__file__).resolve().parent.parent, bundle)
    else:
        directory.mkdir(mode=0o700)
    write_export(
        progress,
        dict(
            schema="buildr-paired-experiment-progress-v1",
            fixture_directory=str(directory),
            source_before=source_before,
            scenarios=selected,
            pairs=pairs,
            automatic_retries=False,
        ),
    )
    active = None
    try:
        os.sched_setaffinity(0, chosen)

        def signal_handler(signum, frame):
            cancelled[0] = True

        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, signal_handler)
        for pair in range(pairs):
            order = (
                ("independent", "queued")
                if pair % 2 == 0
                else ("queued", "independent")
            )
            for scenario in selected:
                scenario_order = order
                if include_batches and scenario in VERIFICATION_SCENARIOS:
                    modes = ("independent", "queued", "serial_batch", "parallel_batch")
                    shift = pair % len(modes)
                    scenario_order = modes[shift:] + modes[:shift]
                for mode in scenario_order:
                    active = dict(pair=pair, scenario=scenario, mode=mode)
                    if bundle is not None and scenario == "buildr_verification":
                        result = arm(
                            directory / str(pair) / scenario / mode,
                            scenario,
                            mode,
                            agents,
                            cpus,
                            cancelled,
                            bundle=bundle,
                        )
                    else:
                        result = arm(
                            directory / str(pair) / scenario / mode,
                            scenario,
                            mode,
                            agents,
                            cpus,
                            cancelled,
                        )
                    result["pair"] = pair
                    records.append(result)
                    print(
                        "{} pair {} {}: {}/{} verified, {:.3f}s".format(
                            scenario,
                            pair,
                            mode,
                            result["verified_jobs"],
                            result["jobs"],
                            result["wall_seconds"],
                        ),
                        flush=True,
                    )
        active = None
    except BaseException as error:
        write_export(
            output,
            dict(
                schema="buildr-paired-experiment-v1",
                application_version=__version__,
                result="failed",
                error=type(error).__name__ + ": " + str(error),
                active_arm=active,
                records=records,
                comparisons=[],
                batch_comparisons=[],
                source_before=source_before,
                fixture_directory=str(directory),
                automatic_retries=False,
                note=NOTE,
            ),
        )
        raise
    finally:
        os.sched_setaffinity(0, affinity)
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    comparisons = []
    source_after = capture(Path(__file__).resolve().parent.parent)
    same_source = changed(source_before, source_after) is False
    for scenario in selected:
        matched = [r for r in records if r["scenario"] == scenario]
        for pair in range(pairs):
            base = next(
                r for r in matched if r["pair"] == pair and r["mode"] == "independent"
            )
            queued = next(
                r for r in matched if r["pair"] == pair and r["mode"] == "queued"
            )
            comparable = same_source and all(
                r["verified_jobs"] == agents for r in (base, queued)
            )
            cost_allowed = comparable or scenario != "buildr_verification"
            comparisons.append(
                dict(
                    scenario=scenario,
                    pair=pair,
                    verified_delta=queued["verified_jobs"] - base["verified_jobs"],
                    comparable=comparable,
                    verified_throughput_ratio=(
                        queued["verified_completions_per_second"]
                        / base["verified_completions_per_second"]
                    )
                    if comparable
                    else None,
                    queued_to_independent_wall_ratio=queued["wall_seconds"]
                    / base["wall_seconds"]
                    if cost_allowed
                    else None,
                    queued_to_independent_sampled_rss_ratio=queued[
                        "sampled_process_tree_peak_rss_gb"
                    ]
                    / base["sampled_process_tree_peak_rss_gb"]
                    if cost_allowed and base["sampled_process_tree_peak_rss_gb"]
                    else None,
                )
            )
    batch_comparisons = []
    if include_batches:
        for scenario in selected:
            if scenario not in VERIFICATION_SCENARIOS:
                continue
            for pair in range(pairs):
                matched = {
                    r["mode"]: r
                    for r in records
                    if r["scenario"] == scenario and r["pair"] == pair
                }
                for mode in ("serial_batch", "parallel_batch"):
                    result = matched[mode]
                    baseline = matched["queued"]
                    comparable = same_source and all(
                        r["verified_jobs"] == agents for r in (result, baseline)
                    )
                    allowed = comparable or scenario != "buildr_verification"
                    batch_comparisons.append(
                        dict(
                            pair=pair,
                            scenario=scenario,
                            mode=mode,
                            comparable=comparable,
                            verified_delta=result["verified_jobs"]
                            - baseline["verified_jobs"],
                            batch_to_separate_queue_wall_ratio=result["wall_seconds"]
                            / baseline["wall_seconds"]
                            if allowed
                            else None,
                            batch_to_separate_queue_sampled_rss_ratio=result[
                                "sampled_process_tree_peak_rss_gb"
                            ]
                            / baseline["sampled_process_tree_peak_rss_gb"]
                            if allowed and baseline["sampled_process_tree_peak_rss_gb"]
                            else None,
                        )
                    )
    report = dict(
        schema="buildr-paired-experiment-v1",
        application_version=__version__,
        pairs=pairs,
        agents=agents,
        cpu_affinity=chosen,
        outer_reservation=budget,
        fixture_directory=str(directory),
        before_machine=before,
        after_machine=snapshot(directory),
        records=records,
        comparisons=comparisons,
        result="completed",
        buildr_workload=workload,
        verification_workload_valid=(
            same_source
            and all(
                r["verified_jobs"] == agents
                for r in records
                if r["scenario"] == "buildr_verification"
            )
        )
        if workload
        else True,
        include_batches=include_batches,
        batch_comparisons=batch_comparisons,
        batch_comparison_scope="Fixed independent verification workloads; one aggregate reservation, serial or explicit parallel execution. Four-mode order rotates by pair. Worker hints differ by allocation; this is a workflow strategy comparison.",
        independent_arm_scope="fixed fixtures launched directly inside the same bounded outer reservation",
        queued_arm_config=dict(
            cpu_slots=cpus, max_running=cpus, memory_gb=2, quick_reserves=0
        ),
        source_before=source_before,
        source_after=source_after,
        source_changed=changed(source_before, source_after),
        autonomous_coding_productivity_measured=False,
        note=NOTE,
    )
    write_export(output, report)
    return report


if __name__ == "__main__":
    if len(sys.argv) != 5 or sys.argv[1] != "_worker":
        raise SystemExit("Internal fixture worker only; use buildr experiment run")
    raise SystemExit(worker(*sys.argv[2:]))
