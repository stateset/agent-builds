"""One explicit read-only linked-log lookup, with no automatic retries."""

from .tui_reads import SnapshotRead


class LogLinks:
    """Retain the originating view until its current lookup succeeds."""

    def __init__(self, loader, submit=None):
        self.read = SnapshotRead(
            lambda request: loader(request[1:]),
            interval=float("inf"),
            submit=submit,
            label="linked job",
        )
        self.serial = 0
        self.request = self.origin = self.handle = None
        self.error = None

    @property
    def status(self):
        if self.request is not None:
            return "Opening log {} | navigation stays available".format(self.handle)
        return "Log unavailable: " + self.error if self.error else ""

    def open(self, kind, handle, origin, now):
        if kind not in ("browser", "blocker"):
            raise ValueError("Unknown linked-job read: " + str(kind))
        if not isinstance(handle, str) or not handle:
            raise ValueError("A linked job ID is required")
        self.serial += 1
        self.request = (self.serial, kind, handle)
        self.origin, self.handle, self.error = origin, handle, None
        self.read.get(self.request, now)

    def cancel(self):
        self.request = self.origin = self.handle = self.error = None
        # Keep an outstanding worker until it finishes; hiding invalidates its
        # generation without cancellation, waiting, or overlapping another read.
        self.read.get(None, 0)

    def poll(self, origin, visible, now):
        if self.request is None:
            self.read.get(None, now)
            return None
        if origin != self.origin:
            self.cancel()
            return None
        if not visible:
            return None
        row = self.read.get(self.request, now)
        if self.read.future is not None and self.read.future.done():
            row = self.read.get(self.request, now)
        error = self.read.error
        if error is None and self.read.future is not None:
            return None
        if error is None:
            valid = isinstance(row, dict) and all(
                isinstance(row.get(field), str) and row[field]
                for field in ("id", "state", "cwd", "log_path")
            )
            if not valid or (self.request[1] == "browser" and row["id"] != self.handle):
                error = "invalid linked-job reply"
        self.request = self.origin = None
        self.error = error
        self.read.get(None, now)
        return row if error is None else None
