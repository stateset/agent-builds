import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
import zipfile

from agent_builds import engine_study, experiments, verification


def record(engine, pair=0):
    return dict(
        engine=engine,
        pair=pair,
        jobs=2,
        verified_jobs=2,
        results=[
            dict(id="agent-" + str(i), verified=True, exit_code=0) for i in range(2)
        ],
        wall_seconds=2 if engine == "baseline" else 1,
        setup_seconds=0.1,
        end_to_end_seconds=2.1,
        child_cpu_seconds=1,
        controller_cpu_seconds=0.2,
        sampled_process_tree_peak_rss_gb=0.1,
        verified_completions_per_second=1 if engine == "baseline" else 2,
    )


class EngineEvidenceTests(unittest.TestCase):
    def test_ratios_require_complete_unique_verified_jobs_and_unchanged_source(self):
        rows = [record("baseline"), record("candidate")]
        valid, comparisons, summary = engine_study.summarize(rows, 1, 2, True)
        self.assertTrue(valid)
        self.assertEqual(comparisons[0]["candidate_to_baseline_wall_ratio"], 0.5)
        self.assertEqual(comparisons[0]["verified_throughput_ratio"], 2)
        self.assertEqual(set(summary), {"baseline", "candidate"})
        for unchanged in (False, None):
            self.assertEqual(
                engine_study.summarize(rows, 1, 2, unchanged), (False, [], None)
            )
        self.assertEqual(
            engine_study.summarize(rows[:1], 1, 2, True), (False, [], None)
        )
        rows[1]["results"][1]["id"] = "agent-0"
        self.assertEqual(engine_study.summarize(rows, 1, 2, True), (False, [], None))

    def test_failed_or_unknown_native_exit_withholds_all_performance_ratios(self):
        for code in (None, 7, False):
            rows = [record("baseline"), record("candidate")]
            rows[1]["results"][0]["exit_code"] = code
            self.assertEqual(
                engine_study.summarize(rows, 1, 2, True), (False, [], None)
            )
        rows = [record("baseline"), record("candidate")]
        rows[1]["verified_jobs"] = 1
        self.assertEqual(engine_study.summarize(rows, 1, 2, True), (False, [], None))

    def test_unmeasured_child_cpu_does_not_invent_a_ratio(self):
        rows = [record("baseline"), record("candidate")]
        rows[0]["child_cpu_seconds"] = 0
        valid, comparisons, _ = engine_study.summarize(rows, 1, 2, True)
        self.assertTrue(valid)
        self.assertIsNone(comparisons[0]["candidate_to_baseline_child_cpu_ratio"])

    def test_invalid_or_unqueued_study_never_creates_files(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "study.json"
            for pairs, agents in ((0, 2), (5, 2), (True, 2), (1, 1), (1, 7)):
                with self.assertRaises(ValueError):
                    engine_study.run(path, pairs=pairs, agents=agents)
            with mock.patch.dict(os.environ, {"AGENT_BUILDS_JOB_ID": ""}):
                with self.assertRaisesRegex(ValueError, "shared queue"):
                    engine_study.run(path)
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_progress_receipt_prevents_repeating_uncertain_work(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "study.json"
            path.with_name(path.name + ".progress.json").write_text("retain")
            with mock.patch.object(
                engine_study, "parent_budget", return_value={}
            ), mock.patch.object(
                engine_study, "freeze", side_effect=AssertionError("work repeated")
            ):
                with self.assertRaisesRegex(ValueError, "new evidence"):
                    engine_study.run(path)

    def test_interrupted_arm_keeps_prior_evidence_and_restores_affinity_and_signals(
        self,
    ):
        before = os.sched_getaffinity(0)
        previous = {
            sig: engine_study.signal.getsignal(sig)
            for sig in (engine_study.signal.SIGINT, engine_study.signal.SIGTERM)
        }

        def freeze(root, path):
            path.parent.mkdir()
            path.write_bytes(b"frozen")
            return {"sha256": "frozen"}

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "study.json"
            with mock.patch.object(
                engine_study, "parent_budget", return_value={}
            ), mock.patch.object(
                engine_study, "freeze", side_effect=freeze
            ), mock.patch.object(
                engine_study, "baseline", return_value={}
            ), mock.patch.object(
                engine_study, "prepare", return_value=Path(temp)
            ), mock.patch.object(
                engine_study,
                "arm",
                side_effect=[record("baseline"), RuntimeError("interrupted")],
            ) as arms:
                with self.assertRaisesRegex(RuntimeError, "interrupted"):
                    engine_study.run(path, pairs=1, agents=2)
            r = json.loads(path.read_text())
            self.assertEqual(r["result"], "failed")
            self.assertEqual(len(r["records"]), 1)
            self.assertEqual(r["active_arm"]["engine"], "candidate")
            self.assertFalse(r["automatic_retries"])
            self.assertEqual(r["comparisons"], [])
            self.assertEqual(arms.call_count, 2)
        self.assertEqual(os.sched_getaffinity(0), before)
        for sig, handler in previous.items():
            self.assertEqual(engine_study.signal.getsignal(sig), handler)

    def test_baseline_archive_rejects_paths_outside_its_package(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("../outside", "bad")
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "baseline"
            with mock.patch.object(
                engine_study.subprocess, "check_output", return_value=data.getvalue()
            ):
                with self.assertRaisesRegex(ValueError, "Invalid baseline"):
                    engine_study.baseline(target)
            self.assertFalse(target.exists())

    def test_runner_override_cannot_execute_other_scenarios_or_batch_modes(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "arm"
            for scenario, mode in (
                ("python_verification", "queued"),
                ("buildr_verification", "parallel_batch"),
                ("buildr_verification", "independent"),
            ):
                with self.assertRaisesRegex(ValueError, "fixed queued"):
                    experiments.arm(
                        target, scenario, mode, 2, 2, [False], runner_package=Path(temp)
                    )
            self.assertFalse(target.exists())

    def test_invalid_observer_interval_cannot_start_an_arm(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "arm"
            for interval in (0, 0.01, 2, True, float("nan"), float("inf")):
                with self.assertRaisesRegex(ValueError, "Sampler interval"):
                    experiments.arm(
                        target,
                        "buildr_verification",
                        "queued",
                        2,
                        2,
                        [False],
                        sampler_interval=interval,
                    )
            self.assertFalse(target.exists())

    def test_native_worker_imports_frozen_workload_instead_of_wrapper_package(self):
        with tempfile.TemporaryDirectory(prefix="buildr frozen ") as temp:
            root = Path(temp)
            work = root / "worker"
            package = work / "checkout/agent_builds"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("")
            (package / "verification.py").write_text(
                "from pathlib import Path\ndef run_worker(work,identifier):\n Path(work,'observed').write_text(identifier)\n return 7\n"
            )
            foreign = root / "foreign/agent_builds"
            foreign.mkdir(parents=True)
            (foreign / "__init__.py").write_text(
                "raise AssertionError('wrapper imported by worker')\n"
            )
            identifier = "literal ; $(not-a-command)"
            result = subprocess.run(
                verification.worker_command(work, identifier),
                env=dict(os.environ, PYTHONPATH=str(foreign.parent)),
                cwd=str(root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
            )
            self.assertEqual(result.returncode, 7, result.stderr)
            self.assertEqual((work / "observed").read_text(), identifier)


if __name__ == "__main__":
    unittest.main()
