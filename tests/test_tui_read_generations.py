"""A reopened view cannot adopt data or errors from its previous visit."""

from concurrent.futures import Future
import unittest

from agent_builds.tui_reads import SnapshotRead


class ControlledReads:
    def __init__(self):
        self.calls = []

    def __call__(self, loader, key):
        if any(not future.done() for unused_loader, unused_key, future in self.calls):
            raise AssertionError("A second inspection overlapped the pending worker")
        future = Future()
        self.calls.append((loader, key, future))
        return future


class SnapshotGenerationTests(unittest.TestCase):
    def setUp(self):
        self.submit = ControlledReads()
        self.loader = lambda key: key
        self.read = SnapshotRead(
            self.loader, interval=3, submit=self.submit, label="job status"
        )

    def round_trip(self, intermediate, failure):
        self.assertIsNone(self.read.get("A", 0))
        old = self.read.future
        self.assertIsNone(self.read.get(intermediate, 1))
        self.assertIsNone(self.read.get("A", 2))
        self.assertIs(self.read.future, old)
        self.assertFalse(old.cancelled())
        self.assertEqual(len(self.submit.calls), 1)
        if failure:
            old.set_exception(OSError("obsolete A error"))
        else:
            old.set_result("obsolete A result")

        self.assertIsNone(self.read.get("A", 3))
        self.assertIsNone(self.read.error)
        self.assertIsNone(self.read.observed)
        self.assertEqual(
            [key for unused_loader, key, unused_future in self.submit.calls], ["A", "A"]
        )
        fresh = self.read.future
        self.assertIsNot(fresh, old)
        self.assertFalse(fresh.done())
        self.assertIn("Loading job status", self.read.status(3))
        fresh.set_result("current A result")
        self.assertEqual(self.read.get("A", 4), "current A result")
        self.assertEqual(self.read.observed, 4)
        self.assertIsNone(self.read.error)
        self.assertIsNone(self.read.future)
        self.assertEqual(len(self.submit.calls), 2)

    def test_a_b_a_discards_obsolete_data_and_submits_one_fresh_read(self):
        self.round_trip("B", failure=False)

    def test_a_b_a_discards_obsolete_error_and_submits_one_fresh_read(self):
        self.round_trip("B", failure=True)

    def test_hide_and_reopen_discards_obsolete_data(self):
        self.round_trip(None, failure=False)

    def test_hide_and_reopen_discards_obsolete_error(self):
        self.round_trip(None, failure=True)

    def test_same_key_refresh_retains_cache_and_adopts_fresh_results_and_errors(self):
        self.read.get("A", 0)
        self.read.future.set_result("first")
        self.assertEqual(self.read.get("A", 1), "first")
        self.assertEqual(self.read.observed, 1)
        self.assertEqual(self.read.get("A", 3), "first")
        self.assertEqual(len(self.submit.calls), 1)
        self.assertEqual(self.read.get("A", 4), "first")
        self.assertEqual(len(self.submit.calls), 2)
        self.assertEqual(self.read.observed, 1)
        self.assertIn("refreshing", self.read.status(4))
        self.read.future.set_exception(OSError("current read failed"))
        self.assertEqual(self.read.get("A", 5), "first")
        self.assertEqual(self.read.error, "current read failed")
        self.assertEqual(self.read.observed, 1)
        self.assertIn("Stale job status", self.read.status(5))
        self.assertEqual(self.read.get("A", 8), "first")
        self.read.future.set_result("fresh")
        self.assertEqual(self.read.get("A", 9), "fresh")
        self.assertEqual(self.read.observed, 9)
        self.assertIsNone(self.read.error)
        self.assertEqual(len(self.submit.calls), 3)

    def test_key_changes_clear_report_error_and_observed_and_hidden_never_submits(self):
        self.read.get("A", 0)
        self.read.future.set_result("A cache")
        self.read.get("A", 1)
        self.read.get("A", 4)
        self.read.future.set_exception(OSError("A error"))
        self.read.get("A", 5)
        self.assertEqual(self.read.report, "A cache")
        self.assertEqual(self.read.error, "A error")
        self.assertEqual(self.read.observed, 1)

        self.assertIsNone(self.read.get("B", 6))
        self.assertIsNone(self.read.error)
        self.assertIsNone(self.read.observed)
        pending = self.read.future
        self.assertIsNone(self.read.get(None, 7))
        self.assertIs(self.read.future, pending)
        pending.set_result("hidden B data")
        self.assertIsNone(self.read.get(None, 8))
        self.assertIsNone(self.read.report)
        self.assertIsNone(self.read.error)
        self.assertIsNone(self.read.observed)
        self.assertIsNone(self.read.future)
        self.assertEqual(
            [key for unused_loader, key, unused_future in self.submit.calls],
            ["A", "A", "B"],
        )
        self.read.get(None, 100)
        self.assertEqual(len(self.submit.calls), 3)


if __name__ == "__main__":
    unittest.main()
