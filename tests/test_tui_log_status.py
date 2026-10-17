"""Pinned log status reads remain small and independent of browser selection."""

import contextlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from agent_builds import tui_data
from agent_builds.store import Store


class PinnedLogStatusTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.store = Store(self.base / "state")
        self.addCleanup(self.store.close)
        self.job = self.submit()

    def submit(self):
        # These are persisted fixture rows only; no native runner is launched.
        return self.store.submit(
            self.base, ["true"], "local", "quick", 1, 0.25, str(self.base), "", 5
        )

    def test_exact_small_shape_normalizes_held_and_keeps_cancellation_flag(self):
        for state, expected, cancel in (
            ("held", "queued", 0),
            ("queued", "queued", 0),
            ("running", "running", 0),
            ("cancelling", "cancelling", 1),
            ("failed", "failed", 0),
            ("succeeded", "succeeded", 0),
            ("cancelled", "cancelled", 1),
            ("lost", "lost", 0),
        ):
            with self.subTest(state=state):
                self.store.db.execute(
                    "UPDATE jobs SET state=?,cancel_requested=? WHERE id=?",
                    (state, cancel, self.job),
                )
                before = tuple(
                    self.store.db.execute(
                        "SELECT state,cancel_requested FROM jobs WHERE id=?",
                        (self.job,),
                    ).fetchone()
                )
                changes = self.store.db.total_changes
                self.assertEqual(
                    tui_data.selected(self.store.root, ("control", self.job)),
                    dict(id=self.job, state=expected, cancel_requested=bool(cancel)),
                )
                after = tuple(
                    self.store.db.execute(
                        "SELECT state,cancel_requested FROM jobs WHERE id=?",
                        (self.job,),
                    ).fetchone()
                )
                self.assertEqual(before, (state, cancel))
                self.assertEqual(after, before)
                self.assertEqual(self.store.db.total_changes, changes)

    def test_finished_pinned_job_refreshes_after_falling_outside_browser_limit(self):
        self.store.db.execute("UPDATE jobs SET state='running' WHERE id=?", (self.job,))
        first = tui_data.selected(self.store.root, ("control", self.job))
        self.assertEqual(first["state"], "running")
        for unused in range(205):
            self.submit()
        self.store.db.execute(
            "UPDATE jobs SET state='failed',exit_code=7 WHERE id=?", (self.job,)
        )
        rows = tui_data.browser(self.store.root)["rows"]
        self.assertEqual(len(rows), 200)
        self.assertNotIn(self.job, [row["id"] for row in rows])
        latest = tui_data.selected(self.store.root, ("control", self.job))
        self.assertEqual(
            latest, dict(id=self.job, state="failed", cancel_requested=False)
        )
        self.assertEqual(first["state"], "running")

    def test_control_does_not_parse_or_load_large_job_evidence(self):
        self.store.db.execute(
            "UPDATE jobs SET command=?,source=?,usage=?,wait_context=? WHERE id=?",
            (
                "invalid command JSON",
                "x" * 100000,
                "invalid usage JSON",
                "invalid context JSON",
                self.job,
            ),
        )
        targets = (
            "get",
            "describe",
            "get_wait_context",
            "browser_rows",
            "config",
            "recover",
            "_ensure_schema",
        )
        with contextlib.ExitStack() as guards:
            for name in targets:
                guards.enter_context(
                    mock.patch.object(Store, name, side_effect=AssertionError(name))
                )
            result = tui_data.selected(self.store.root, ("control", self.job))
        self.assertEqual(
            result, dict(id=self.job, state="queued", cancel_requested=False)
        )

    def test_each_call_owns_closes_and_bounds_its_worker_connection(self):
        opened = []

        class Reader(Store):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.thread = threading.get_ident()
                self.snapshots = 0
                opened.append(self)

            @contextlib.contextmanager
            def read_snapshot(self):
                with super().read_snapshot():
                    self.snapshots += 1
                    self.in_snapshot = self.db.in_transaction
                    self.busy_timeout = self.db.execute(
                        "PRAGMA busy_timeout"
                    ).fetchone()[0]
                    self.query_only = self.db.execute("PRAGMA query_only").fetchone()[0]
                    yield

            def close(self):
                self.closed_thread = threading.get_ident()
                self.changed = self.db.total_changes
                self.transaction_on_close = self.db.in_transaction
                super().close()

        with mock.patch.object(tui_data, "Store", Reader):
            with ThreadPoolExecutor(max_workers=1) as worker:
                first = worker.submit(
                    tui_data.selected, self.store.root, ("control", self.job)
                ).result(5)
                second = worker.submit(
                    tui_data.selected, self.store.root, ("control", self.job)
                ).result(5)
        self.assertEqual(first, second)
        self.assertEqual(len(opened), 2)
        self.assertIsNot(opened[0].db, opened[1].db)
        for reader in opened:
            self.assertTrue(reader.read_only)
            self.assertNotEqual(reader.thread, threading.get_ident())
            self.assertEqual(reader.closed_thread, reader.thread)
            self.assertEqual(reader.snapshots, 1)
            self.assertTrue(reader.in_snapshot)
            self.assertEqual(reader.busy_timeout, 1000)
            self.assertEqual(reader.query_only, 1)
            self.assertEqual(reader.changed, 0)
            self.assertFalse(reader.transaction_on_close)
            with self.assertRaises(sqlite3.ProgrammingError):
                reader.db.execute("SELECT 1")

    def test_unknown_job_and_query_errors_propagate_after_closing_reader(self):
        for handle, error in (
            ("absent", ValueError("fixture unknown job")),
            (self.job, sqlite3.OperationalError("fixture unavailable")),
        ):
            opened = []

            def reader(*args, **kwargs):
                connection = Store(*args, **kwargs)
                connection.close = mock.Mock(wraps=connection.close)
                opened.append(connection)
                return connection

            with self.subTest(handle=handle), mock.patch.object(
                tui_data, "Store", side_effect=reader
            ), mock.patch.object(Store, "get_control", side_effect=error):
                with self.assertRaisesRegex(type(error), "fixture"):
                    tui_data.selected(self.store.root, ("control", handle))
            self.assertEqual(len(opened), 1)
            opened[0].close.assert_called_once_with()
            with self.assertRaises(sqlite3.ProgrammingError):
                opened[0].db.execute("SELECT 1")

    def test_missing_state_is_not_initialized(self):
        missing = self.base / "missing"
        with mock.patch.object(
            Store, "_ensure_schema", side_effect=AssertionError("migration")
        ), mock.patch.object(Store, "recover", side_effect=AssertionError("recovery")):
            with self.assertRaisesRegex(ValueError, "Unknown job"):
                tui_data.selected(missing, ("control", self.job))
        self.assertFalse(missing.exists())

    def test_minimal_legacy_control_table_needs_no_evidence_schema_or_migration(self):
        legacy = self.base / "legacy"
        legacy.mkdir()
        path = legacy / "queue.sqlite3"
        connection = sqlite3.connect(str(path))
        try:
            connection.execute(
                "CREATE TABLE jobs(id TEXT,state TEXT,cancel_requested INTEGER)"
            )
            connection.execute("INSERT INTO jobs VALUES(?,?,?)", (self.job, "held", 1))
            connection.commit()
        finally:
            connection.close()
        before = path.read_bytes()
        with mock.patch.object(
            Store, "_ensure_schema", side_effect=AssertionError("migration")
        ):
            result = tui_data.selected(legacy, ("control", self.job))
        self.assertEqual(
            result, dict(id=self.job, state="queued", cancel_requested=True)
        )
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual([item.name for item in legacy.iterdir()], ["queue.sqlite3"])


if __name__ == "__main__":
    unittest.main()
