"""Dashboard data reads own their connections and never initialize queue state."""

import contextlib
import os
from pathlib import Path

from . import queue_health
from .store import Store


def state_root(root=None):
    """Resolve the Store state path without opening SQLite or creating files."""
    return (
        Path(
            root
            or os.environ.get("AGENT_BUILDS_STATE")
            or Path.home() / ".local/state/agent-builds"
        )
        .expanduser()
        .resolve()
    )


def _open(root, read_only):
    store = Store(root, read_only=read_only)
    try:
        if store.db is not None:
            store.db.execute("PRAGMA busy_timeout=1000")
    except BaseException:
        store.close()
        raise
    return store


@contextlib.contextmanager
def _reader(root):
    store = _open(root, read_only=True)
    try:
        yield store
    finally:
        store.close()


class LazyStore:
    """UI-thread access for explicit tools; workers receive only ``root``.

    Existing read-only tools can use the Store interface. A separate writable
    Store is opened only by writable() from a deliberate action flow.
    """

    def __init__(self, root=None):
        self.root = state_root(root)
        self._reader = None
        self._writer = None

    def __getattr__(self, name):
        # A read-only open of absent state deliberately has no connection.
        # Explicit tools must notice later initialization by another process or
        # by this dashboard's separate writable Store.
        if (
            self._reader is not None
            and self._reader.db is None
            and self._reader.path.exists()
        ):
            previous, self._reader = self._reader, None
            previous.close()
        if self._reader is None:
            self._reader = _open(self.root, read_only=True)
        return getattr(self._reader, name)

    def writable(self):
        if self._writer is None:
            self._writer = _open(self.root, read_only=False)
        return self._writer

    def close(self):
        reader, writer = self._reader, self._writer
        self._reader = self._writer = None
        try:
            if reader is not None:
                reader.close()
        finally:
            if writer is not None:
                writer.close()


def browser(root):
    """Load bounded display rows and their settings from one read snapshot."""
    with _reader(root) as reader:
        with reader.read_snapshot():
            return dict(
                initialized=reader.db is not None,
                rows=reader.browser_rows(),
                config=reader.config(),
            )


def selected(root, key):
    """Load evidence only for the selected job, on the calling worker thread."""
    kind, job = key
    if kind not in ("details", "context"):
        raise ValueError("Unknown selected-job read: " + str(kind))
    with _reader(root) as reader:
        with reader.read_snapshot():
            return (
                reader.get(job) if kind == "details" else reader.get_wait_context(job)
            )


def summary(root):
    """Load global queue health independently of the bounded browser rows."""
    with _reader(root) as reader:
        return queue_health.report(reader)


def tool(root, key):
    """Load one explicit tool view; read errors belong to its snapshot status."""
    kind, repository = key
    if kind not in (
        "announcements",
        "workers",
        "plans",
        "groups",
        "watches",
        "advisor",
    ):
        raise ValueError("Unknown tool read: " + str(kind))
    with _reader(root) as reader:
        if kind == "advisor":
            from .advice import report

            # The advisor owns its read_snapshot; another BEGIN would nest it.
            return report(reader, repo=repository)
        with reader.read_snapshot():
            if kind == "announcements":
                from .announce import deliveries

                return deliveries(reader)
            if kind == "workers":
                from .profiles import load
                from .remote import dispatches

                return dict(
                    records=dispatches(reader),
                    workers=list(load(reader).get("workers", {})),
                )
            if kind == "plans":
                from .plans import listing
            elif kind == "groups":
                from .groups import listing
            else:
                from .external import listing
            return listing(reader, limit=20)
