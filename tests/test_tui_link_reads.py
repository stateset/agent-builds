"""Linked jobs use exact display projections on worker-owned read snapshots."""

import contextlib
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from agent_builds import tui_data
from agent_builds.store import BROWSER_COLUMNS, Store
from agent_builds.tui_browser import filter_rows


class LinkedJobReadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.store = Store(self.base / "state")
        self.addCleanup(self.store.close)
        self.target = self.submit("target")
        self.source = self.submit("source")
        self.link(self.source, dict(id=self.target))

    def submit(self, name):
        # Durable fixture rows only; these tests never launch native jobs.
        location = self.base / name
        location.mkdir()
        return self.store.submit(
            location,
            ["printf", "%s", "two words"],
            "local",
            "quick",
            2,
            0.25,
            name,
            "",
            5,
        )

    def link(self, source, blocker):
        self.store.db.execute(
            "UPDATE jobs SET wait_context=? WHERE id=?",
            (json.dumps(dict(blocking_job=blocker)), source),
        )

    def test_exact_row_matches_browser_projection_and_keeps_display_fields(self):
        origin = dict(pane_id="pane", tab_id="tab", workspace_id="workspace")
        self.store.db.execute(
            "UPDATE jobs SET state='held',created=10,started=NULL,finished=NULL,"
            "reason='fixture wait',herdr=? WHERE id=?",
            (json.dumps(origin), self.target),
        )
        with mock.patch("agent_builds.store.time.time", return_value=30):
            row = self.store.browser_row(self.target)
            rows = self.store.browser_rows()
        self.assertEqual(row, next(item for item in rows if item["id"] == self.target))
        self.assertEqual(row["state"], "queued")
        self.assertEqual(row["storage_state"], "held")
        self.assertEqual(row["wait_seconds"], 20)
        self.assertEqual(row["elapsed_seconds"], 0)
        self.assertEqual(row["command_display"], "printf %s 'two words'")
        self.assertEqual(row["origin"], origin)
        self.assertEqual(row["reason"], "fixture wait")
        self.assertEqual(row["cwd"], str(self.base / "target"))
        self.assertEqual(row["log_path"], str(self.store.log_path(self.target)))
        self.assertNotIn("herdr", row)
        self.assertEqual(
            set(row),
            set(BROWSER_COLUMNS.split(",")) - {"herdr"}
            | {
                "storage_state",
                "command_display",
                "origin",
                "log_path",
                "wait_seconds",
                "elapsed_seconds",
            },
        )

    def test_projection_preserves_running_finished_and_nonnegative_timings(self):
        for state, created, started, finished, wait, elapsed in (
            ("running", 10, 15, None, 5, 15),
            ("succeeded", 10, 15, 25, 5, 10),
            ("failed", 10, None, 25, 15, 0),
            ("queued", 40, None, None, 0, 0),
            ("running", 40, 35, None, 0, 0),
        ):
            with self.subTest(state=state, created=created):
                self.store.db.execute(
                    "UPDATE jobs SET state=?,created=?,started=?,finished=? WHERE id=?",
                    (state, created, started, finished, self.target),
                )
                with mock.patch("agent_builds.store.time.time", return_value=30):
                    row = self.store.browser_row(self.target)
                self.assertEqual(row["state"], state)
                self.assertEqual(row["wait_seconds"], wait)
                self.assertEqual(row["elapsed_seconds"], elapsed)

    def test_exact_lookup_reaches_jobs_beyond_browser_limit_and_display_filters(self):
        self.store.db.execute(
            "UPDATE jobs SET state='failed',exit_code=7 WHERE id=?", (self.target,)
        )
        for index in range(205):
            self.submit("backlog-{}".format(index))
        rows = self.store.browser_rows()
        self.assertEqual(len(rows), 200)
        self.assertNotIn(self.target, [row["id"] for row in rows])
        self.assertEqual(
            filter_rows(rows, "problems", "unmatched", str(self.base / "other")), []
        )
        self.assertEqual(
            tui_data.selected(self.store.root, ("browser", self.target))["id"],
            self.target,
        )
        self.assertEqual(
            tui_data.selected(self.store.root, ("blocker", self.source))["id"],
            self.target,
        )

    def test_lookup_is_exact_id_parameterized_and_selects_only_browser_columns(self):
        statements = []
        self.store.db.set_trace_callback(statements.append)
        try:
            self.assertEqual(self.store.browser_row(self.target)["id"], self.target)
            for unknown in (self.target[:-1], self.target + "extra", "' OR 1=1 --"):
                with self.subTest(unknown=unknown):
                    with self.assertRaisesRegex(ValueError, "Unknown job"):
                        self.store.browser_row(unknown)
        finally:
            self.store.db.set_trace_callback(None)
        self.assertEqual(len(statements), 4)
        for statement in statements:
            self.assertTrue(
                statement.startswith(
                    "SELECT " + BROWSER_COLUMNS + " FROM jobs WHERE id="
                )
            )
            self.assertNotIn("ORDER BY", statement)
            self.assertNotIn("SELECT *", statement)

    def test_unselected_evidence_is_not_read_or_decoded(self):
        self.store.db.execute(
            "UPDATE jobs SET source=?,usage=?,admission=?,batch=?,wait_context=? WHERE id=?",
            ("invalid evidence" * 10000,) * 5 + (self.target,),
        )
        forbidden = (
            "get",
            "describe",
            "get_wait_context",
            "browser_rows",
            "config",
            "recover",
            "_ensure_schema",
        )
        with contextlib.ExitStack() as guards:
            for name in forbidden:
                guards.enter_context(
                    mock.patch.object(Store, name, side_effect=AssertionError(name))
                )
            row = tui_data.selected(self.store.root, ("browser", self.target))
        self.assertEqual(row["id"], self.target)
        for field in ("source", "usage", "admission", "batch", "wait_context"):
            self.assertNotIn(field, row)

    def test_only_browser_command_and_origin_json_are_decoded(self):
        for field in ("command", "herdr"):
            original = self.store.db.execute(
                "SELECT " + field + " FROM jobs WHERE id=?", (self.target,)
            ).fetchone()[0]
            self.store.db.execute(
                "UPDATE jobs SET " + field + "=? WHERE id=?",
                ("invalid JSON", self.target),
            )
            try:
                with self.subTest(field=field):
                    with self.assertRaises(json.JSONDecodeError):
                        tui_data.selected(self.store.root, ("browser", self.target))
            finally:
                self.store.db.execute(
                    "UPDATE jobs SET " + field + "=? WHERE id=?",
                    (original, self.target),
                )

    def test_blocker_is_resolved_from_recorded_id_and_refreshed_target_projection(self):
        self.link(self.source, dict(id=self.target, state="running", log_path="wrong"))
        self.store.db.execute(
            "UPDATE jobs SET state='failed',exit_code=7 WHERE id=?", (self.target,)
        )
        with mock.patch.object(
            Store, "get", side_effect=AssertionError("full evidence load")
        ), mock.patch.object(
            Store, "describe", side_effect=AssertionError("evidence expansion")
        ), mock.patch.object(
            Store, "browser_rows", side_effect=AssertionError("bounded list lookup")
        ):
            row = tui_data.selected(self.store.root, ("blocker", self.source))
        self.assertEqual(row["id"], self.target)
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["exit_code"], 7)
        self.assertEqual(row["log_path"], str(self.store.log_path(self.target)))

    def test_absent_malformed_and_missing_target_blockers_fail_honestly(self):
        for blocker in (None, {}, [], "other", dict(id=None), dict(id=[]), dict(id="")):
            self.link(self.source, blocker)
            with self.subTest(blocker=blocker):
                with self.assertRaisesRegex(ValueError, "No recorded blocker"):
                    tui_data.selected(self.store.root, ("blocker", self.source))
        self.store.db.execute(
            "UPDATE jobs SET wait_context='{}' WHERE id=?", (self.source,)
        )
        with self.assertRaisesRegex(ValueError, "No recorded blocker"):
            tui_data.selected(self.store.root, ("blocker", self.source))
        self.link(self.source, dict(id="unknown-target"))
        with self.assertRaisesRegex(ValueError, "Unknown job: unknown-target"):
            tui_data.selected(self.store.root, ("blocker", self.source))
        with self.assertRaisesRegex(ValueError, "Unknown job: unknown-source"):
            tui_data.selected(self.store.root, ("blocker", "unknown-source"))

    def test_worker_calls_own_close_and_bound_read_only_snapshot_connections(self):
        opened, operations = [], []

        class Reader(Store):
            def __init__(self, *args, **kwargs):
                if kwargs.get("read_only") is not True:
                    raise AssertionError("writable inspection")
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

            def get_wait_context(self, job):
                operations.append(
                    ("context", job, threading.get_ident(), self.db.in_transaction)
                )
                return super().get_wait_context(job)

            def browser_row(self, job):
                operations.append(
                    ("browser", job, threading.get_ident(), self.db.in_transaction)
                )
                return super().browser_row(job)

            def close(self):
                self.closed_thread = threading.get_ident()
                self.changed = self.db.total_changes
                self.transaction_on_close = self.db.in_transaction
                super().close()

        with mock.patch.object(tui_data, "Store", Reader), mock.patch.object(
            Store, "_ensure_schema", side_effect=AssertionError("migration")
        ), mock.patch.object(Store, "recover", side_effect=AssertionError("recovery")):
            with ThreadPoolExecutor(max_workers=1) as worker:
                direct = worker.submit(
                    tui_data.selected, self.store.root, ("browser", self.target)
                ).result(5)
                linked = worker.submit(
                    tui_data.selected, self.store.root, ("blocker", self.source)
                ).result(5)
        self.assertEqual(direct["id"], linked["id"])
        self.assertEqual(len(opened), 2)
        self.assertIsNot(opened[0].db, opened[1].db)
        self.assertEqual(
            [(kind, job) for kind, job, thread, snapshot in operations],
            [
                ("browser", self.target),
                ("context", self.source),
                ("browser", self.target),
            ],
        )
        for kind, job, thread, snapshot in operations:
            self.assertNotEqual(thread, threading.get_ident())
            self.assertTrue(snapshot)
        for reader in opened:
            self.assertTrue(reader.read_only)
            self.assertEqual(reader.thread, reader.closed_thread)
            self.assertEqual(reader.snapshots, 1)
            self.assertTrue(reader.in_snapshot)
            self.assertEqual(reader.busy_timeout, 1000)
            self.assertEqual(reader.query_only, 1)
            self.assertEqual(reader.changed, 0)
            self.assertFalse(reader.transaction_on_close)
            with self.assertRaises(sqlite3.ProgrammingError):
                reader.db.execute("SELECT 1")

    def test_readers_close_on_lookup_context_json_and_query_errors(self):
        scenarios = (
            ("browser", "absent", None, ValueError),
            ("blocker", "absent", None, ValueError),
            ("blocker", self.source, "invalid JSON", json.JSONDecodeError),
            ("blocker", self.source, "[]", ValueError),
        )
        for kind, handle, context, expected in scenarios:
            if context is not None:
                self.store.db.execute(
                    "UPDATE jobs SET wait_context=? WHERE id=?", (context, self.source)
                )
            opened = []

            def reader(*args, **kwargs):
                connection = Store(*args, **kwargs)
                connection.close = mock.Mock(wraps=connection.close)
                opened.append(connection)
                return connection

            with self.subTest(kind=kind, context=context), mock.patch.object(
                tui_data, "Store", side_effect=reader
            ):
                with self.assertRaises(expected):
                    tui_data.selected(self.store.root, (kind, handle))
            self.assertEqual(len(opened), 1)
            opened[0].close.assert_called_once_with()
            with self.assertRaises(sqlite3.ProgrammingError):
                opened[0].db.execute("SELECT 1")
        for kind in ("browser", "blocker"):
            opened = []
            with self.subTest(kind=kind), mock.patch.object(
                Store,
                "browser_row",
                side_effect=sqlite3.OperationalError("fixture busy"),
            ), mock.patch.object(tui_data, "Store", side_effect=reader):
                self.link(self.source, dict(id=self.target))
                with self.assertRaisesRegex(sqlite3.OperationalError, "fixture busy"):
                    tui_data.selected(self.store.root, (kind, self.source))
            self.assertEqual(len(opened), 1)
            opened[0].close.assert_called_once_with()
            with self.assertRaises(sqlite3.ProgrammingError):
                opened[0].db.execute("SELECT 1")

    def test_missing_state_is_not_initialized_for_either_read(self):
        missing = self.base / "missing"
        with mock.patch.object(
            Store, "_ensure_schema", side_effect=AssertionError("migration")
        ), mock.patch.object(Store, "recover", side_effect=AssertionError("recovery")):
            for kind in ("browser", "blocker"):
                with self.subTest(kind=kind):
                    with self.assertRaisesRegex(ValueError, "Unknown job"):
                        tui_data.selected(missing, (kind, self.target))
        self.assertFalse(missing.exists())

    def test_legacy_projection_schema_works_without_initialization_or_evidence(self):
        legacy = self.base / "legacy"
        legacy.mkdir()
        path = legacy / "queue.sqlite3"
        connection = sqlite3.connect(str(path))
        try:
            connection.execute(
                "CREATE TABLE jobs(id TEXT PRIMARY KEY,state TEXT,created REAL,"
                "started REAL,finished REAL,cwd TEXT,command TEXT,backend TEXT,"
                "slots INTEGER,memory_gb REAL,exit_code INTEGER,reason TEXT,herdr TEXT,"
                "wait_context TEXT)"
            )
            for handle, state, context in (
                (self.target, "held", "{}"),
                (
                    self.source,
                    "queued",
                    json.dumps(dict(blocking_job=dict(id=self.target))),
                ),
            ):
                connection.execute(
                    "INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        handle,
                        state,
                        10,
                        None,
                        None,
                        str(self.base),
                        '["true"]',
                        "local",
                        1,
                        0.25,
                        None,
                        "",
                        "{}",
                        context,
                    ),
                )
            connection.commit()
        finally:
            connection.close()
        before = path.read_bytes()
        with mock.patch.object(
            Store, "_ensure_schema", side_effect=AssertionError("migration")
        ), mock.patch.object(Store, "recover", side_effect=AssertionError("recovery")):
            direct = tui_data.selected(legacy, ("browser", self.target))
            linked = tui_data.selected(legacy, ("blocker", self.source))
        for row in (direct, linked):
            self.assertEqual(row["id"], self.target)
            self.assertEqual(row["state"], "queued")
            self.assertEqual(row["storage_state"], "held")
            self.assertEqual(
                row["log_path"], str(legacy / "logs" / (self.target + ".log"))
            )
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual([item.name for item in legacy.iterdir()], ["queue.sqlite3"])

    def test_missing_legacy_projection_columns_report_error_without_migration(self):
        legacy = self.base / "incompatible"
        legacy.mkdir()
        path = legacy / "queue.sqlite3"
        connection = sqlite3.connect(str(path))
        try:
            connection.execute("CREATE TABLE jobs(id TEXT,wait_context TEXT)")
            connection.execute(
                "INSERT INTO jobs VALUES(?,?)",
                (self.source, json.dumps(dict(blocking_job=dict(id=self.target)))),
            )
            connection.commit()
        finally:
            connection.close()
        before = path.read_bytes()
        with mock.patch.object(
            Store, "_ensure_schema", side_effect=AssertionError("migration")
        ):
            for kind, handle in (("browser", self.target), ("blocker", self.source)):
                with self.subTest(kind=kind):
                    with self.assertRaises(sqlite3.OperationalError):
                        tui_data.selected(legacy, (kind, handle))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual([item.name for item in legacy.iterdir()], ["queue.sqlite3"])


if __name__ == "__main__":
    unittest.main()
