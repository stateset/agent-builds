"""Receipt navigation opens only explicitly selected, confirmed job identities."""

from concurrent.futures import Future
import curses
from pathlib import Path
import unittest
from unittest import mock

import test_tui_submission_flow as flows
import test_tui_link_navigation as links
from agent_builds.store import Store
from agent_builds.tui_data import LazyStore
from agent_builds.tui_submission_view import SubmissionView


def status(key, phase="submitted", kind="job", handle="abcdef123456"):
    return flows.Status(
        key,
        "command " + key,
        phase,
        kind,
        handle,
        "detail " + key,
        Path("/private/receipt-" + key),
        Path("/private/transcript-" + key),
    )


class ReceiptSelectionTests(unittest.TestCase):
    def test_overview_and_receipts_cycle_in_both_directions(self):
        report = (status("old"), status("new"))
        view = SubmissionView()
        self.assertIsNone(view.log_target(report))
        self.assertIn("Tab", view.action_note(report))
        for expected in ("new", "old", None, "new"):
            view.choose(report, 1)
            self.assertEqual(view.selected_key, expected)
        view.choose(report, -1)
        self.assertIsNone(view.selected_key)
        view.choose(report, -1)
        self.assertEqual(view.selected_key, "old")
        self.assertEqual(view.selection_label(report), "2/2")
        view.choose((), 1)
        self.assertIsNone(view.current(()))

    def test_selection_survives_new_arrivals_and_phase_updates_without_retargeting(
        self,
    ):
        first = status("first", "starting", None, None)
        view = SubmissionView()
        view.choose((first,), 1)
        report = (first, status("later"))
        self.assertEqual(view.current(report), first)
        self.assertEqual(view.selection_label(report), "2/2")
        self.assertIsNone(view.log_target(report))
        ready = first._replace(phase="submitted", kind="job", handle="111111111111")
        report = (ready, report[1])
        self.assertEqual(view.log_target(report), ready.handle)
        self.assertIn(ready.handle, "\n".join(view.page(None, report, 100, 20)))
        self.assertIsNone(view.log_target((report[1],)))
        self.assertIsNone(view.selected_key)

    def test_only_complete_confirmed_job_identities_offer_logs(self):
        for entry in (
            status("a", "starting", None, None),
            status("a", "unconfirmed", "job", "abcdef123456"),
            status("a", kind="plan"),
            status("a", kind=None),
            status("a", handle=None),
            status("a", handle="short"),
            status("a", handle="abcdef123456; command"),
        ):
            view = SubmissionView()
            with self.subTest(entry=entry):
                view.choose((entry,), 1)
                self.assertIsNone(view.log_target((entry,)))
                self.assertNotIn("Enter ", view.action_note((entry,)))
                view.page(None, (entry,), 100, 20)

    def test_selected_pages_cache_wrapping_and_leave_overview_available(self):
        report = (status("old"), status("new"))
        view = SubmissionView()
        with mock.patch.object(
            view, "lines", wraps=view.lines
        ) as formatter, mock.patch.object(
            Path, "stat", side_effect=AssertionError("presentation IO")
        ):
            overview = view.page(None, report, 100, 100)
            self.assertIn("detail old", "\n".join(overview))
            self.assertIn("detail new", "\n".join(overview))
            view.choose(report, 1)
            selected = view.page(None, report, 19, 5)
            calls = formatter.call_count
            wrapped = view.wrapped
            for unused in range(300):
                self.assertEqual(view.page(None, report, 19, 5), selected)
                self.assertEqual(view.log_target(report), "abcdef123456")
            self.assertEqual(formatter.call_count, calls)
            self.assertIs(view.wrapped, wrapped)
            view.end()
            view.page(None, report, 19, 5)
            view.choose(report, 1)
            self.assertEqual(view.offset, 0)
            view.choose(report, 1)
            self.assertEqual(view.page(None, report, 100, 100), overview)


class ReceiptNavigationTests(unittest.TestCase):
    def setUp(self):
        self.flow = flows.TuiSubmissionFlowTests()
        self.flow.setUp()
        self.addCleanup(self.flow.doCleanups)
        self.store = self.flow.store
        self.target = self.flow.fixture.jobs[2]
        self.store.log_path(self.target).write_text("RECEIPT LOG TARGET\n")

    def seed(self, entries):
        self.flow.tracker.current = {entry.key: entry for entry in entries}
        self.flow.tracker.updates = list(entries)

    def dashboard(self, keys, size=(24, 120), **kwargs):
        before = list(self.store.db.iterdump()), self.store.config_path.read_bytes()
        with mock.patch.object(
            LazyStore, "writable", side_effect=AssertionError("write")
        ), mock.patch.object(
            Store, "_ensure_schema", side_effect=AssertionError("migration")
        ), mock.patch.object(Store, "recover", side_effect=AssertionError("recovery")):
            frames = self.flow.dashboard(keys, dimensions=size, **kwargs)
        self.assertEqual(
            (list(self.store.db.iterdump()), self.store.config_path.read_bytes()),
            before,
        )
        return frames

    def test_select_then_enter_opens_exact_log_and_keeps_browser_filters(self):
        for size in ((24, 120), (8, 20)):
            self.seed((status("receipt", handle=self.target),))
            calls = []

            def submit(loader, key):
                calls.append(key)
                return links.completed(loader, key)

            with self.subTest(size=size):
                frames = self.dashboard(
                    [ord("/"), ord("U"), 10, 9, 10, -1, ord("l"), ord("U"), ord("q")],
                    size,
                    answers=["unmatched-browser"],
                    link_submit=submit,
                )
                self.assertFalse(any("LOG LIVE" in f.get(0, "") for f in frames[:4]))
                self.assertTrue(
                    any("RECEIPT LOG TARGET" in links.text(f) for f in frames)
                )
                self.assertTrue(
                    any("No matching jobs" in links.text(f) for f in frames)
                )
                self.assertIn("SUBMISSIONS 1/1", frames[-1][0])
                self.assertEqual([key[1:] for key in calls], [("browser", self.target)])

    def test_plan_starting_and_unconfirmed_entries_never_lookup_or_retry(self):
        for entry in (
            status("entry", "starting", None, None),
            status("entry", "unconfirmed", "job", self.target),
            status("entry", kind="plan", handle=self.target),
            status("entry", handle=None),
        ):
            self.seed((entry,))
            lookup = mock.Mock(side_effect=AssertionError("unexpected lookup"))
            with self.subTest(entry=entry):
                frames = self.dashboard(
                    [
                        ord("U"),
                        9,
                        10,
                        -1,
                        ord("n"),
                        ord("c"),
                        ord("p"),
                        ord("t"),
                        ord("q"),
                    ],
                    link_submit=lookup,
                )
                lookup.assert_not_called()
                self.assertIn("SUBMISSIONS 1/1", frames[-1][0])
                self.assertEqual(self.flow.tracker.starts, [])

    def test_older_receipt_opens_while_newest_unknown_attempt_remains_untouched(self):
        self.seed(
            (
                status("old", handle=self.target),
                status("new", "unconfirmed", None, None),
            )
        )
        frames = self.dashboard([ord("U"), 9, 10, 9, 10, -1, ord("q")])
        self.assertIn("SUBMISSIONS 1/2", frames[2][0])
        self.assertIn("SUBMISSIONS 2/2", frames[4][0])
        self.assertIn(self.target, frames[-1][1])
        self.assertEqual(self.flow.tracker.current["new"].phase, "unconfirmed")

    def test_passive_new_receipt_does_not_abandon_selected_pending_lookup(self):
        self.seed((status("selected", handle=self.target),))
        pending = links.PendingLinks()
        ticks = [0]
        keys = iter([ord("U"), 9, 10, -1, -1, ord("q")])

        def key():
            value = next(keys)
            if value == -1:
                ticks[0] += 1
                if ticks[0] == 1:
                    self.flow.tracker.deliver(
                        "selected",
                        "submitted",
                        "job",
                        self.target,
                        "Fresh confirmation",
                    )
                    newer = status("new", "unconfirmed", None, None)
                    self.flow.tracker.current["new"] = newer
                    self.flow.tracker.updates.append(newer)
                else:
                    pending.finish()
            return value

        frames = self.dashboard(key, link_submit=pending)
        self.assertIn("LOG LIVE", frames[-1][0])
        self.assertIn(self.target, frames[-1][1])
        self.assertEqual(len(pending.calls), 1)

    def test_help_defers_handoff_and_selection_navigation_discards_late_reply(self):
        for abandon in (False, True):
            self.seed(
                (status("old", handle=self.target), status("new", handle=self.target))
            )
            pending = links.PendingLinks()
            keys = iter(
                [ord("U"), 9, 10, 9, -1, ord("q")]
                if abandon
                else [ord("U"), 9, 10, ord("?"), -1, ord("q"), -1, ord("q")]
            )
            completed = [False]

            def key():
                value = next(keys)
                if value == -1 and not completed[0]:
                    pending.finish()
                    completed[0] = True
                return value

            with self.subTest(abandon=abandon):
                frames = self.dashboard(key, link_submit=pending)
                if abandon:
                    self.assertFalse(any("LOG LIVE" in f.get(0, "") for f in frames))
                    self.assertIn("SUBMISSIONS 2/2", frames[-1][0])
                else:
                    self.assertIn("BUILDR HELP", frames[4][0])
                    self.assertIn("LOG LIVE", frames[-1][0])

    def test_receipt_logs_work_while_initial_browser_read_is_stalled(self):
        for size in ((24, 120), (8, 20)):
            self.seed((status("receipt", handle=self.target),))
            browser = Future()
            with self.subTest(size=size):
                frames = self.dashboard(
                    [ord("U"), 9, 10, -1, ord("q")],
                    size,
                    background_submit=lambda loader, key: (
                        browser if key == "queue" else links.completed(loader, key)
                    ),
                )
                self.assertIn("LOG LIVE", frames[-1][0])
                self.assertIn(self.target, frames[-1][1])
                self.assertFalse(browser.done())


if __name__ == "__main__":
    unittest.main()
