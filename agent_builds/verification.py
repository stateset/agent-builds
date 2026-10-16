"""Fixed Buildr package/test workloads in isolated, frozen source copies."""

import email
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tarfile
import time
import zipfile

from . import __version__
from .plans import write_export

MAX_BYTES = 8 * 1024 * 1024
STAGES = ("package", "browser_tests", "prompt_tests")


def snapshot(root, destination):
    if (
        importlib.util.find_spec("build") is None
        or importlib.util.find_spec("setuptools") is None
    ):
        raise ValueError(
            "Buildr verification needs an interpreter with the optional build tool and setuptools backend installed"
        )
    status = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=normal"], cwd=str(root)
    )
    if status.strip():
        raise ValueError(
            "Buildr verification requires a clean, committed source checkout"
        )
    data = subprocess.check_output(
        ["git", "archive", "--format=zip", "HEAD"], cwd=str(root)
    )
    if len(data) > MAX_BYTES:
        raise ValueError("Buildr verification source archive exceeds 8 MiB")
    destination.parent.mkdir(mode=0o700, exist_ok=True)
    with destination.open("xb") as output:
        os.chmod(str(destination), 0o600)
        output.write(data)
    return dict(sha256=hashlib.sha256(data).hexdigest(), bytes=len(data))


def prepare(bundle, work):
    checkout = work / "checkout"
    checkout.mkdir(mode=0o700)
    with zipfile.ZipFile(bundle) as archive:
        for name in archive.namelist():
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("Invalid frozen source archive member")
        archive.extractall(checkout)
    return checkout


def artifacts(checkout):
    files = sorted((checkout / "dist").iterdir())
    if len(files) != 2:
        raise ValueError("Expected one wheel and one source archive")
    records = []
    kinds = set()
    for path in files:
        if path.is_symlink() or not path.is_file():
            raise ValueError("Distribution must be a regular file")
        with path.open("rb") as source:
            data = source.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError("Distribution exceeds 8 MiB")
        if path.suffix == ".whl":
            kinds.add("wheel")
            with zipfile.ZipFile(io.BytesIO(data)) as wheel:
                names = wheel.namelist()
                for module in (
                    "agent_builds/tui.py",
                    "agent_builds/tui_prompt.py",
                    "agent_builds/runner.py",
                ):
                    if module not in names:
                        raise ValueError("Wheel is missing " + module)
                metadata = [
                    name for name in names if name.endswith(".dist-info/METADATA")
                ]
                if len(metadata) != 1:
                    raise ValueError("Expected unique wheel metadata")
                info = email.message_from_bytes(wheel.read(metadata[0]))
        elif path.name.endswith(".tar.gz"):
            kinds.add("source")
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as source:
                names = source.getnames()
                if not any(
                    name.endswith("/agent_builds/tui_prompt.py") for name in names
                ):
                    raise ValueError("Source archive is missing prompt code")
                metadata = [
                    name
                    for name in names
                    if name.count("/") == 1 and name.endswith("/PKG-INFO")
                ]
                if len(metadata) != 1:
                    raise ValueError("Expected unique source metadata")
                info = email.message_from_bytes(source.extractfile(metadata[0]).read())
        else:
            raise ValueError("Unexpected distribution format")
        if info["Name"] != "agent-builds" or info["Version"] != __version__:
            raise ValueError(
                "Distribution name/version does not match the frozen workload"
            )
        records.append(
            dict(
                name=path.name, bytes=len(data), sha256=hashlib.sha256(data).hexdigest()
            )
        )
    if kinds != {"wheel", "source"}:
        raise ValueError("Expected both wheel and source distributions")
    return records


def run_worker(work, identifier):
    work = Path(work)
    checkout = work / "checkout"
    commands = (
        [
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--outdir",
            str(checkout / "dist"),
        ],
        [
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-p",
            "test_tui_browser.py",
            "-v",
        ],
        [
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-p",
            "test_tui_prompt.py",
            "-v",
        ],
    )
    env = dict(os.environ, AGENT_BUILDS_STATE=str(work / "verification-state"))
    env.pop("PYTHONPATH", None)  # Native tests must import their own frozen checkout.
    stages = []
    for name, command in zip(STAGES, commands):
        print("Buildr verification stage: " + name, flush=True)
        started = time.monotonic()
        # Foreground descendants inherit the worker's process group. The arm's
        # deadline and fixture-only cleanup cover the complete command tree.
        result = subprocess.run(command, cwd=str(checkout), env=env, check=False)
        stages.append(
            dict(
                name=name,
                argv=command,
                exit_code=result.returncode,
                seconds=time.monotonic() - started,
            )
        )
    error, outputs = None, []
    try:
        outputs = artifacts(checkout)
    except (
        OSError,
        ValueError,
        KeyError,
        tarfile.TarError,
        zipfile.BadZipFile,
    ) as problem:
        error = str(problem)
    verified = all(stage["exit_code"] == 0 for stage in stages) and error is None
    write_export(
        work / ("result-" + identifier + ".json"),
        dict(
            id=identifier,
            verified=verified,
            stages=stages,
            artifacts=outputs,
            error=error,
        ),
    )
    return 0 if verified else 7


def read_result(work, identifier):
    with (work / ("result-" + identifier + ".json")).open("rb") as source:
        data = source.read(64 * 1024 + 1)
    if len(data) > 64 * 1024:
        raise ValueError("Workload receipt exceeds 64 KiB")
    value = json.loads(data)
    if not isinstance(value, dict) or value.get("id") != identifier:
        raise ValueError("Workload receipt has the wrong identity")
    return value


def verify(work, identifier, value=None):
    """Recheck native stage outcomes and artifact contents, not only wrapper exit."""
    value = read_result(work, identifier) if value is None else value
    if value.get("id") != identifier or value.get("verified") is not True:
        raise ValueError("Workload has no verified result for " + identifier)
    stages = value.get("stages", [])
    if tuple(stage.get("name") for stage in stages) != STAGES or any(
        type(stage.get("exit_code")) is not int or stage["exit_code"] != 0
        for stage in stages
    ):
        raise ValueError("Native verification stages did not all succeed")
    if value.get("artifacts") != artifacts(work / "checkout"):
        raise ValueError("Distribution evidence changed after native verification")
    return value
