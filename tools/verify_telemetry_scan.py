"""Compare process telemetry with v0.26.0 using fixed, non-executable proc data."""

import argparse
import importlib.util
import io
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from agent_builds import process_tree, telemetry
from agent_builds.experiments import parent_budget
from agent_builds.plans import write_export
from agent_builds.source import capture, changed


def fixture(root, count=1000):
    root.mkdir()
    for pid in range(100, 100 + count):
        fields = ["0"] * 22
        fields[0] = "S"
        fields[1] = str(pid - 1 if 100 < pid <= 132 else 1)
        fields[2] = str(100 if pid in (100, 133) else pid)
        fields[11], fields[12] = str(pid), "7"
        fields[19], fields[21] = str(pid * 10), "16"
        directory = root / str(pid)
        directory.mkdir()
        (directory / "stat").write_text(
            str(pid) + " (fixture) " + " ".join(fields) + "\n"
        )


def run(output):
    budget = parent_budget(1)
    output = Path(output).expanduser().absolute()
    progress = output.with_name(output.name + ".progress.json")
    if (
        any(path.exists() or path.is_symlink() for path in (output, progress))
        or ROOT in output.parents
    ):
        raise ValueError("Use new evidence paths outside the source checkout")
    source = capture(ROOT)
    if source.get("status") != "captured":
        raise ValueError("Complete source identity is required")
    report = dict(
        schema="buildr-telemetry-scan-study-v1",
        result="running",
        baseline_tag="v0.26.0",
        source_before=source,
        outer_reservation=budget,
        records=[],
        automatic_retries=False,
        note="Fixed proc metadata for 1000 processes, including 34 selected identities and a 32-level tree. No fixture process is launched. Timings measure the sampler, not build throughput or agent productivity. CPU/RSS remain sampled estimates and are not used to authorize signalling.",
    )
    write_export(progress, report)
    try:
        with tempfile.TemporaryDirectory(prefix="buildr-telemetry-study-") as directory:
            root = Path(directory)
            archive = subprocess.check_output(
                [
                    "git",
                    "archive",
                    "--format=zip",
                    "v0.26.0",
                    "agent_builds/telemetry.py",
                ],
                cwd=str(ROOT),
            )
            with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                bundle.extractall(root / "baseline")
            path = root / "baseline/agent_builds/telemetry.py"
            spec = importlib.util.spec_from_file_location(
                "buildr_telemetry_reference", path
            )
            baseline = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(baseline)
            proc = root / "proc"
            fixture(proc)
            baseline.Path = lambda path: proc if str(path) == "/proc" else Path(path)
            read = process_tree.read_processes
            reference = None
            for pair in range(4):
                order = (
                    ("baseline", "candidate")
                    if pair % 2 == 0
                    else ("candidate", "baseline")
                )
                for mode in order:
                    sampler = (
                        baseline if mode == "baseline" else telemetry
                    ).JobSampler(100, "1000")
                    with mock.patch.object(
                        telemetry, "read_processes", side_effect=lambda: read(proc)
                    ):
                        cpu, started = time.process_time(), time.monotonic()
                        for _ in range(20):
                            sampler.sample()
                        cpu, elapsed = (
                            time.process_time() - cpu,
                            time.monotonic() - started,
                        )
                    proof = dict(
                        identities=sorted(sampler.known),
                        counters=sorted(sampler.counters.items()),
                        peak_bytes=sampler.peak,
                        samples=sampler.samples,
                    )
                    if reference is None:
                        reference = proof
                    if (
                        proof != reference
                        or len(proof["identities"]) != 34
                        or proof["samples"] != 20
                    ):
                        raise ValueError(
                            "Candidate and baseline process evidence differ"
                        )
                    record = dict(
                        pair=pair,
                        mode=mode,
                        cpu_seconds=cpu,
                        wall_seconds=elapsed,
                        processes=1000,
                        samples=20,
                        selected_identities=34,
                        measurements_match=True,
                    )
                    report["records"].append(record)
                    print(json.dumps(record), flush=True)
        report["source_after"] = capture(ROOT)
        report["source_changed"] = changed(source, report["source_after"])
        if report["source_changed"] is not False:
            raise ValueError("Source changed during telemetry comparison")
        report["summary"] = {
            mode: {
                key: statistics.median(
                    r[key] for r in report["records"] if r["mode"] == mode
                )
                for key in ("cpu_seconds", "wall_seconds")
            }
            for mode in ("baseline", "candidate")
        }
        report["result"] = "passed"
    except BaseException as error:
        report.update(result="failed", error=type(error).__name__ + ": " + str(error))
        raise
    finally:
        write_export(output, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = run(args.output)
    print(
        json.dumps(
            dict(
                result=report["result"],
                summary=report["summary"],
                output=str(args.output),
            )
        )
    )


if __name__ == "__main__":
    main()
