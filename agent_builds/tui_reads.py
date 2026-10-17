"""One bounded in-flight inspection; never share a SQLite connection across threads."""

from concurrent.futures import Future
from threading import Thread


def submit_read(loader, key):
    """Run inspection on a disposable daemon; quitting never joins a slow read.

    This worker is only for read-only inspection, never native jobs or mutations.
    The loader owns and closes its connection. SnapshotRead permits one in flight.
    """
    future = Future()

    def inspect():
        if not future.set_running_or_notify_cancel():
            return
        try:
            future.set_result(loader(key))
        except BaseException as error:
            future.set_exception(error)

    Thread(target=inspect, name="buildr-inspection", daemon=True).start()
    return future


class SnapshotRead:
    """Keep one in-flight read; never wait for it in the keyboard loop."""

    def __init__(self, loader, interval=3, submit=None, label="inspection"):
        self.label = label
        self.loader, self.interval = loader, interval
        self.submit = submit or submit_read
        self.key = self.pending_key = None
        self.epoch, self.pending_epoch = 0, None
        self.future = self.report = None
        self.error = None
        self.observed = None
        self.next_read = 0

    def get(self, key, now):
        if key != self.key:
            self.key = key
            # A -> B -> A and hide -> reopen are distinct visits to the same key.
            self.epoch += 1
            self.report = self.error = self.observed = None
            self.next_read = now
        if self.future is not None and self.future.done():
            current = (
                key is not None
                and key == self.pending_key
                and self.epoch == self.pending_epoch
            )
            try:
                result = self.future.result()
            except Exception as error:
                if current:
                    self.error = str(error) or type(error).__name__
                    self.next_read = now + self.interval
            else:
                if current:
                    self.report, self.error, self.observed = result, None, now
                    self.next_read = now + self.interval
            self.future = None
        if key is not None and self.future is None and now >= self.next_read:
            self.pending_key = key
            self.pending_epoch = self.epoch
            try:
                self.future = self.submit(self.loader, key)
            except (RuntimeError, OSError) as error:
                self.error = str(error) or type(error).__name__
                self.next_read = now + self.interval
        return self.report

    def status(self, now, short_label=None):
        """Describe the last accepted read; ``~`` marks a compact refresh."""
        if short_label is not None:
            if self.report is None:
                return short_label + (" error" if self.error else " loading")
            age = max(0, now - self.observed)
            stale = bool(self.error or age > 2 * self.interval)
            unit, seconds = next(
                (unit, seconds)
                for unit, seconds in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1))
                if age >= seconds or unit == "s"
            )
            amount = int(age / seconds)
            short_age = ">9d" if unit == "d" and amount > 9 else str(amount) + unit
            return "{}{} {}{}".format(
                "STALE " if stale else "",
                short_label,
                short_age,
                "~" if self.future is not None else "",
            )
        if self.error:
            return (
                "Stale " + self.label + ": "
                if self.report is not None
                else self.label.capitalize() + " unavailable: "
            ) + self.error
        if self.report is None:
            return "Loading " + self.label + "; navigation stays available"
        age = max(0, now - self.observed)
        return "{}Snapshot {:.0f}s ago{}".format(
            "STALE " if age > 2 * self.interval else "",
            age,
            "; refreshing" if self.future is not None else "",
        )
