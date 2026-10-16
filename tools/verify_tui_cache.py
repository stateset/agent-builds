"""Compare bounded TUI hot paths with v0.25.0; fixture commands never execute."""

import argparse
import importlib
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
from agent_builds import tui_browser, tui_logs
from agent_builds.source import capture, changed
from agent_builds.plans import write_export
from agent_builds.experiments import parent_budget


def run(output):
    budget = parent_budget(1)
    output = Path(output).expanduser().absolute()
    if output.exists() or output.is_symlink() or ROOT in output.parents:
        raise ValueError("Use a new report path outside the source checkout")
    source = capture(ROOT)
    if source.get("status") != "captured":
        raise ValueError("Complete source identity is required")
    rows = [
        dict(
            id=str(i),
            state="running" if i % 4 == 0 else "queued",
            cwd="/fixture/repo-" + str(i % 10),
            command_display="cargo check --workspace --all-targets",
            origin=dict(pane_id="fixture-" + str(i)),
            created=i,
            wait_seconds=i,
            elapsed_seconds=i,
        )
        for i in range(200)
    ]
    records = []
    with tempfile.TemporaryDirectory(prefix="buildr-tui-cache-study-") as directory:
        root = Path(directory)
        archive = subprocess.check_output(
            ["git", "archive", "--format=zip", "v0.25.0", "agent_builds"], cwd=str(ROOT)
        )
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            bundle.extractall(root / "old")
        package = root / "old/agent_builds"
        spec = importlib.util.spec_from_file_location(
            "buildr_tui_reference",
            package / "__init__.py",
            submodule_search_locations=[str(package)],
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        old = importlib.import_module(spec.name + ".tui")
        path = root / "fixture.log"
        original_open, original_fdopen = Path.open, os.fdopen
        original_filter, original_sort = tui_browser.filter_rows, tui_browser.sort_rows

        def measure(mode, changing):
            path.write_text("unchanged log row\n" * 5000)
            counts = dict(read_calls=0, read_bytes=0, filter_calls=0, sort_calls=0)

            class Reader:
                def __init__(self, handle):
                    self.handle = handle

                def __getattr__(self, name):
                    return getattr(self.handle, name)

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return self.handle.__exit__(*args)

                def read(self, *args):
                    data = self.handle.read(*args)
                    counts["read_calls"] += 1
                    counts["read_bytes"] += len(data)
                    return data

            def opened(location, *args, **kwargs):
                handle = original_open(location, *args, **kwargs)
                return (
                    Reader(handle)
                    if location == path and args and args[0] == "rb"
                    else handle
                )

            def fdopened(*args, **kwargs):
                return Reader(original_fdopen(*args, **kwargs))

            def filtered(*args):
                counts["filter_calls"] += 1
                return original_filter(*args)

            def ordered(*args):
                counts["sort_calls"] += 1
                return original_sort(*args)

            tail, browser = tui_logs.TailCache(), tui_browser.BrowserCache()
            started, cpu = time.monotonic(), time.process_time()
            with mock.patch.object(Path, "open", opened), mock.patch.object(
                os, "fdopen", fdopened
            ), mock.patch.object(
                tui_browser, "filter_rows", filtered
            ), mock.patch.object(tui_browser, "sort_rows", ordered):
                for frame in range(300):
                    if frame % 5 == 0:
                        snapshot = list(
                            rows
                        )  # simulate the dashboard's one-second row refresh
                        if changing:
                            with path.open("a") as writer:
                                writer.write("update " + str(frame) + "\n")
                    if mode == "baseline":
                        visible = old.tail(path, 15)
                        displayed = old.sort_rows(
                            old.filter_rows(snapshot, "all", "cargo"), "repository"
                        )
                        counts["filter_calls"] += 1
                        counts["sort_calls"] += 1
                    else:
                        visible = tail.read(path, 15, frame * 0.2)
                        displayed = browser.get(snapshot, "all", "cargo", "repository")
            elapsed, cpu = time.monotonic() - started, time.process_time() - cpu
            # A live tail may lag by up to its poll interval. Compare settled output.
            expected = old.tail(path, 15)
            if mode == "candidate":
                visible = tail.read(path, 15, 61)
            assert visible == expected
            assert [r["id"] for r in displayed] == [
                r["id"]
                for r in old.sort_rows(
                    old.filter_rows(rows, "all", "cargo"), "repository"
                )
            ]
            assert (
                counts["filter_calls"]
                == counts["sort_calls"]
                == (300 if mode == "baseline" else 60)
            )
            assert (
                counts["read_calls"] == 300
                if mode == "baseline"
                else counts["read_calls"] <= 60
            )
            return dict(
                mode=mode,
                changing=changing,
                wall_seconds=elapsed,
                cpu_seconds=cpu,
                frames=300,
                rows=200,
                final_output_matches=True,
                **counts,
            )

        for pair, order in enumerate(
            (("baseline", "candidate"), ("candidate", "baseline"))
        ):
            for changing in (False, True):
                for mode in order:
                    record = measure(mode, changing)
                    record["pair"] = pair
                    records.append(record)
    after = capture(ROOT)
    source_changed = changed(source, after)
    if source_changed is not False:
        raise ValueError("Source identity changed during the TUI comparison")
    result = dict(
        schema="buildr-tui-cache-microbenchmark-v1",
        result="passed",
        baseline_tag="v0.25.0",
        source_before=source,
        source_after=after,
        records=records,
        note="Synthetic unchanged/growing inline tails and 200 display rows at simulated 5 Hz, renewed once a second. Paired checkout order; wall timings are observations, not terminal FPS, real UI latency or coding productivity. No job commands launched by the fixture.",
    )
    result.update(outer_reservation=budget, source_changed=source_changed)
    write_export(output, result)
    for changing in (False, True):
        print(
            json.dumps(
                dict(
                    changing=changing,
                    summary={
                        mode: {
                            k: statistics.median(
                                r[k]
                                for r in records
                                if r["mode"] == mode and r["changing"] == changing
                            )
                            for k in (
                                "read_calls",
                                "read_bytes",
                                "filter_calls",
                                "wall_seconds",
                                "cpu_seconds",
                            )
                        }
                        for mode in ("baseline", "candidate")
                    },
                )
            )
        )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    run(parser.parse_args().output)


if __name__ == "__main__":
    main()
