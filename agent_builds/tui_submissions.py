"""Durable dashboard attempts; receipts plus read-only queue identity confirm creation."""

from collections import OrderedDict
import contextlib
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
from stat import S_ISREG
import subprocess
import sys
import time
import traceback
from typing import NamedTuple, Optional
import uuid

from .store import Store
from .tui_reads import submit_read

JOURNAL_SCHEMA = "buildr-tui-submission-v1"
MAX_JSON_BYTES = 64 * 1024
MAX_TRANSCRIPT_BYTES = 64 * 1024
KEY = re.compile(r"[0-9a-f]{32}\Z")
HANDLE = re.compile(r"[0-9a-f]{12}\Z")


class SubmissionStatus(NamedTuple):
    key: str
    label: str
    phase: str
    kind: Optional[str]
    handle: Optional[str]
    detail: str
    receipt_path: str
    transcript_path: str


class SubmissionInspection(NamedTuple):
    key: str
    kind: Optional[str] = None
    handle: Optional[str] = None
    detail: str = ""
    returncode: Optional[int] = None


def _paths(root, key):
    return (
        root / ("receipt-tui-" + key + ".json"),
        root / "logs" / ("submission-tui-" + key + ".log"),
        root / ("attempt-tui-" + key + ".json"),
    )


def _validate_argv(argv):
    if (
        not isinstance(argv, (list, tuple))
        or not argv
        or len(argv) > 1024
        or any(not isinstance(value, str) or "\0" in value for value in argv)
        or len(json.dumps(argv, ensure_ascii=False).encode("utf-8")) > MAX_JSON_BYTES
        or not (argv[0] == "run" or list(argv[:2]) == ["task", "run"])
    ):
        raise ValueError("Dashboard submissions require run or task run arguments")
    if argv[0] == "run" and ("--" not in argv or argv.index("--") == len(argv) - 1):
        raise ValueError(
            "Dashboard run arguments require '--' before the native command"
        )
    boundary = argv.index("--") if argv[0] == "run" and "--" in argv else len(argv)
    if any(
        value == "--receipt-file" or value.startswith("--receipt-file=")
        for value in argv[:boundary]
    ):
        raise ValueError(
            "Dashboard receipt paths are assigned by the submission helper"
        )
    return list(argv)


def _command(argv, receipt):
    argv = _validate_argv(argv)
    boundary = argv.index("--") if argv[0] == "run" and "--" in argv else len(argv)
    options = ["--receipt-file", str(receipt)]
    if "--detach" not in argv[:boundary]:
        options.append("--detach")
    return argv[:boundary] + options + argv[boundary:]


def _read_json(path):
    try:
        descriptor = os.open(
            str(path), os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
        )
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(descriptor)
        if not S_ISREG(info.st_mode) or info.st_size > MAX_JSON_BYTES:
            raise ValueError("Submission evidence must be a bounded regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            encoded = source.read(MAX_JSON_BYTES + 1)
        if len(encoded) > MAX_JSON_BYTES:
            raise ValueError("Submission evidence exceeds 64 KiB")
        return json.loads(encoded.decode("utf-8"))
    finally:
        os.close(descriptor)


def _journal(value, key):
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or value.get("schema") != JOURNAL_SCHEMA
        or value.get("key") != key
    ):
        raise ValueError("Submission attempt journal has a foreign or invalid identity")
    _validate_argv(value.get("argv"))
    stamp = value.get("started_at")
    if type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp < 0:
        raise ValueError("Submission attempt timestamp is invalid")
    if value.get("state") not in ("starting", "finished"):
        raise ValueError("Submission attempt state is invalid")
    if value["state"] == "finished" and type(value.get("returncode")) is not int:
        raise ValueError("Submission helper outcome is invalid")
    return value


def _receipt(value, root):
    if not isinstance(value, dict):
        raise ValueError("Submission receipt must be an object")
    if ("job_id" in value) == ("plan_id" in value):
        raise ValueError("Submission receipt must identify exactly one job or plan")
    kind = "job" if "job_id" in value else "plan"
    handle = value[kind + "_id"]
    if not isinstance(handle, str) or HANDLE.fullmatch(handle) is None:
        raise ValueError("Submission receipt handle is invalid")
    cwd = value.get("cwd")
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        raise ValueError("Submission receipt checkout is invalid")
    if kind == "job" and value.get("log_path") != str(
        root / "logs" / (handle + ".log")
    ):
        raise ValueError("Submission receipt job log path does not match its identity")
    if kind == "plan" and (
        value.get("state") != "created" or value.get("reviewed_task") is not True
    ):
        raise ValueError("Submission receipt does not confirm plan creation")
    return kind, handle, cwd


def _failure(error):
    return "Inspection unavailable ({}): {}".format(
        type(error).__name__, str(error)[:240]
    )


def inspect_submissions(root, keys):
    """Inspect one bounded batch on its worker; never initialize or recover state."""
    if not isinstance(keys, (tuple, list)) or len(keys) > 16:
        raise ValueError("Submission inspection requires a bounded batch")
    root = Path(root)
    results, candidates = {}, []
    for key in keys:
        if not isinstance(key, str) or KEY.fullmatch(key) is None:
            continue
        receipt, unused_transcript, journal = _paths(root, key)
        try:
            attempt = _journal(_read_json(journal), key)
            code = (
                attempt.get("returncode")
                if attempt and attempt["state"] == "finished"
                else None
            )
            value = _read_json(receipt)
            results[key] = SubmissionInspection(key, returncode=code)
            if value is not None:
                if attempt is None:
                    raise ValueError(
                        "Submission receipt has no matching attempt journal"
                    )
                kind, handle, cwd = _receipt(value, root)
                if attempt["argv"][0] == "run" and kind != "job":
                    raise ValueError(
                        "Submission receipt kind does not match this run attempt"
                    )
                candidates.append((key, kind, handle, cwd, attempt))
        except (OSError, ValueError, TypeError, RecursionError, OverflowError) as error:
            results[key] = SubmissionInspection(key, detail=_failure(error))
    if candidates:
        reader = None
        try:
            reader = Store(root, read_only=True)
            if reader.db is not None:
                reader.db.execute("PRAGMA busy_timeout=1000")
            with reader.read_snapshot():
                for key, kind, handle, cwd, attempt in candidates:
                    try:
                        if kind == "job":
                            reader.get_control(handle)
                            row = reader.db.execute(
                                "SELECT cwd,created FROM jobs WHERE id=?", (handle,)
                            ).fetchone()
                        else:
                            row = (
                                reader.db.execute(
                                    "SELECT cwd,created FROM plans WHERE id=?",
                                    (handle,),
                                ).fetchone()
                                if reader.db
                                else None
                            )
                        if row is None or row[0] != cwd:
                            raise ValueError(
                                "Receipt handle is not confirmed in this queue and checkout"
                            )
                        if attempt and row[1] < attempt["started_at"]:
                            raise ValueError(
                                "Receipt points to work created before this submission attempt"
                            )
                        results[key] = results[key]._replace(kind=kind, handle=handle)
                    except Exception as error:
                        results[key] = results[key]._replace(detail=_failure(error))
        except Exception as error:
            for (
                key,
                unused_kind,
                unused_handle,
                unused_cwd,
                unused_attempt,
            ) in candidates:
                results[key] = results[key]._replace(detail=_failure(error))
        finally:
            if reader is not None:
                reader.close()
    return tuple(
        results[key] for key in keys if isinstance(key, str) and key in results
    )


@dataclass
class _Attempt:
    status: SubmissionStatus
    started: float
    process: object = None
    returncode: Optional[int] = None
    journal_code: Optional[int] = None
    kind: Optional[str] = None
    handle: Optional[str] = None
    inspection_detail: str = ""
    launch_failed: bool = False

    def active(self):
        return not self.launch_failed and (
            self.kind is None or self.process is not None
        )


class SubmissionTracker:
    """The UI only launches/polls helpers and adopts immutable batch observations."""

    def __init__(self, root, submit=None, interval=0.5, timeout=15, limit=16):
        if (
            type(limit) is not int
            or not 1 <= limit <= 16
            or type(interval) not in (int, float)
            or not math.isfinite(interval)
            or interval <= 0
            or type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("Submission tracking needs positive bounds")
        self.root = Path(root)
        if not self.root.is_absolute():
            raise ValueError("Submission state root must be absolute")
        self.submit = submit or submit_read
        self.interval, self.timeout, self.limit = interval, timeout, limit
        self._attempts, self._changed = OrderedDict(), OrderedDict()
        self.future = None
        self.pending_keys = ()
        self.next_read = 0

    @property
    def statuses(self):
        return tuple(attempt.status for attempt in self._attempts.values())

    @property
    def processes(self):
        return tuple(
            attempt.process
            for attempt in self._attempts.values()
            if attempt.process is not None
        )

    def get(self, key):
        attempt = self._attempts.get(key)
        return attempt.status if attempt is not None else None

    def _set(self, attempt, status):
        if status != attempt.status:
            attempt.status = status
            self._changed[status.key] = status

    def _status(self, attempt, now):
        old = attempt.status
        code = (
            attempt.returncode
            if attempt.returncode is not None
            else attempt.journal_code
        )
        if attempt.kind:
            phase = "submitted"
            detail = "Created {} {}; receipt and queue identity confirmed. Execution outcome is not verified.".format(
                attempt.kind, attempt.handle
            )
            if code is not None and code != 0:
                detail = "Created {} {}; helper exited {}. Control startup is unconfirmed; inspect the transcript before another request.".format(
                    attempt.kind, attempt.handle, code
                )
            elif attempt.inspection_detail:
                detail += " " + attempt.inspection_detail
        elif attempt.launch_failed:
            phase, detail = "unconfirmed", attempt.inspection_detail
        elif code is not None:
            phase = "unconfirmed"
            detail = "Helper exited {} without a confirmed receipt; inspect the attempt and transcript. Do not retry automatically.".format(
                code
            )
            if attempt.inspection_detail:
                detail += " " + attempt.inspection_detail
        elif attempt.inspection_detail or now - attempt.started >= self.timeout:
            phase = "unconfirmed"
            detail = (
                (attempt.inspection_detail + " " if attempt.inspection_detail else "")
                + "Receipt not confirmed; inspection continues for a late receipt. Do not retry automatically."
            )
        else:
            phase, detail = (
                "starting",
                "Starting; awaiting a receipt confirmed against the queue.",
            )
        self._set(
            attempt,
            old._replace(
                phase=phase, kind=attempt.kind, handle=attempt.handle, detail=detail
            ),
        )

    def _prune(self):
        settled = [
            key for key, attempt in self._attempts.items() if not attempt.active()
        ]
        for key in settled[: -self.limit]:
            del self._attempts[key]
            self._changed.pop(key, None)

    def start(self, argv, label, now):
        argv = _validate_argv(argv)
        if type(now) not in (int, float) or not math.isfinite(now):
            raise ValueError("Submission clock must be finite")
        if sum(attempt.active() for attempt in self._attempts.values()) >= self.limit:
            raise ValueError(
                "Too many unresolved dashboard submissions; inspect existing attempts first"
            )
        key = uuid.uuid4().hex
        if key in self._attempts:
            raise ValueError("Submission attempt key already exists")
        receipt, transcript, unused_journal = _paths(self.root, key)
        label = "".join(char if char.isprintable() else " " for char in str(label))[
            :256
        ]
        status = SubmissionStatus(
            key,
            label,
            "starting",
            None,
            None,
            "Starting; awaiting a receipt confirmed against the queue.",
            str(receipt),
            str(transcript),
        )
        attempt = _Attempt(status, now)
        self._attempts[key], self._changed[key] = attempt, status
        env = dict(os.environ, AGENT_BUILDS_STATE=str(self.root))
        source = str(Path(__file__).parent.parent)
        env["PYTHONPATH"] = source + os.pathsep + env.get("PYTHONPATH", "")
        try:
            attempt.process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "agent_builds.tui_submissions",
                    str(self.root),
                    key,
                    json.dumps(argv, ensure_ascii=False),
                ],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except (OSError, ValueError) as error:
            attempt.launch_failed = True
            attempt.inspection_detail = "Submission helper did not launch ({}); no creation is confirmed.".format(
                type(error).__name__
            )
            self._status(attempt, now)
            self._prune()
            raise
        self.next_read = 0
        self._prune()
        return key

    def _collect(self, now):
        if self.future is None or not self.future.done():
            return
        future, keys = self.future, self.pending_keys
        self.future, self.pending_keys = None, ()
        try:
            observations = future.result()
            if (
                not isinstance(observations, (tuple, list))
                or len(observations) > self.limit
            ):
                raise ValueError("Invalid submission inspection result")
        except Exception as error:
            observations = tuple(
                SubmissionInspection(key, detail=_failure(error)) for key in keys
            )
        for observation in observations:
            if (
                not isinstance(observation, SubmissionInspection)
                or observation.key not in keys
                or not isinstance(observation.detail, str)
                or (
                    observation.returncode is not None
                    and type(observation.returncode) is not int
                )
            ):
                continue
            attempt = self._attempts.get(observation.key)
            if attempt is None:
                continue
            attempt.inspection_detail = observation.detail
            attempt.journal_code = observation.returncode
            if (
                observation.kind in ("job", "plan")
                and isinstance(observation.handle, str)
                and HANDLE.fullmatch(observation.handle)
            ):
                if attempt.kind and (attempt.kind, attempt.handle) != (
                    observation.kind,
                    observation.handle,
                ):
                    attempt.inspection_detail = "A changed receipt was ignored; the previously confirmed handle is retained."
                else:
                    attempt.kind, attempt.handle = observation.kind, observation.handle
            self._status(attempt, now)
        self.next_read = now + self.interval

    def poll(self, now):
        for attempt in self._attempts.values():
            if attempt.process is not None:
                code = attempt.process.poll()
                if code is not None:
                    attempt.returncode, attempt.process = code, None
            self._status(attempt, now)
        self._collect(now)
        keys = tuple(key for key, attempt in self._attempts.items() if attempt.active())
        if keys and self.future is None and now >= self.next_read:
            self.pending_keys = keys
            try:
                self.future = self.submit(
                    lambda batch: inspect_submissions(self.root, batch), keys
                )
            except (RuntimeError, OSError) as error:
                self.pending_keys = ()
                self.next_read = now + self.interval
                for key in keys:
                    attempt = self._attempts[key]
                    attempt.inspection_detail = _failure(error)
                    self._status(attempt, now)
            self._collect(now)
        self._prune()
        changed = list(self._changed.values())
        self._changed.clear()
        return changed


def _atomic_json(path, value, initial=False):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    descriptor = os.open(
        str(temporary),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as output:
            json.dump(value, output)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        if initial:
            os.link(str(temporary), str(path))
            temporary.unlink()
        else:
            temporary.replace(path)
        directory = os.open(
            str(path.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class _Transcript:
    encoding = "utf-8"

    def __init__(self, output):
        self.output, self.written = output, 0

    def write(self, text):
        encoded = str(text).encode("utf-8", errors="replace")
        remaining = MAX_TRANSCRIPT_BYTES - self.written
        if remaining > 0:
            if len(encoded) > remaining:
                marker = b"\n[submission transcript clipped]\n"
                encoded = (
                    encoded[: max(0, remaining - len(marker))] + marker[:remaining]
                )
            self.output.write(encoded)
            self.written += len(encoded)
        return len(text)

    def flush(self):
        self.output.flush()


def run_helper(root, key, argv):
    if not isinstance(key, str) or KEY.fullmatch(key) is None:
        raise ValueError("Invalid dashboard submission key")
    argv = _validate_argv(argv)
    root = Path(root).expanduser().resolve()
    receipt, transcript, journal = _paths(root, key)
    command = _command(argv, receipt)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    (root / "logs").mkdir(mode=0o700, exist_ok=True)
    attempt = dict(
        schema=JOURNAL_SCHEMA,
        key=key,
        argv=argv,
        state="starting",
        started_at=time.time(),
    )
    _atomic_json(journal, attempt, initial=True)
    code = 2
    try:
        descriptor = os.open(
            str(transcript),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as output:
                sink = _Transcript(output)
                with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                    previous = sys.argv
                    previous_state = os.environ.get("AGENT_BUILDS_STATE")
                    sys.argv = ["buildr"] + command
                    os.environ["AGENT_BUILDS_STATE"] = str(root)
                    try:
                        from .__main__ import main as cli_main

                        result = cli_main()
                        code = result if type(result) is int else 0
                    except SystemExit as error:
                        code = (
                            error.code
                            if type(error.code) is int
                            else 0
                            if error.code is None
                            else 2
                        )
                        if error.code is not None and type(error.code) is not int:
                            print(str(error.code), file=sys.stderr)
                    except BaseException as error:
                        traceback.print_exc()
                        code = 130 if isinstance(error, KeyboardInterrupt) else 2
                    finally:
                        sys.argv = previous
                        if previous_state is None:
                            os.environ.pop("AGENT_BUILDS_STATE", None)
                        else:
                            os.environ["AGENT_BUILDS_STATE"] = previous_state
                    sink.flush()
                os.fsync(output.fileno())
        finally:
            os.close(descriptor)
    finally:
        attempt.update(state="finished", finished_at=time.time(), returncode=code)
        _atomic_json(journal, attempt)
    return code


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3 or len(args[2].encode("utf-8")) > MAX_JSON_BYTES:
        return 2
    try:
        return run_helper(args[0], args[1], json.loads(args[2]))
    except (OSError, ValueError, TypeError, RecursionError):
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
