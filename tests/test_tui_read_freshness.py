"""Snapshot freshness is presentation of accepted reads, never another read."""

from concurrent.futures import Future
import unittest

from agent_builds.tui_reads import SnapshotRead


class SnapshotFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def submit(loader, key):
            future = Future()
            self.calls.append((key, future))
            return future

        self.read = SnapshotRead(
            lambda key: key, interval=1, submit=submit, label="plans"
        )

    def accept(self, report):
        self.read.get("plans", 0)
        self.calls[-1][1].set_result(report)
        return self.read.get("plans", 1)

    def test_age_uses_last_accepted_read_and_stale_threshold_is_strict(self):
        report = {"state": "running"}
        self.assertIs(self.accept(report), report)
        self.read.get("plans", 2)
        self.assertEqual(self.read.status(2), "Snapshot 1s ago; refreshing")
        self.assertEqual(self.read.status(3), "Snapshot 2s ago; refreshing")
        self.assertEqual(self.read.status(3.01), "STALE Snapshot 2s ago; refreshing")
        self.assertEqual(self.read.status(6), "STALE Snapshot 5s ago; refreshing")
        self.assertIs(self.read.report, report)
        self.assertEqual(self.read.observed, 1)
        self.assertFalse(self.calls[-1][1].done())
        self.assertEqual(len(self.calls), 2)

    def test_short_headers_keep_age_stale_and_pending_within_minimum_width(self):
        self.accept([])
        self.read.get("plans", 2)
        for age, expected in (
            (1, "EXTERNAL 1s~"),
            (2, "EXTERNAL 2s~"),
            (5, "STALE EXTERNAL 5s~"),
            (59, "STALE EXTERNAL 59s~"),
            (60, "STALE EXTERNAL 1m~"),
            (3599, "STALE EXTERNAL 59m~"),
            (3600, "STALE EXTERNAL 1h~"),
            (86399, "STALE EXTERNAL 23h~"),
            (86400, "STALE EXTERNAL 1d~"),
            (864000, "STALE EXTERNAL >9d~"),
        ):
            with self.subTest(age=age):
                heading = self.read.status(age + 1, short_label="EXTERNAL")
                self.assertEqual(heading, expected)
                self.assertLessEqual(len(heading), 19)
        self.assertEqual(len(self.calls), 2)

    def test_initial_loading_and_errors_preserve_full_wording(self):
        self.read.get("plans", 0)
        self.assertEqual(
            self.read.status(0), "Loading plans; navigation stays available"
        )
        self.assertEqual(self.read.status(0, short_label="PLANS"), "PLANS loading")
        self.calls[-1][1].set_exception(OSError("fixture unavailable"))
        self.read.get("plans", 1)
        self.assertEqual(self.read.status(1), "Plans unavailable: fixture unavailable")
        self.assertEqual(self.read.status(1, short_label="PLANS"), "PLANS error")
        self.assertIsNone(self.read.observed)

    def test_cached_failure_keeps_original_error_wording_until_fresh_acceptance(self):
        self.accept({})
        self.read.get("plans", 2)
        self.calls[-1][1].set_exception(OSError("fixture busy"))
        self.read.get("plans", 3)
        self.assertEqual(self.read.status(3), "Stale plans: fixture busy")
        self.assertEqual(self.read.status(3, short_label="PLANS"), "STALE PLANS 2s")
        self.assertEqual(self.read.observed, 1)
        self.read.get("plans", 4)
        self.calls[-1][1].set_result({"fresh": True})
        self.read.get("plans", 5)
        self.assertEqual(self.read.status(5), "Snapshot 0s ago")
        self.assertEqual(self.read.status(5, short_label="PLANS"), "PLANS 0s")
        self.assertIsNone(self.read.error)

    def test_status_calls_do_not_refresh_mutate_or_reset_observation_age(self):
        report = {"state": "succeeded"}
        self.accept(report)
        before = (
            self.read.report,
            self.read.observed,
            self.read.next_read,
            self.read.future,
        )
        for now in (1, 2, 20, 2000):
            self.read.status(now)
            self.read.status(now, short_label="PLANS")
        self.assertEqual(
            (
                self.read.report,
                self.read.observed,
                self.read.next_read,
                self.read.future,
            ),
            before,
        )
        self.assertIs(self.read.report, report)
        self.assertEqual(len(self.calls), 1)

    def test_empty_successful_reports_age_and_hidden_keys_clear_old_status(self):
        for report in ([], {}):
            with self.subTest(report=report):
                self.read.get(None, 0)
                self.accept(report)
                self.assertEqual(self.read.status(6), "STALE Snapshot 5s ago")
                self.assertEqual(
                    self.read.status(6, short_label="PLANS"), "STALE PLANS 5s"
                )
                self.read.get(None, 7)
                self.assertIsNone(self.read.observed)
                self.assertEqual(
                    self.read.status(7, short_label="PLANS"), "PLANS loading"
                )


if __name__ == "__main__":
    unittest.main()
