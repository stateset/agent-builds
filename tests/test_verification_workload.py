import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile

from agent_builds import __version__, experiments, verification


class VerificationWorkloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.work = self.root / "agent-0"
        self.checkout = self.work / "checkout"
        self.checkout.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def distributions(self):
        directory = self.checkout / "dist"
        directory.mkdir(exist_ok=True)
        metadata = "Name: agent-builds\nVersion: " + __version__ + "\n"
        with zipfile.ZipFile(directory / "fixture.whl", "w") as wheel:
            wheel.writestr("agent_builds-fixture.dist-info/METADATA", metadata)
            for name in ("tui", "tui_prompt", "runner"):
                wheel.writestr("agent_builds/" + name + ".py", "# fixture\n")
        with tarfile.open(directory / "fixture.tar.gz", "w:gz") as source:
            for name, content in (
                ("fixture/PKG-INFO", metadata),
                ("fixture/agent_builds/tui_prompt.py", "# fixture\n"),
            ):
                data = content.encode()
                info = tarfile.TarInfo(name)
                info.size = len(data)
                source.addfile(info, io.BytesIO(data))

    def run_worker(self, failure=False):
        self.distributions()

        def execute(argv, **kwargs):
            self.assertEqual(kwargs["cwd"], str(self.checkout))
            self.assertNotIn("PYTHONPATH", kwargs["env"])
            self.assertEqual(
                kwargs["env"]["AGENT_BUILDS_STATE"],
                str(self.work / "verification-state"),
            )
            return mock.Mock(
                returncode=7 if failure and "test_tui_browser.py" in argv else 0
            )

        with mock.patch(
            "agent_builds.verification.subprocess.run", side_effect=execute
        ) as run:
            code = verification.run_worker(self.work, "agent-0")
        self.assertEqual(run.call_count, 3)
        return code

    def test_native_stages_and_artifacts_are_independently_rechecked(self):
        self.assertEqual(self.run_worker(), 0)
        evidence = verification.verify(self.work, "agent-0")
        self.assertEqual(
            tuple(stage["name"] for stage in evidence["stages"]), verification.STAGES
        )
        self.assertEqual(len(evidence["artifacts"]), 2)
        self.assertEqual({row["exit_code"] for row in evidence["stages"]}, {0})

    def test_native_failure_is_not_hidden_by_valid_artifacts(self):
        self.assertEqual(self.run_worker(failure=True), 7)
        result = json.loads((self.work / "result-agent-0.json").read_text())
        self.assertEqual([stage["exit_code"] for stage in result["stages"]], [0, 7, 0])
        with self.assertRaises(ValueError):
            verification.verify(self.work, "agent-0")
        self.assertEqual(verification.read_result(self.work, "agent-0"), result)

    def test_receipts_are_bounded_and_bound_to_their_logical_agent(self):
        path = self.work / "result-agent-0.json"
        path.write_text(json.dumps({"id": "other"}))
        with self.assertRaisesRegex(ValueError, "identity"):
            verification.read_result(self.work, "agent-0")
        path.write_bytes(b"x" * (64 * 1024 + 1))
        with self.assertRaisesRegex(ValueError, "64 KiB"):
            verification.read_result(self.work, "agent-0")

    def test_changed_distribution_is_not_accepted_from_a_successful_receipt(self):
        self.run_worker()
        with zipfile.ZipFile(self.checkout / "dist/fixture.whl", "a") as wheel:
            wheel.writestr("extra", "changed")
        with self.assertRaisesRegex(ValueError, "changed"):
            verification.verify(self.work, "agent-0")

    def test_missing_stage_or_boolean_exit_code_cannot_verify(self):
        self.run_worker()
        path = self.work / "result-agent-0.json"
        result = json.loads(path.read_text())
        result["stages"][0]["exit_code"] = False
        path.write_text(json.dumps(result))
        with self.assertRaisesRegex(ValueError, "stages"):
            verification.verify(self.work, "agent-0")
        result["stages"].pop()
        path.write_text(json.dumps(result))
        with self.assertRaises(ValueError):
            verification.verify(self.work, "agent-0")

    def test_symlink_distribution_is_rejected(self):
        self.distributions()
        path = self.checkout / "dist/fixture.whl"
        target = self.root / "outside.whl"
        path.rename(target)
        path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "regular"):
            verification.artifacts(self.checkout)

    def test_missing_optional_builder_never_creates_files_or_archives(self):
        destination = self.root / "unused/source.zip"
        with mock.patch(
            "agent_builds.verification.importlib.util.find_spec", return_value=None
        ), mock.patch(
            "agent_builds.verification.subprocess.check_output",
            side_effect=AssertionError("git executed"),
        ):
            with self.assertRaisesRegex(ValueError, "optional build"):
                verification.snapshot(self.root, destination)
        self.assertFalse(destination.parent.exists())

    def test_dirty_source_is_rejected_before_an_archive_is_created(self):
        destination = self.root / "unused/source.zip"
        with mock.patch(
            "agent_builds.verification.importlib.util.find_spec", return_value=True
        ), mock.patch(
            "agent_builds.verification.subprocess.check_output",
            return_value=b" M tracked.py\n",
        ) as git:
            with self.assertRaisesRegex(ValueError, "clean, committed"):
                verification.snapshot(self.root, destination)
        self.assertEqual(git.call_count, 1)
        self.assertFalse(destination.parent.exists())

    def test_archive_identity_and_independent_checkouts_are_preserved(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("agent_builds/__init__.py", "# frozen\n")
        bundle = self.root / "source.zip"
        with mock.patch(
            "agent_builds.verification.importlib.util.find_spec", return_value=True
        ), mock.patch(
            "agent_builds.verification.subprocess.check_output",
            side_effect=[b"", data.getvalue()],
        ):
            identity = verification.snapshot(self.root, bundle)
        self.assertEqual(
            identity["sha256"], hashlib.sha256(data.getvalue()).hexdigest()
        )
        first, second = self.root / "first", self.root / "second"
        first.mkdir()
        second.mkdir()
        a, b = verification.prepare(bundle, first), verification.prepare(bundle, second)
        (a / "agent_builds/__init__.py").write_text("changed\n")
        self.assertEqual((b / "agent_builds/__init__.py").read_text(), "# frozen\n")

    def test_archive_cannot_write_outside_the_private_checkout(self):
        bundle = self.root / "bad.zip"
        with zipfile.ZipFile(bundle, "w") as archive:
            archive.writestr("../outside", "bad")
        work = self.root / "bad-worker"
        work.mkdir()
        with self.assertRaisesRegex(ValueError, "Invalid"):
            verification.prepare(bundle, work)
        self.assertFalse((work / "outside").exists())


class ExperimentEvidenceTests(unittest.TestCase):
    def test_interrupted_arm_retains_completed_evidence_without_retry(self):
        first = dict(
            mode="independent",
            scenario="shared_output",
            verified_jobs=2,
            jobs=2,
            wall_seconds=1,
        )
        before = os.sched_getaffinity(0)
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "report.json"
            with mock.patch.object(
                experiments, "parent_budget", return_value={}
            ), mock.patch.object(
                experiments,
                "arm",
                side_effect=[first, RuntimeError("fixture interrupted")],
            ) as arm:
                with self.assertRaisesRegex(RuntimeError, "fixture interrupted"):
                    experiments.run(
                        output, pairs=1, agents=2, scenarios=["shared_output"]
                    )
            result = json.loads(output.read_text())
            self.assertEqual(result["result"], "failed")
            self.assertEqual(len(result["records"]), 1)
            self.assertEqual(result["active_arm"]["mode"], "queued")
            self.assertEqual(result["comparisons"], [])
            self.assertFalse(result["automatic_retries"])
            self.assertEqual(arm.call_count, 2)
            self.assertTrue(output.with_name(output.name + ".progress.json").exists())
        self.assertEqual(os.sched_getaffinity(0), before)

    def test_progress_receipt_prevents_repeating_an_uncertain_experiment(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "report.json"
            output.with_name(output.name + ".progress.json").write_text("retained")
            with mock.patch.object(
                experiments, "parent_budget", return_value={}
            ), mock.patch.object(
                experiments, "arm", side_effect=AssertionError("work retried")
            ):
                with self.assertRaisesRegex(ValueError, "new path"):
                    experiments.run(output, pairs=1, agents=2)

    def test_failed_real_workload_withholds_performance_ratios(self):
        def arm(path, scenario, mode, agents, cpus, cancelled, bundle=None):
            return dict(
                mode=mode,
                scenario=scenario,
                verified_jobs=agents if mode == "independent" else agents - 1,
                jobs=agents,
                wall_seconds=1,
                verified_completions_per_second=1,
                sampled_process_tree_peak_rss_gb=1,
            )

        def freeze(root, path):
            path.parent.mkdir()
            path.write_bytes(b"fixture")
            return {"sha256": "fixture"}

        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(
                experiments, "parent_budget", return_value={}
            ), mock.patch.object(experiments, "arm", side_effect=arm), mock.patch(
                "agent_builds.verification.snapshot", side_effect=freeze
            ), mock.patch(
                "agent_builds.source.capture", return_value={"status": "captured"}
            ), mock.patch("agent_builds.source.changed", return_value=False):
                result = experiments.run(
                    Path(temp) / "report.json",
                    pairs=1,
                    agents=2,
                    scenarios=["buildr_verification"],
                    include_batches=True,
                )
        self.assertFalse(result["verification_workload_valid"])
        self.assertIsNone(result["comparisons"][0]["verified_throughput_ratio"])
        self.assertIsNone(result["comparisons"][0]["queued_to_independent_wall_ratio"])
        self.assertTrue(
            all(
                item["batch_to_separate_queue_wall_ratio"] is None
                for item in result["batch_comparisons"]
            )
        )


if __name__ == "__main__":
    unittest.main()
