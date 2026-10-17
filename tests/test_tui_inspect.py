"""Pinned, asynchronous operational inspection without queue mutations."""

from concurrent.futures import Future
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from agent_builds.store import Store
from agent_builds.tui_inspect import (
    InspectionFocus,
    inspect_report,
    WhyView,
    HealthView,
)
from agent_builds.tui_reads import SnapshotRead
from agent_builds.tui_text import ReportView


class FocusTests(unittest.TestCase):
    def test_removed_job_is_retained_and_refreshed_without_switching_to_another(self):
        old, other = dict(id="old", state="running"), dict(id="other", state="queued")
        focus = InspectionFocus()
        self.assertIs(focus.get(3, old, [old, other]), old)
        self.assertIs(focus.get(3, other, [other]), old)
        settled = dict(old, state="failed")
        self.assertIs(focus.get(12, other, [settled, other]), settled)
        self.assertIs(focus.get(14, other, [other]), settled)
        self.assertIsNone(focus.get(0, other, [other]))
        self.assertIs(focus.get(3, other, [other]), other)

    def test_explicit_navigation_can_leave_a_retained_job_and_filters_can_reset(self):
        focus = InspectionFocus()
        focus.select(dict(id="gone"))
        rows = [dict(id="first"), dict(id="last")]
        self.assertEqual(focus.move(rows, 1), 0)
        self.assertEqual(focus.row["id"], "first")
        self.assertEqual(focus.move(rows, 1), 1)
        focus.select(dict(id="gone"))
        self.assertEqual(focus.move(rows, -1), 1)
        focus.select(None)
        self.assertIs(focus.get(3, rows[0], rows), rows[0])
        self.assertEqual(focus.move([], 1), 0)
        self.assertIsNone(focus.row)

    def test_shared_read_discards_old_view_payload_and_does_not_overlap(self):
        pending = []

        def submit(load, key):
            future = Future()
            pending.append((key, future))
            return future

        reader = SnapshotRead(
            lambda key: key, submit=submit, label="diagnostics", interval=2
        )
        self.assertIsNone(reader.get(("health", None), 0))
        self.assertIsNone(reader.get(("why", "selected"), 1))
        self.assertEqual(len(pending), 1)
        pending[0][1].set_result({"global": "old view"})
        self.assertIsNone(reader.get(("why", "selected"), 2))
        self.assertEqual(pending[-1][0], ("why", "selected"))
        data = {"selected": "specific job"}
        pending[-1][1].set_result(data)
        self.assertIs(reader.get(("why", "selected"), 3), data)
        self.assertIsNone(reader.get(None, 4))
        self.assertEqual(len(pending), 2)


class TextView(ReportView):
    def lines(self, scope, report):
        return report["lines"]


class ReportPageTests(unittest.TestCase):
    def test_cache_resize_hidden_reload_and_scope_reset(self):
        view = TextView()
        report = {"lines": ["row " + str(i) for i in range(100)]}
        with mock.patch.object(view, "lines", wraps=view.lines) as render:
            view.page("a", report, 40, 5)
            view.scroll(3, pages=True)
            expected = view.page("a", report, 40, 5)
            self.assertEqual(render.call_count, 1)
            self.assertEqual(view.offset, 15)
            self.assertEqual(view.page("a", None, 40, 5), [])
            self.assertEqual(view.offset, 15)
            self.assertEqual(view.page("a", dict(report), 40, 5), expected)
            self.assertEqual(render.call_count, 2)
            view.page("a", view.report, 20, 5)
            self.assertEqual(render.call_count, 2)
            view.page("b", report, 20, 5)
            self.assertEqual(view.offset, 0)

    def test_unavailable_data_shows_full_wrapped_error_without_old_evidence(self):
        view = TextView()
        view.page("a", {"lines": ["secret old evidence"]}, 80, 10)
        lines = view.page("b", None, 19, 10, "Read failed: " + "database busy " * 10)
        self.assertIn("Read failed:", " ".join(lines))
        self.assertNotIn("secret", " ".join(lines))
        self.assertEqual(view.page("c", None, 19, 10), [])
        self.assertEqual(view.position(), "0/0")

    def test_identical_error_for_another_scope_is_still_visible(self):
        view = HealthView()
        first = view.page("first", None, 20, 10, "database unavailable")
        self.assertTrue(first)
        self.assertEqual(
            view.page("second", None, 20, 10, "database unavailable"), first
        )

    def test_bounded_and_sanitized_wrapping(self):
        view = TextView()
        view.page("a", {"lines": ["\x1b[31m" + "界" * 100000]}, 19, 2)
        self.assertLess(sum(map(len, view.logical)), view.limit + 100)
        self.assertNotIn("\x1b", "".join(view.wrapped))
        self.assertIn("Report clipped", " ".join(view.wrapped))
        view.end()
        self.assertIn("CLI report", " ".join(view.page("a", view.report, 80, 3)))


class DiagnosisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state")
        self.store.update_config(
            dict(
                min_free_gb=4,
                min_available_gb=2,
                cpu_slots=2,
                max_running=2,
                quick_reserve_slots=0,
                quick_reserve_jobs=0,
                quick_reserve_memory_gb=0,
            )
        )
        self.running = self.store.submit(
            self.root, ["echo", "blocker"], "local", "quick", 2, 1, "shared", "", 30
        )
        self.waiting = self.store.submit(
            self.root, ["echo", "waiting"], "local", "quick", 2, 1, "shared", "", 30
        )
        self.store.db.execute(
            "UPDATE jobs SET state='running' WHERE id=?", (self.running,)
        )
        self.store.db.execute(
            "UPDATE jobs SET reason='repository already building',wait_context=? WHERE id=?",
            (
                '{"measured_at":0,"blocking_job":{"id":"' + self.running + '"}}',
                self.waiting,
            ),
        )

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def diagnosis(self, job):
        with mock.patch(
            "agent_builds.diagnostics.snapshot", return_value=dict(available_gb=8)
        ), mock.patch(
            "agent_builds.diagnostics.shutil.disk_usage",
            return_value=SimpleNamespace(free=1024**3),
        ):
            return inspect_report(self.store.root, ("why", job))

    def test_selected_report_includes_global_capacity_stale_reason_disk_and_blocker(
        self,
    ):
        result = self.diagnosis(self.waiting)
        self.assertEqual([row["id"] for row in result["jobs"]], [self.waiting])
        self.assertEqual(result["global_reservations"]["cpu_slots"], 2)
        view = WhyView()
        view.page(self.waiting, result, 120, 1000)
        text = "\n".join(view.logical)
        self.assertIn("Runner reason: repository already building", text)
        self.assertIn("[STALE]", text)
        self.assertIn("global CPU slots: 2 used / 2 limit | 2 requested", text)
        self.assertIn("1.000 GiB free / 4.000 required | deficit 3.000 GiB", text)
        self.assertIn(self.running + " running", text)
        self.assertNotIn(self.running + " running [STALE", text)
        self.assertIn("8.00 GiB available / 3.00 required | deficit 0.00 GiB", text)
        self.assertIn("not an ETA", text)
        self.assertFalse(result["changes_applied"])

    def test_read_only_loader_does_not_initialize_missing_state_or_mutate_existing_rows(
        self,
    ):
        absent = self.root / "missing"
        with self.assertRaises(ValueError):
            inspect_report(absent, ("why", "unknown"))
        self.assertFalse(absent.exists())
        before = list(
            self.store.db.execute(
                "SELECT id,state,slots,memory_gb,cancel_requested FROM jobs ORDER BY id"
            )
        )
        config = self.store.config_path.read_bytes()
        self.diagnosis(self.waiting)
        after = list(
            self.store.db.execute(
                "SELECT id,state,slots,memory_gb,cancel_requested FROM jobs ORDER BY id"
            )
        )
        self.assertEqual([tuple(row) for row in before], [tuple(row) for row in after])
        self.assertEqual(config, self.store.config_path.read_bytes())

    def test_workbench_uses_its_own_read_only_connection_and_closes_it(self):
        readers = []

        def report(reader, handle):
            readers.append(reader)
            self.assertTrue(reader.read_only)
            self.assertEqual(reader.db.execute("PRAGMA query_only").fetchone()[0], 1)
            self.assertEqual(handle, self.waiting)
            return {"anchor": handle}

        with mock.patch("agent_builds.workbench.report", side_effect=report):
            self.assertEqual(
                inspect_report(self.store.root, ("workbench", self.waiting)),
                {"anchor": self.waiting},
            )
        with self.assertRaises(sqlite3.ProgrammingError):
            readers[0].db.execute("SELECT 1")

    def test_terminal_unknown_exit_is_not_claimed_to_be_a_native_success(self):
        self.store.db.execute(
            "UPDATE jobs SET state='lost',exit_code=NULL WHERE id=?", (self.waiting,)
        )
        report = self.diagnosis(self.waiting)
        view = WhyView()
        view.page(self.waiting, report, 80, 100)
        text = "\n".join(view.logical)
        self.assertIn("This job is not queued", text)
        self.assertIn("Recorded job exit: not recorded", text)
        self.assertNotIn("Reservation snapshot", text)
