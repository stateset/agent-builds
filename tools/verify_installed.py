"""Exercise an installed wheel in a private queue outside the source checkout."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent_builds import __version__


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", required=True, type=Path)
    args = parser.parse_args()
    if not os.environ.get("AGENT_BUILDS_JOB_ID"):
        parser.error("Queue this entire verification once")
    buildr = args.python.absolute().parent / "buildr"
    with tempfile.TemporaryDirectory(prefix="buildr-installed-") as directory:
        root = Path(directory)
        state = root / "private-state"
        env = dict(os.environ, AGENT_BUILDS_STATE=str(state))
        env.pop("PYTHONPATH", None)
        env.pop("AGENT_BUILDS_JOB_ID", None)  # isolated installed-wheel queue only

        def run(command, code=0):
            result = subprocess.run(
                command,
                cwd=str(root),
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
            )
            if result.returncode != code:
                raise AssertionError(
                    (command, result.returncode, result.stdout, result.stderr)
                )
            return result.stdout

        # Keep the venv entrypoint path: resolving its Python symlink would use
        # the base interpreter and miss the package installed in this venv.
        python = args.python.absolute()
        observed = json.loads(
            run(
                [
                    str(python),
                    "-c",
                    "import agent_builds,json; print(json.dumps(dict(version=agent_builds.__version__,file=agent_builds.__file__)))",
                ]
            )
        )
        assert observed["version"] == __version__, observed
        assert str(python.parent.parent) in observed["file"], observed
        status = json.loads(run([str(buildr), "status", "--json"]))
        assert status["initialized"] is False and not state.exists()
        run(
            [
                str(buildr),
                "config",
                "--set",
                "cpu_slots=2",
                "--set",
                "memory_gb=2",
                "--set",
                "max_running=2",
                "--set",
                "max_heavy=1",
                "--set",
                "quick_jobs=1",
                "--set",
                "heavy_jobs=1",
                "--set",
                "quick_reserve_slots=0",
                "--set",
                "quick_reserve_jobs=0",
                "--set",
                "quick_reserve_memory_gb=0",
                "--set",
                "min_free_gb=0",
                "--set",
                "min_available_gb=0",
                "--set",
                "nice=0",
            ]
        )
        run(
            [
                str(buildr),
                "run",
                "--cwd",
                str(root),
                "--jobs",
                "1",
                "--memory-gb",
                ".25",
                "--timeout",
                "10",
                "--",
                str(python),
                "-c",
                'print("native-seven"); raise SystemExit(7)',
            ],
            code=7,
        )
        manifest = root / "checks.json"
        manifest.write_text(
            json.dumps(
                dict(
                    max_parallel=2,
                    checks=[
                        dict(
                            name="success",
                            command=[str(python), "-c", 'print("batch-zero")'],
                        ),
                        dict(
                            name="failure",
                            command=[str(python), "-c", "raise SystemExit(7)"],
                        ),
                    ],
                )
            )
        )
        # A misleading package in cwd must not shadow the installed worker.
        (root / "agent_builds").mkdir()
        (root / "agent_builds/__init__.py").write_text(
            'raise RuntimeError("cwd import escaped")'
        )
        run(
            [
                str(buildr),
                "batch",
                "run",
                str(manifest),
                "--cwd",
                str(root),
                "--jobs",
                "2",
                "--memory-gb",
                ".5",
                "--timeout",
                "10",
            ],
            code=7,
        )
        status = json.loads(
            run(
                [
                    str(buildr),
                    "status",
                    "--json",
                    "--history",
                    "--details",
                    "--limit",
                    "10",
                ]
            )
        )
        batches = [row for row in status["jobs"] if row.get("batch")]
        assert len(batches) == 1, status
        assert [check["exit_code"] for check in batches[0]["batch"]["checks"]] == [
            0,
            7,
        ], batches
        print(
            json.dumps(
                dict(
                    version=__version__,
                    installed=observed,
                    native_exit=7,
                    batch_exit=7,
                    check_exits=[0, 7],
                    read_only_missing_state=True,
                )
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
