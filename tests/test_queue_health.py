import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_builds import mcp, queue_health
from agent_builds.__main__ import main
from agent_builds.store import Store
from agent_builds.tui import compact_lines
from agent_builds import tui


class QueueHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state")
        self.store.update_config(
            dict(
                cpu_slots=6,
                memory_gb=16,
                max_running=4,
                min_free_gb=10,
                min_available_gb=0,
                paused=False,
            )
        )

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def job(self, key="repo", scratch=0, floor=0, locks=None):
        return self.store.submit(
            self.root,
            ["true"],
            "local",
            "quick",
            1,
            1,
            key,
            "",
            5,
            scratch_gb=scratch,
            min_free_gb=floor,
            locks=locks,
        )

    def free(self, value):
        return mock.patch(
            "agent_builds.queue_health.shutil.disk_usage",
            return_value=type("Usage", (), dict(free=int(value * 1024**3)))(),
        )

    def test_all_waiters_share_disk_blockage_including_held_multi_lock_jobs(self):
        for index in range(18):
            self.job(
                str(index),
                floor=14 if index == 17 else 0,
                locks=["shared-output"] if index % 2 else None,
            )
        with self.free(6.7):
            result = queue_health.report(self.store)
        self.assertEqual((result["queued_jobs"], result["waiting_jobs"]), (18, 18))
        self.assertEqual(result["status"], "disk_blocked")
        self.assertEqual(result["disk"]["limited_jobs"], 18)
        self.assertAlmostEqual(result["disk"]["shortfall_min_gb"], 3.3)
        self.assertAlmostEqual(result["disk"]["shortfall_max_gb"], 7.3)
        self.assertIn("All 18", result["summary"])
        self.assertIn("3.3", queue_health.banner(result, compact=True))
        self.assertIn("7.3", "\n".join(queue_health.lines(result)))

    def test_filtered_requirements_include_cancelling_global_scratch(self):
        busy = self.job("other", scratch=2)
        self.store.db.execute("UPDATE jobs SET state='cancelling' WHERE id=?", (busy,))
        selected = self.root / "selected"
        self.job(str(selected), scratch=1)
        self.job("unrelated", floor=30)
        with self.free(12):
            result = queue_health.report(self.store, repo=str(selected))
        self.assertEqual(result["waiting_jobs"], 1)
        self.assertEqual(result["reservations"]["jobs"], 1)
        self.assertEqual(result["reservation_scope"], "global")
        self.assertEqual(result["disk"]["required_min_gb"], 13)
        self.assertEqual(result["disk"]["shortfall_max_gb"], 1)
        self.assertIn("running count is global", result["summary"])

    def test_cancellation_is_excluded_from_admission_requirements_and_pause_is_explicit(
        self,
    ):
        cancelled = self.job("cancel", floor=10)
        self.store.cancel(cancelled)
        self.job("waiting", floor=14)
        with self.free(12):
            result = queue_health.report(self.store)
            self.assertEqual(result["waiting_jobs"], 1)
            self.assertEqual(result["queued_cancellations"], 1)
            self.assertEqual(result["disk"]["required_min_gb"], 14)
            self.assertEqual(result["status"], "disk_blocked")
            self.store.update_config(dict(paused=True))
            paused = queue_health.report(self.store)
        self.assertEqual(paused["status"], "paused")
        self.assertIn("running jobs continue", paused["summary"])
        self.assertIn("1 awaiting cancellation", paused["summary"])

    def test_partial_disk_pressure_and_missing_filesystem_never_claim_all_jobs_blocked(
        self,
    ):
        self.job("small")
        self.job("large", floor=14)
        with self.free(12):
            result = queue_health.report(self.store)
        self.assertEqual(result["disk"]["limited_jobs"], 1)
        self.assertEqual(result["status"], "waiting")
        with mock.patch(
            "agent_builds.queue_health.shutil.disk_usage", side_effect=FileNotFoundError
        ):
            unknown = queue_health.report(self.store)
        self.assertIsNone(unknown["disk"]["available_gb"])
        self.assertIsNone(unknown["disk"]["shortfall_min_gb"])
        self.assertIsNone(unknown["disk"]["limited_jobs"])
        self.assertIn(
            "Shared state disk: unknown", "\n".join(queue_health.lines(unknown))
        )
        self.assertEqual(unknown["disk"]["error"], "FileNotFoundError")
        self.assertNotEqual(unknown["status"], "disk_blocked")

    def test_fresh_stale_future_missing_and_invalid_observations_are_separate(self):
        for index, value in enumerate(
            (
                dict(measured_at=100),
                dict(measured_at=90),
                dict(measured_at=101),
                {},
                "{bad",
                "x" * 100000,
            )
        ):
            job = self.job(str(index))
            encoded = json.dumps(value) if isinstance(value, dict) else value
            self.store.db.execute(
                "UPDATE jobs SET reason='CPU slot budget',wait_context=? WHERE id=?",
                (encoded, job),
            )
        before = self.store.db.total_changes
        with self.free(20), mock.patch.object(
            self.store, "describe", side_effect=AssertionError("expanded diagnostics")
        ), mock.patch.object(
            self.store, "rows", side_effect=AssertionError("expanded jobs")
        ):
            result = queue_health.report(self.store, now=100)
        observed = result["observations"]
        self.assertEqual(
            observed["fresh"],
            [dict(category="cpu_capacity", jobs=1, label="CPU capacity")],
        )
        self.assertEqual(
            (
                observed["stale_jobs"],
                observed["missing_jobs"],
                observed["unreadable_jobs"],
            ),
            (2, 1, 2),
        )
        self.assertEqual(self.store.db.total_changes, before)
        self.assertFalse(result["changes_applied"])

    def test_sample_limit_never_limits_counts_disk_ranges_or_loads_large_job_evidence(
        self,
    ):
        for index in range(3):
            self.job(str(index), floor=14 if index == 2 else 0)
        self.store.db.execute(
            "UPDATE jobs SET source=?,command=?",
            (json.dumps(dict(secret="x" * 100000)), json.dumps(["x" * 100000])),
        )
        with self.free(6.7), mock.patch.object(queue_health, "MAX_WAIT_SAMPLE", 2):
            result = queue_health.report(self.store)
        self.assertEqual(result["waiting_jobs"], 3)
        self.assertEqual(result["observations"]["sampled_jobs"], 2)
        self.assertTrue(result["observations"]["truncated"])
        self.assertEqual(result["disk"]["required_max_gb"], 14)
        self.assertEqual(result["disk"]["limited_jobs"], 3)
        self.assertLess(len(json.dumps(result)), 8000)
        self.assertNotIn("secret", json.dumps(result))

    def test_deeply_nested_observation_does_not_break_queue_inspection(self):
        job = self.job()
        self.store.db.execute(
            "UPDATE jobs SET wait_context=? WHERE id=?",
            ("[" * 1100 + "0" + "]" * 1100, job),
        )
        with self.free(20):
            result = queue_health.report(self.store)
        self.assertEqual(result["observations"]["unreadable_jobs"], 1)
        self.assertEqual(result["waiting_jobs"], 1)

    def test_invalid_numeric_timestamps_are_unreadable_not_report_errors(self):
        for index, stamp in enumerate(
            (10**500, float("inf"), float("nan"), True, "yesterday")
        ):
            job = self.job(str(index))
            self.store.db.execute(
                "UPDATE jobs SET wait_context=? WHERE id=?",
                (json.dumps(dict(measured_at=stamp)), job),
            )
        with self.free(20):
            result = queue_health.report(self.store)
        self.assertEqual(result["observations"]["unreadable_jobs"], 5)
        self.assertEqual(result["observations"]["fresh"], [])

    def test_missing_queue_cli_and_mcp_are_read_only_and_nested_snapshot_is_preserved(
        self,
    ):
        missing = self.root / "missing"
        with mock.patch.dict(os.environ, AGENT_BUILDS_STATE=str(missing)):
            with mock.patch(
                "sys.argv", ["buildr", "status", "--summary", "--json"]
            ), mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(main(), 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result["queue_health"]["status"], "uninitialized")
            self.assertFalse(
                mcp.call("buildr_status", {}, read_only=True)["queue_health"][
                    "initialized"
                ]
            )
        self.assertFalse(missing.exists())
        with self.store.read_snapshot():
            queue_health.report(self.store)
            self.assertTrue(self.store.db.in_transaction)
        self.assertFalse(self.store.db.in_transaction)

    def test_compact_dashboard_and_small_shortfall_keep_the_action_visible(self):
        self.job(floor=10.0001)
        with self.free(10):
            result = queue_health.report(self.store)
        self.assertIn("+0.1 GiB", queue_health.banner(result, compact=True))
        for height, width in ((8, 20), (14, 40)):
            lines = compact_lines(
                [], 0, [], 0, 0, self.store.config(), height, width, queue_health=result
            )
            self.assertLessEqual(len(lines), height)
            self.assertTrue(any("DISK:" in line and "0.1" in line for line in lines))
            self.assertIn("q", lines[-1])

    def test_cli_and_mcp_show_all_waiters_despite_one_displayed_job(self):
        for index in range(3):
            self.job(str(index))
        with self.free(6.7), mock.patch.dict(
            os.environ, AGENT_BUILDS_STATE=str(self.store.root)
        ):
            with mock.patch(
                "sys.argv", ["buildr", "status", "--limit", "1", "--json"]
            ), mock.patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(main(), 0)
            cli = json.loads(output.getvalue())
            protocol = mcp.call("buildr_status", dict(limit=1), read_only=True)
        for result in (cli, protocol):
            self.assertEqual(len(result["jobs"]), 1)
            self.assertEqual(result["queue_health"]["waiting_jobs"], 3)
            self.assertEqual(result["queue_health"]["status"], "disk_blocked")
            self.assertFalse(result["queue_health"]["changes_applied"])

    def test_full_dashboard_renders_shortfall_and_uses_opportunistic_recovery(self):
        job = self.job()
        screen = mock.Mock()
        screen.getmaxyx.return_value = (20, 80)
        screen.getch.return_value = ord("q")
        machine = dict(
            cpus=8,
            load=0,
            available_gb=16,
            total_gb=32,
            free_gb=6.7,
            cpu_pressure=0,
            memory_pressure=0,
            io_pressure=0,
        )
        with self.free(6.7), mock.patch.dict(
            os.environ, AGENT_BUILDS_STATE=str(self.store.root)
        ), mock.patch("agent_builds.tui.curses.curs_set"), mock.patch(
            "agent_builds.tui.curses.has_colors", return_value=False
        ), mock.patch("agent_builds.tui.curses.color_pair", return_value=0), mock.patch(
            "agent_builds.tui.snapshot", return_value=machine
        ), mock.patch("agent_builds.tui.ProcessSampler") as sampler, mock.patch.object(
            Store, "recover", side_effect=AssertionError("blocking recovery")
        ), mock.patch.object(Store, "recover_if_due", return_value=False) as recovery:
            sampler.return_value.sample.return_value = []
            tui.main(screen, self.root)
        recovery.assert_called_once()
        header = [
            call.args[2] for call in screen.addnstr.call_args_list if call.args[0] == 4
        ]
        self.assertEqual(len(header), 1)
        self.assertIn("+3.3 GiB", header[0])
        self.assertIn("all 1 waiting", header[0])
        self.assertEqual(self.store.get_control(job)["state"], "queued")


if __name__ == "__main__":
    unittest.main()
