"""Snapshot loaders inspect isolated state with thread-local read connections."""

import contextlib
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from agent_builds import tui_data
from agent_builds.store import Store
from agent_builds.tui_browser import filter_rows


class TuiDataPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_root_resolution_matches_store_without_initializing_state(self):
        with mock.patch.dict(os.environ, AGENT_BUILDS_STATE=str(self.root / "env")):
            for root in (None, "", self.root / "explicit", self.root / "a/../b"):
                with self.subTest(root=root):
                    reader = Store(root, read_only=True)
                    try:
                        self.assertEqual(tui_data.state_root(root), reader.root)
                        self.assertFalse(reader.root.exists())
                    finally:
                        reader.close()
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            Path, "home", return_value=self.root / "home"
        ):
            self.assertEqual(
                tui_data.state_root(), self.root / "home/.local/state/agent-builds"
            )
        with mock.patch.dict(os.environ, AGENT_BUILDS_STATE="~/buildr-inspection"):
            self.assertEqual(
                tui_data.state_root(),
                Path("~/buildr-inspection").expanduser().resolve(),
            )

    def test_lazy_root_and_close_do_not_open_a_store(self):
        missing = self.root / "missing"
        with mock.patch("agent_builds.tui_data.Store") as constructor:
            lazy = tui_data.LazyStore(missing)
            self.assertEqual(lazy.root, missing)
            lazy.close()
            lazy.close()
        constructor.assert_not_called()
        self.assertFalse(missing.exists())

    def test_missing_state_stays_absent_for_every_read(self):
        missing = self.root / "missing"
        with mock.patch.object(
            Store, "_ensure_schema", side_effect=AssertionError("migration")
        ), mock.patch.object(Store, "recover", side_effect=AssertionError("recovery")):
            browser = tui_data.browser(missing)
            summary = tui_data.summary(missing)
            for kind in ("details", "context"):
                with self.assertRaisesRegex(ValueError, "Unknown job"):
                    tui_data.selected(missing, (kind, "absent"))
            for kind in ("announcements", "plans", "groups", "watches"):
                self.assertEqual(tui_data.tool(missing, (kind, None)), [])
            self.assertEqual(
                tui_data.tool(missing, ("workers", None)), dict(records=[], workers=[])
            )
            advisor = tui_data.tool(missing, ("advisor", None))
        self.assertEqual(browser["rows"], [])
        self.assertFalse(browser["initialized"])
        self.assertFalse(summary["initialized"])
        self.assertEqual(summary["status"], "uninitialized")
        self.assertEqual(advisor["active_sampled"], 0)
        self.assertFalse(advisor["changes_applied"])
        self.assertFalse(missing.exists())

    def test_lazy_missing_reader_reopens_after_external_or_explicit_initialization(
        self,
    ):
        for initializer in ("external", "writable"):
            with self.subTest(initializer=initializer):
                missing = self.root / initializer
                opened = []

                def create(root, read_only=False):
                    if read_only and opened:
                        # Dispose of the absent-state handle before opening its
                        # replacement, even when the writer belongs elsewhere.
                        opened[0].close.assert_called_once_with()
                    store = Store(root, read_only=read_only)
                    store.close = mock.Mock(wraps=store.close)
                    opened.append(store)
                    return store

                with mock.patch("agent_builds.tui_data.Store", side_effect=create):
                    lazy = tui_data.LazyStore(missing)
                    self.addCleanup(lazy.close)
                    self.assertFalse(lazy.config()["paused"])
                    self.assertFalse(lazy.config()["paused"])
                    self.assertEqual(lazy.browser_rows(), [])
                    self.assertIsNone(lazy.db)
                    self.assertEqual(len(opened), 1)
                    self.assertFalse(missing.exists())
                    if initializer == "external":
                        writer = Store(missing)
                        self.addCleanup(writer.close)
                    else:
                        writer = lazy.writable()
                    writer.update_config(dict(paused=True))
                    job = writer.submit(
                        self.root, ["true"], "local", "quick", 1, 0.25, "later", "", 5
                    )
                    self.assertTrue(lazy.config()["paused"])
                    self.assertEqual(lazy.browser_rows()[0]["id"], job)
                    self.assertEqual(len(opened), 2 if initializer == "external" else 3)
                    self.assertTrue(opened[-1].read_only)
                    self.assertIsNot(lazy.db, writer.db)
                    if initializer == "writable":
                        self.assertIs(lazy.writable(), writer)
                    lazy.close()
                for store in opened:
                    store.close.assert_called_once_with()

    def test_incompatible_schema_is_reported_without_migration(self):
        state = self.root / "old"
        state.mkdir()
        connection = sqlite3.connect(str(state / "queue.sqlite3"))
        try:
            connection.execute("CREATE TABLE legacy_marker (value TEXT)")
            connection.commit()
        finally:
            connection.close()
        with mock.patch.object(
            Store, "_ensure_schema", side_effect=AssertionError("migration")
        ):
            for load in (
                lambda: tui_data.browser(state),
                lambda: tui_data.selected(state, ("details", "absent")),
                lambda: tui_data.summary(state),
                lambda: tui_data.tool(state, ("announcements", None)),
            ):
                with self.assertRaises(sqlite3.OperationalError):
                    load()
        reader = Store(state, read_only=True)
        try:
            self.assertEqual(reader.db.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(
                [row[0] for row in reader.db.execute("SELECT name FROM sqlite_master")],
                ["legacy_marker"],
            )
        finally:
            reader.close()


class TuiDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state")
        self.store.update_config(dict(min_free_gb=0, min_available_gb=0, nice=0))
        self.job = self.submit("waiting")
        self.context = dict(measured_at=100, used_cpu_slots=1)
        self.store.db.execute(
            "UPDATE jobs SET wait_context=?,reason='CPU slot budget' WHERE id=?",
            (json.dumps(self.context), self.job),
        )

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def submit(self, key, **kwargs):
        return self.store.submit(
            self.root,
            ["printf", "%s", "snapshot"],
            "local",
            "quick",
            2,
            0.25,
            key,
            "",
            5,
            **kwargs,
        )

    def test_worker_calls_each_open_and_close_an_independent_connection(self):
        opened = []

        class TrackedStore(Store):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.opened_thread = threading.get_ident()
                self.snapshots = []
                opened.append(self)

            @contextlib.contextmanager
            def read_snapshot(self):
                self.timeout = self.db.execute("PRAGMA busy_timeout").fetchone()[0]
                with super().read_snapshot():
                    self.snapshots.append(self.db.in_transaction)
                    yield

            def close(self):
                self.closed_thread = threading.get_ident()
                self.changed = self.db.total_changes
                self.transaction_on_close = self.db.in_transaction
                super().close()

        with mock.patch("agent_builds.tui_data.Store", TrackedStore):
            with ThreadPoolExecutor(max_workers=1) as workers:
                browser = workers.submit(tui_data.browser, self.store.root).result(5)
                details = workers.submit(
                    tui_data.selected, self.store.root, ("details", self.job)
                ).result(5)
                context = workers.submit(
                    tui_data.selected, self.store.root, ("context", self.job)
                ).result(5)
                summary = workers.submit(tui_data.summary, self.store.root).result(5)
                tools = {
                    kind: workers.submit(
                        tui_data.tool, self.store.root, (kind, None)
                    ).result(5)
                    for kind in (
                        "announcements",
                        "workers",
                        "plans",
                        "groups",
                        "watches",
                        "advisor",
                    )
                }
        self.assertTrue(browser["initialized"])
        self.assertEqual(browser["rows"][0]["id"], self.job)
        self.assertEqual(details["id"], self.job)
        self.assertEqual(context, self.context)
        self.assertEqual(summary["waiting_jobs"], 1)
        self.assertEqual(tools["workers"], dict(records=[], workers=[]))
        self.assertEqual(tools["advisor"]["active_sampled"], 1)
        self.assertIsNone(tools["advisor"]["repository"])
        for kind in ("announcements", "plans", "groups", "watches"):
            self.assertEqual(tools[kind], [])
        self.assertEqual(len(opened), 10)
        self.assertEqual(len({id(reader.db) for reader in opened}), 10)
        for reader in opened:
            self.assertTrue(reader.read_only)
            self.assertIsNot(reader.db, self.store.db)
            self.assertNotEqual(reader.opened_thread, threading.get_ident())
            self.assertEqual(reader.opened_thread, reader.closed_thread)
            self.assertEqual(reader.timeout, 1000)
            self.assertEqual(reader.snapshots, [True])
            self.assertEqual(reader.changed, 0)
            self.assertFalse(reader.transaction_on_close)
            with self.assertRaises(sqlite3.ProgrammingError):
                reader.db.execute("SELECT 1")

    def test_browser_rows_and_policy_flags_share_one_snapshot(self):
        original = Store.browser_rows

        def rows(reader):
            self.assertTrue(reader.db.in_transaction)
            result = original(reader)
            self.store.db.execute("UPDATE admission_policy SET requested=1 WHERE id=1")
            self.store.db.execute(
                "UPDATE jobs SET state='failed' WHERE id=?", (self.job,)
            )
            return result

        with mock.patch.object(Store, "browser_rows", rows):
            first = tui_data.browser(self.store.root)
        self.assertEqual(first["rows"][0]["state"], "queued")
        self.assertFalse(first["config"]["protected_quick_requested"])
        fresh = tui_data.browser(self.store.root)
        self.assertEqual(fresh["rows"][0]["state"], "failed")
        self.assertTrue(fresh["config"]["protected_quick_requested"])

    def test_errors_roll_back_and_close_each_owned_reader(self):
        cases = (
            (
                "agent_builds.store.Store.browser_rows",
                lambda: tui_data.browser(self.store.root),
            ),
            (
                "agent_builds.store.Store.get",
                lambda: tui_data.selected(self.store.root, ("details", self.job)),
            ),
            (
                "agent_builds.store.Store.get_wait_context",
                lambda: tui_data.selected(self.store.root, ("context", self.job)),
            ),
            (
                "agent_builds.queue_health.report",
                lambda: tui_data.summary(self.store.root),
            ),
        ) + tuple(
            (target, lambda kind=kind: tui_data.tool(self.store.root, (kind, None)))
            for kind, target in (
                ("announcements", "agent_builds.announce.deliveries"),
                ("workers", "agent_builds.remote.dispatches"),
                ("workers", "agent_builds.profiles.load"),
                ("plans", "agent_builds.plans.listing"),
                ("groups", "agent_builds.groups.listing"),
                ("watches", "agent_builds.external.listing"),
                ("advisor", "agent_builds.advice.report"),
            )
        )
        for target, load in cases:
            opened = []

            def create(*args, **kwargs):
                reader = Store(*args, **kwargs)
                reader.close = mock.Mock(wraps=reader.close)
                opened.append(reader)
                return reader

            with self.subTest(target=target), mock.patch(
                "agent_builds.tui_data.Store", side_effect=create
            ), mock.patch(target, side_effect=ValueError("unreadable snapshot")):
                with self.assertRaisesRegex(ValueError, "unreadable snapshot"):
                    load()
            self.assertEqual(len(opened), 1)
            opened[0].close.assert_called_once_with()
            with self.assertRaises(sqlite3.ProgrammingError):
                opened[0].db.execute("SELECT 1")

    def test_workers_preserve_local_evidence_without_contacting_remote_hosts(self):
        dispatches = self.store.root / "dispatches"
        dispatches.mkdir()
        records = [
            dict(handle="older", worker="dedicated", created=1, state="submitted"),
            dict(handle="newer", worker="dedicated", created=2, state="unknown"),
        ]
        for record in records:
            (dispatches / (record["handle"] + ".json")).write_text(json.dumps(record))
        (self.store.root / "profiles.json").write_text(
            json.dumps(dict(workers=dict(dedicated=dict(host="never-contact.invalid"))))
        )
        with mock.patch(
            "agent_builds.remote.rpc", side_effect=AssertionError("remote connection")
        ):
            result = tui_data.tool(self.store.root, ("workers", None))
        self.assertEqual(
            result, dict(records=list(reversed(records)), workers=["dedicated"])
        )

    def test_invalid_worker_journal_propagates_error_and_closes_reader(self):
        dispatches = self.store.root / "dispatches"
        dispatches.mkdir()
        (dispatches / "broken.json").write_text("{broken")
        opened = []

        def create(*args, **kwargs):
            reader = Store(*args, **kwargs)
            reader.close = mock.Mock(wraps=reader.close)
            opened.append(reader)
            return reader

        with mock.patch("agent_builds.tui_data.Store", side_effect=create):
            with self.assertRaises(ValueError):
                tui_data.tool(self.store.root, ("workers", None))
        self.assertEqual(len(opened), 1)
        opened[0].close.assert_called_once_with()
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].db.execute("SELECT 1")
        self.assertEqual((dispatches / "broken.json").read_text(), "{broken")

    def test_advisor_defaults_to_global_scope_and_honors_explicit_repository(self):
        other = self.root / "other"
        other.mkdir()
        self.store.submit(other, ["true"], "local", "quick", 1, 0.25, "other", "", 5)
        global_report = tui_data.tool(self.store.root, ("advisor", None))
        scoped = tui_data.tool(self.store.root, ("advisor", self.root))
        self.assertEqual(global_report["active_sampled"], 2)
        self.assertIsNone(global_report["repository"])
        self.assertEqual(scoped["active_sampled"], 1)
        self.assertEqual(scoped["repository"], str(self.root))
        self.assertFalse(global_report["changes_applied"])
        self.assertFalse(scoped["changes_applied"])

    def test_global_reservations_include_jobs_beyond_browser_limit_and_filters(self):
        with self.store.transaction():
            for index in range(205):
                job = self.submit("running-" + str(index), scratch_gb=0.5)
                self.store.db.execute(
                    "UPDATE jobs SET state='running',started=created,owner_start='gone' WHERE id=?",
                    (job,),
                )
        with mock.patch.object(
            Store, "recover", side_effect=AssertionError("recovery")
        ):
            browser = tui_data.browser(self.store.root)
            report = tui_data.summary(self.store.root)
            detail = tui_data.selected(self.store.root, ("details", self.job))
        self.assertEqual(len(browser["rows"]), 200)
        self.assertEqual(filter_rows(browser["rows"], repository="/unmatched"), [])
        self.assertEqual(report["waiting_jobs"], 1)
        self.assertEqual(report["reservation_scope"], "global")
        self.assertEqual(
            report["reservations"],
            dict(jobs=205, cpu_slots=410, memory_gb=51.25, scratch_gb=102.5),
        )
        self.assertEqual(len(detail["wait_context"]["contributing_jobs"]), 205)
        self.assertEqual(detail["wait_context"]["current_used_cpu_slots"], 410)
        self.assertEqual(self.store.counts()["running"], 205)

    def test_lazy_tools_stay_read_only_until_explicit_writable_access(self):
        opened = []

        def create(*args, **kwargs):
            store = Store(*args, **kwargs)
            opened.append(store)
            return store

        with mock.patch("agent_builds.tui_data.Store", side_effect=create):
            lazy = tui_data.LazyStore(self.store.root)
            self.assertEqual(opened, [])
            self.assertEqual(lazy.get(self.job)["id"], self.job)
            self.assertEqual(len(opened), 1)
            self.assertTrue(opened[0].read_only)
            with self.assertRaises(sqlite3.OperationalError):
                lazy.db.execute("DELETE FROM jobs")
            writer = lazy.writable()
            self.assertIs(lazy.writable(), writer)
            self.assertEqual(len(opened), 2)
            self.assertFalse(writer.read_only)
            self.assertIsNot(writer.db, opened[0].db)
            writer.update_config(dict(paused=True))
            self.assertTrue(lazy.config()["paused"])
            lazy.close()
            lazy.close()
            self.assertEqual(len(opened), 2)
        for store in opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                store.db.execute("SELECT 1")

    def test_lazy_close_attempts_both_opened_stores_even_if_one_close_fails(self):
        reader, writer = mock.Mock(), mock.Mock()
        reader.close.side_effect = OSError("close failed")
        with mock.patch(
            "agent_builds.tui_data.Store", side_effect=[reader, writer]
        ) as create:
            lazy = tui_data.LazyStore(self.store.root)
            lazy.config()
            lazy.writable()
            with self.assertRaisesRegex(OSError, "close failed"):
                lazy.close()
            lazy.close()
        self.assertEqual(create.call_count, 2)
        reader.close.assert_called_once_with()
        writer.close.assert_called_once_with()

    def test_unknown_selection_kind_does_not_open_a_connection(self):
        with mock.patch("agent_builds.tui_data.Store") as create:
            with self.assertRaisesRegex(ValueError, "Unknown selected-job read"):
                tui_data.selected(self.store.root, ("mutate", self.job))
            with self.assertRaisesRegex(ValueError, "Unknown tool read"):
                tui_data.tool(self.store.root, ("mutate", None))
        create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
