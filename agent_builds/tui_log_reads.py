"""Immutable log snapshots with one disposable, read-only worker in flight."""

from pathlib import Path
from typing import NamedTuple, Optional, Tuple

from .tui_logs import (
    MAX_BYTES,
    TAIL_BYTES,
    NonRegularLog,
    clean_lines,
    read_recent,
    signature,
)
from .tui_reads import submit_read


LogKey = Tuple[str, Optional[str], str]
Signature = Tuple[int, int, int, int, int]


class LogRequest(NamedTuple):
    epoch: int
    key: LogKey
    known_signature: Optional[Signature] = None
    force_content: bool = False


class LogResult(NamedTuple):
    request: LogRequest
    signature: Optional[Signature] = None
    # None means unchanged; an empty tuple is a newly observed empty snapshot.
    lines: Optional[Tuple[str, ...]] = None
    clipped: bool = False
    error: Optional[str] = None


def error_message(error):
    if isinstance(error, (NonRegularLog, IsADirectoryError)):
        return "Log is not a regular file"
    if isinstance(error, FileNotFoundError):
        return "Log not available yet"
    return "Cannot read log (" + type(error).__name__ + ")"


def load_log(request):
    """Own all filesystem access; never receive or mutate a view/cache object."""
    mode, unused_job, path = request.key
    if mode not in ("inline", "focused"):
        raise ValueError("Unknown log read mode: " + str(mode))
    try:
        current = signature(Path(path).stat())
        if current == request.known_signature and not request.force_content:
            return LogResult(request, current)
        data, observed, clipped = read_recent(
            path, TAIL_BYTES if mode == "inline" else MAX_BYTES
        )
        if mode == "inline":
            # Inline output retains its historical splitlines semantics; the
            # painter strips controls only from the displayed lines.
            lines = data.decode("utf-8", errors="replace").splitlines()
        else:
            if clipped and b"\n" in data:
                data = data.split(b"\n", 1)[1]
            lines = clean_lines(data)
        return LogResult(request, observed, tuple(lines), clipped)
    except OSError as error:
        return LogResult(request, lines=(), error=error_message(error))


class LogReads:
    """Deliver accepted results once, without waiting or accumulating requests.

    All methods and cache adoption run on the UI thread. invalidate() is an
    epoch barrier for hold/search/End, including a return to the same source.
    A hidden or obsolete worker is retained until it finishes; no second worker
    can bypass it. The daemon worker is never joined when the UI exits.
    """

    def __init__(self, submit=None, interval=0.5):
        self.submit = submit or submit_read
        self.interval = interval
        self.key = None
        self.epoch = 0
        self.future = self.pending_request = None
        self.error = self.observed = None
        self.next_read = 0

    def invalidate(self):
        self.epoch += 1
        self.next_read = 0
        self.error = self.observed = None

    def hide(self):
        if self.key is not None:
            self.key = None
            self.invalidate()

    def _accept(self, result, now):
        self.error = result.error
        if not result.error:
            self.observed = now
        self.next_read = now + self.interval
        return result

    def _collect(self, now):
        if self.future is None or not self.future.done():
            return None
        future, request = self.future, self.pending_request
        self.future = self.pending_request = None
        try:
            result = future.result()
            if not isinstance(result, LogResult) or result.request != request:
                raise ValueError("Invalid log snapshot result")
        except Exception as error:
            result = LogResult(request, lines=(), error=error_message(error))
        if self.key is None or request.epoch != self.epoch or request.key != self.key:
            return None
        return self._accept(result, now)

    def get(self, key, known_signature=None, force_content=False, now=0):
        if key is None:
            self.hide()
        else:
            mode, job, path = key
            if mode not in ("inline", "focused"):
                raise ValueError("Unknown log read mode: " + str(mode))
            key = (mode, job, str(path))
            if key != self.key:
                self.key = key
                self.invalidate()
        result = self._collect(now)
        if self.key is not None and self.future is None and now >= self.next_read:
            request = LogRequest(self.epoch, self.key, known_signature, force_content)
            self.pending_request = request
            try:
                self.future = self.submit(load_log, request)
            except (RuntimeError, OSError) as error:
                self.pending_request = None
                return self._accept(
                    LogResult(request, lines=(), error=error_message(error)), now
                )
            # Eager fixture submissions and already-finished workers can be
            # adopted immediately; done() never blocks and no loop resubmits.
            immediate = self._collect(now)
            if immediate is not None:
                result = immediate
        return result

    def status(self, now):
        if self.error:
            return self.error
        if self.observed is None:
            return "Loading log; navigation stays available"
        return "Snapshot {:.0f}s ago{}".format(
            max(0, now - self.observed),
            "; refreshing" if self.future is not None else "",
        )
