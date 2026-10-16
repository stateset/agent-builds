"""Crash real fixture runners; verify recovery without touching other jobs.

Queue this whole driver once. Its commands and queues are private fixtures, not
repository builds or a measure of coding productivity.
"""

import argparse
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import zipfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent_builds import __version__
from agent_builds.experiments import parent_budget
from agent_builds.plans import write_export
from agent_builds.runner import cancel_job
from agent_builds.source import capture, changed
from agent_builds.store import Store, alive, identity


def require(condition, message):
    if not condition:
        raise ValueError(message)


def until(predicate, description, timeout=15):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise ValueError("Timed out observing " + description)
        time.sleep(0.02)


def baseline(destination):
    # Fixed, trusted release; no checkout, network access or user-provided argv.
    data = subprocess.check_output(
        ["git", "archive", "--format=zip", "v0.24.2", "agent_builds"],
        cwd=str(ROOT),
    )
    with zipfile.ZipFile(io.BytesIO(data)) as bundle:
        bundle.extractall(destination)
    return destination


def run_case(root, checkout, cancel=False):
    """One real orphan and one real follower on its secondary resource lock."""
    root.mkdir(mode=0o700)
    store = Store(root / "state")
    processes = []
    release = root / "release"
    ready, finished, ledger = (
        root / "ready.json",
        root / "finished.json",
        root / "ledger",
    )
    follower_started = root / "follower-started"
    try:
        store.update_config(
            dict(
                cpu_slots=2,
                max_running=2,
                max_heavy=1,
                memory_gb=2,
                quick_reserve_slots=0,
                quick_reserve_jobs=0,
                quick_reserve_memory_gb=0,
                min_free_gb=0,
                min_available_gb=0,
                nice=0,
            )
        )
        # Audit each admission atomically, including admissions between polls.
        store.db.executescript(
            "CREATE TABLE fixture_admissions(slots INTEGER,memory REAL);"
            "CREATE TRIGGER fixture_audit AFTER UPDATE OF state ON jobs "
            "WHEN NEW.state='running' AND OLD.state!='running' BEGIN "
            "INSERT INTO fixture_admissions SELECT SUM(slots),SUM(memory_gb) "
            "FROM jobs WHERE state IN ('running','cancelling'); END;"
        )

        def launch(name, source, package, key, extra=()):
            receipt = store.root / ("receipt-" + name + ".json")
            env = dict(
                os.environ, PYTHONPATH=str(package), AGENT_BUILDS_STATE=str(store.root)
            )
            env.pop("AGENT_BUILDS_JOB_ID", None)  # this explicit private fixture only
            env.pop("HERDR_ENV", None)
            with (root / (name + ".log")).open("xb") as log:
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "agent_builds",
                        "run",
                        "--cwd",
                        str(root),
                        "--jobs",
                        "1",
                        "--lane",
                        "quick",
                        "--memory-gb",
                        ".25",
                        "--timeout",
                        "30",
                        "--resource-key",
                        key,
                        "--receipt-file",
                        str(receipt),
                    ]
                    + list(extra)
                    + ["--", sys.executable, "-c", source],
                    cwd=str(package),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                )
            processes.append(child)
            until(
                lambda: receipt.exists() or child.poll() is not None, name + " receipt"
            )
            require(receipt.exists(), name + " runner exited without a receipt")
            return child, json.loads(receipt.read_text())["job_id"]

        holder_source = """
import fcntl,json,os,time
from pathlib import Path
root=Path({root!r})
lock=(root/'output.lock').open('a')
fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
with (root/'ledger').open('a') as log:
    log.write('holder-start\\n'); log.flush(); os.fsync(log.fileno())
temporary=root/'ready.tmp'
temporary.write_text(json.dumps(dict(pid=os.getpid())))
temporary.replace(root/'ready.json')
deadline=time.monotonic()+25
while not (root/'release').exists():
    if time.monotonic()>=deadline: raise SystemExit(9)
    time.sleep(.01)
(root/'finished.json').write_text(json.dumps(dict(native_exit=7,at=time.monotonic_ns())))
raise SystemExit(7)
""".format(root=str(root))
        holder, job = launch(
            "holder", holder_source, checkout, "primary", ["--lock", "secondary"]
        )
        until(lambda: ready.exists(), "native holder readiness")
        child_pid = json.loads(ready.read_text())["pid"]
        original = store.get(job)
        require(
            original["child_pid"] == child_pid,
            "Native PID was not persisted before execution",
        )
        require(
            alive(child_pid, original["child_start"]), "Native identity is not live"
        )
        require(
            original["owner_pid"] == holder.pid,
            "Runner identity differs from its receipt",
        )
        holder.kill()  # only the known fixture runner, deliberately leave its child alive
        holder.wait(timeout=5)

        follower_source = (
            "import fcntl,time; from pathlib import Path; "
            "lock=Path({!r}).open('a'); fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB); ".format(
                str(root / "output.lock")
            )
            + "Path({!r}).write_text(str(time.monotonic_ns()))".format(
                str(follower_started)
            )
        )
        follower, following = launch("follower", follower_source, ROOT, "secondary")
        until(
            lambda: (
                "repository already building" in (store.get(following)["reason"] or "")
            ),
            "follower blocked by orphan's secondary lock",
        )
        recoveries = 0
        for _ in range(4):
            with_store = Store(store.root)
            try:
                with_store.recover()
                observed = with_store.get(job)
                require(
                    observed["state"] == "running", "Recovery released a live orphan"
                )
                require(observed["exit_code"] is None, "Recovery invented an exit code")
                require(
                    "child still running" in observed["reason"],
                    "Missing orphan diagnostic",
                )
                require(
                    with_store.get(following)["state"] == "queued",
                    "Secondary lock was released",
                )
                require(
                    not follower_started.exists(), "Conflicting native command started"
                )
                recoveries += 1
            finally:
                with_store.close()

        # An independent job must still be able to use the remaining capacity.
        independent, independent_id = launch(
            "independent", "raise SystemExit(0)", ROOT, "free"
        )
        require(independent.wait(timeout=15) == 0, "Orphan blocked unrelated work")
        require(
            store.get(independent_id)["exit_code"] == 0, "Independent result was lost"
        )
        if cancel:
            cancel_job(store, job)
        else:
            release.touch()
        until(
            lambda: not alive(child_pid, original["child_start"]), "native holder exit"
        )
        store.recover()
        settled = store.get(job)
        require(
            settled["state"] == ("cancelled" if cancel else "lost"),
            "Wrong orphan outcome",
        )
        require(
            settled["exit_code"] in ((None, 130) if cancel else (None,)),
            "Unknown native result was fabricated",
        )
        require(
            follower.wait(timeout=15) == 0,
            "Follower did not progress after orphan exit",
        )
        require(store.get(following)["exit_code"] == 0, "Follower result was lost")
        if not cancel:
            actual = json.loads(finished.read_text())
            require(
                actual["native_exit"] == 7,
                "Fixture did not reach its deliberate native failure",
            )
            require(
                int(follower_started.read_text()) >= actual["at"],
                "Follower overlapped its resource holder",
            )
        require(
            ledger.read_text().splitlines() == ["holder-start"],
            "Native command ran more than once",
        )
        for _ in range(4):
            store.recover()
        require(
            store.get(job)["state"] == settled["state"],
            "Recovery changed a terminal outcome",
        )
        require(not store.claim(job), "Recovery made unknown work eligible for retry")
        audit = store.db.execute(
            "SELECT MAX(slots),MAX(memory) FROM fixture_admissions"
        ).fetchone()
        require(
            audit[0] <= 2 and audit[1] <= 2,
            "Admission exceeded the private reservation",
        )
        require(len(store.rows(limit=10)) == 3, "Unexpected or duplicate submission")
        return dict(
            result="passed",
            holder_version=original["runner_version"],
            cancelled=cancel,
            job_id=job,
            follower_id=following,
            independent_id=independent_id,
            recoveries_while_child_live=recoveries,
            outcome=settled["state"],
            exit_code=settled["exit_code"],
            native_attempts=1,
            secondary_lock_preserved=True,
            unrelated_work_completed=True,
            native_lock_overlap=False,
            peak_reserved_slots=audit[0],
            peak_reserved_memory_gb=audit[1],
        )
    finally:
        release.touch()
        # Cleanup can signal only identities recorded in this private database.
        # Do not use process-name searches or signal another queue's jobs.
        for child in processes:
            if child.poll() is None:
                child.terminate()
        for child in processes:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        for row in store.rows(limit=10):
            if alive(row["child_pid"], row["child_start"]):
                try:
                    os.killpg(row["child_pid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
        store.close()


def run(output, cycles=2):
    require(type(cycles) is int and 1 <= cycles <= 10, "Use one to ten recovery cycles")
    budget = parent_budget(2)
    output = Path(output).expanduser().absolute()
    require(
        not output.exists() and not output.is_symlink(), "Report must be a new path"
    )
    require(ROOT not in output.parents, "Report must be outside the source checkout")
    source = capture(ROOT)
    require(source["status"] == "captured", "Complete source identity is required")
    fixture = Path(tempfile.mkdtemp(prefix="buildr-recovery-", dir=str(output.parent)))
    old = baseline(fixture / "baseline")
    report = dict(
        schema="buildr-recovery-check-v1",
        application_version=__version__,
        result="running",
        cycles=cycles,
        outer_reservation=budget,
        fixture_directory=str(fixture),
        source_before=source,
        cases=[],
        global_settings_changed=False,
        automatic_retries=False,
        note="Private native-process fault fixtures, not a production soak, compiler-throughput or coding-productivity measurement.",
    )
    write_export(output.with_name(output.name + ".progress.json"), report)

    def interrupted(signum, frame):
        raise KeyboardInterrupt("Recovery check interrupted")

    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        for cycle in range(cycles):
            for label, checkout in (("v0.24.2", old), ("candidate", ROOT)):
                for cancel in (False, True):
                    report["active_case"] = dict(
                        cycle=cycle, arm=label, cancelled=cancel
                    )
                    result = run_case(
                        fixture / (str(cycle) + "-" + label + "-" + str(cancel)),
                        checkout,
                        cancel,
                    )
                    result.update(cycle=cycle, arm=label)
                    report["cases"].append(result)
                    del report["active_case"]
                    print(json.dumps(result), flush=True)
        report["source_after"] = capture(ROOT)
        report["source_changed"] = changed(source, report["source_after"])
        require(
            report["source_changed"] is False, "Source changed during recovery checks"
        )
        report["result"] = "passed"
    except BaseException as error:
        report.update(result="failed", error=str(error))
        raise
    finally:
        try:
            write_export(output, report)
        finally:
            signal.signal(signal.SIGTERM, previous)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--cycles", type=int, default=2)
    args = parser.parse_args()
    report = run(args.output, args.cycles)
    print(
        json.dumps(
            dict(
                result=report["result"],
                cases=len(report["cases"]),
                output=str(args.output),
            )
        )
    )


if __name__ == "__main__":
    main()
