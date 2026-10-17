"""Own the disk inspector and its du children until the dashboard reaps them."""

from concurrent.futures import Future
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from threading import Thread

from .store import Store
from .tui_data import state_root

SCHEMA = "buildr-tui-disk-v1"
MAX_OUTPUT_BYTES = 1024 * 1024


def worker_command(root, repository):
    """The only supervised command is this package's read-only disk worker."""
    return [
        sys.executable,
        "-m",
        "agent_builds.tui_disk",
        "--worker",
        str(root),
        str(repository),
    ]


def _inventory(root, repository):
    from .disk import inventory

    reader = Store(root, read_only=True)
    try:
        if reader.db is not None:
            reader.db.execute("PRAGMA busy_timeout=1000")
        return inventory(reader, repository)
    finally:
        reader.close()


def _result(encoded):
    if len(encoded) > MAX_OUTPUT_BYTES:
        raise ValueError("Disk inventory output exceeded 1 MiB")
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (ValueError, UnicodeError) as error:
        raise ValueError("Invalid disk inventory output") from error
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise ValueError("Invalid disk inventory schema")
    error = payload.get("error")
    if error is not None:
        if not isinstance(error, dict):
            raise ValueError("Invalid disk inventory error")
        raise ValueError(
            "Disk inventory failed: {}: {}".format(
                error.get("type", "Error"), error.get("message", "unknown error")
            )
        )
    result = payload.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("paths"), list):
        raise ValueError("Invalid disk inventory result")
    for item in result["paths"]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("path"), str)
            or "bytes" not in item
            or (
                item["bytes"] is not None
                and (type(item["bytes"]) is not int or item["bytes"] < 0)
            )
        ):
            raise ValueError("Invalid disk inventory path")
    return result


class DiskRead:
    """One owned process group; only the UI thread may close or restart it.

    The stdout reader never polls or reaps the leader. Keeping that child
    unreaped reserves its PID/PGID until close() signals its entire group,
    including du processes that may outlive the helper's main thread.
    """

    def __init__(self, root=None):
        self.root = state_root(root)
        self._process = None

    def start(self, repository):
        if self._process is not None:
            raise RuntimeError(
                "Close the previous disk inspector before starting another"
            )
        if signal.getsignal(signal.SIGCHLD) not in (signal.SIG_DFL, None):
            raise ValueError(
                "Disk inventory requires default SIGCHLD handling to retain child ownership"
            )
        repository = Path(repository).expanduser().resolve()
        env = os.environ.copy()
        package_root = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = package_root + os.pathsep + env.get("PYTHONPATH", "")
        process = subprocess.Popen(
            worker_command(self.root, repository),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._process = process
        future = Future()
        future.set_running_or_notify_cancel()

        def read_output():
            try:
                with process.stdout as stream:
                    result = _result(stream.read(MAX_OUTPUT_BYTES + 1))
            except Exception as error:
                future.set_exception(error)
            else:
                future.set_result(result)

        try:
            Thread(target=read_output, name="buildr-disk-output", daemon=True).start()
        except BaseException:
            self.close()
            raise
        return future

    def close(self):
        process = self._process
        if process is None:
            return True
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            # Keep ownership explicit: a later close can retry cleanup, and a
            # new inspector cannot replace this still-unreaped process.
            return False
        self._process = None
        return True


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3 or args[0] != "--worker":
        return 2
    try:
        result = _inventory(args[1], args[2])
    except Exception as error:
        payload = dict(
            schema=SCHEMA,
            result=None,
            error=dict(type=type(error).__name__, message=str(error)),
        )
        status = 1
    else:
        payload, status = dict(schema=SCHEMA, result=result, error=None), 0
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()
    return status


if __name__ == "__main__":
    raise SystemExit(main())
