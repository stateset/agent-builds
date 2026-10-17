"""Durable private handles for terminal submissions, independent of CLI output."""

import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from agent_builds import plans, profiles
from agent_builds.__main__ import main, parser
from agent_builds.completion import wait
from agent_builds.runner import cancel_job, publish_receipt
from agent_builds.source import capture
from agent_builds.store import Store


ROOT = str(Path(__file__).resolve().parent.parent)


class TaskReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.store = Store(self.root / "state")
        self.store.update_config(
            dict(
                cpu_slots=2,
                memory_gb=4,
                min_free_gb=0,
                min_available_gb=0,
                quick_reserve_slots=0,
                quick_reserve_jobs=0,
                quick_reserve_memory_gb=0,
                nice=0,
            )
        )
        self.env = dict(
            os.environ, AGENT_BUILDS_STATE=str(self.store.root), PYTHONPATH=ROOT
        )
        self.env.pop("AGENT_BUILDS_JOB_ID", None)
        self.environment = mock.patch.dict(os.environ, self.env, clear=True)
        self.environment.start()
        self.config_before = self.store.config_path.read_bytes()

    def tearDown(self):
        try:
            # Only this isolated fixture's runners may need cleanup after an
            # assertion failure. Never inspect or signal the real shared queue.
            for row in self.store.rows(active=True):
                cancel_job(self.store, row["id"])
            deadline = time.monotonic() + 8
            while self.store.rows(active=True) and time.monotonic() < deadline:
                time.sleep(0.03)
            self.assertEqual(self.store.rows(active=True), [])
            self.assertEqual(self.store.config_path.read_bytes(), self.config_before)
        finally:
            self.environment.stop()
            self.store.close()
            self.temporary.cleanup()

    def cli(self, *arguments):
        return subprocess.run(
            [sys.executable, "-m", "agent_builds"] + list(arguments),
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
        )

    def invoke(self, *arguments):
        output, errors = io.StringIO(), io.StringIO()
        with mock.patch.object(
            sys, "argv", ["buildr"] + list(arguments)
        ), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = main()
        return code, output.getvalue(), errors.getvalue()

    def receipt(self, name="fixture"):
        return self.store.root / ("receipt-tui-" + name + ".json")

    def finish(self, handle):
        reader = Store(self.store.root, read_only=True)
        try:
            result = wait(reader, [handle], timeout=12, tail_lines=3)
        finally:
            reader.close()
        self.assertTrue(result["complete"], result)
        return result["jobs"][0]

    def save_task(self, name="check", code='print("task complete")'):
        args = parser().parse_args(
            [
                "task",
                "add",
                name,
                "--cwd",
                str(self.repo),
                "--jobs",
                "1",
                "--memory-gb",
                ".25",
                "--timeout",
                "8",
            ]
        )
        args.command = [sys.executable, "-c", code]
        return profiles.save_task(self.store, args)

    def save_reviewed(self):
        for args in (
            ["init", "-q"],
            ["config", "user.email", "fixture@example.invalid"],
            ["config", "user.name", "Fixture"],
        ):
            subprocess.check_call(
                ["git", "-C", str(self.repo)] + args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        (self.repo / "source.txt").write_text("reviewed source\n")
        subprocess.check_call(["git", "-C", str(self.repo), "add", "."])
        subprocess.check_call(["git", "-C", str(self.repo), "commit", "-qm", "fixture"])
        values = self.save_task()
        values["review"] = dict(source=capture(self.repo))
        self.assertEqual(values["review"]["source"]["status"], "captured")
        profiles.update(
            self.store, "reviewed_tasks", profiles.task_key(self.repo, "check"), values
        )
        return values

    def invalid_receipts(self):
        existing = self.receipt("existing")
        existing.write_text("keep this receipt")
        symlink = self.receipt("symlink")
        symlink.symlink_to(existing)
        dangling = self.receipt("dangling")
        dangling.symlink_to(self.root / "absent")
        return [
            self.root / "receipt-outside.json",
            self.store.root / "other-name.json",
            existing,
            symlink,
            dangling,
        ]

    def test_real_detached_run_retains_receipt_and_native_failure(self):
        receipt = self.receipt()
        result = self.cli(
            "run",
            "--cwd",
            str(self.repo),
            "--lane",
            "quick",
            "--jobs",
            "1",
            "--memory-gb",
            ".25",
            "--timeout",
            "8",
            "--detach",
            "--receipt-file",
            str(receipt),
            "--",
            sys.executable,
            "-c",
            'print("retained failure"); raise SystemExit(7)',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(receipt.read_text())
        self.assertEqual(set(value), {"job_id", "log_path", "cwd"})
        self.assertEqual(value["cwd"], str(self.repo))
        self.assertIn(value["job_id"].encode(), result.stdout)
        self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)
        row = self.finish(value["job_id"])
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["exit_code"], 7)
        self.assertIn("retained failure", "\n".join(row["log_tail"]))
        self.assertEqual(Path(value["log_path"]), self.store.log_path(row["id"]))
        self.assertEqual(json.loads(receipt.read_text()), value)
        self.assertEqual(len(self.store.rows()), 1)

    def test_real_detached_saved_task_preserves_explicit_receipt(self):
        self.save_task()
        profiles_before = (self.store.root / "profiles.json").read_bytes()
        receipt = self.receipt("task")
        result = self.cli(
            "task",
            "run",
            "check",
            "--cwd",
            str(self.repo),
            "--detach",
            "--receipt-file",
            str(receipt),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(receipt.read_text())
        self.assertEqual(value["cwd"], str(self.repo))
        self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)
        row = self.finish(value["job_id"])
        self.assertEqual(row["state"], "succeeded")
        self.assertEqual(row["exit_code"], 0)
        self.assertIn("task complete", "\n".join(row["log_tail"]))
        self.assertEqual(
            (self.store.root / "profiles.json").read_bytes(), profiles_before
        )
        self.assertTrue(receipt.is_file())
        self.assertEqual(len(self.store.rows()), 1)

    def test_default_detached_run_still_removes_temporary_receipt(self):
        result = self.cli(
            "run",
            "--cwd",
            str(self.repo),
            "--lane",
            "quick",
            "--jobs",
            "1",
            "--memory-gb",
            ".25",
            "--timeout",
            "8",
            "--detach",
            "--",
            sys.executable,
            "-c",
            'print("default detach")',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = self.store.rows()
        self.assertEqual(len(rows), 1)
        self.finish(rows[0]["id"])
        self.assertEqual(list(self.store.root.glob("receipt-*.json")), [])

    def test_unknown_saved_task_creates_no_receipt_or_submission(self):
        receipt = self.receipt("missing")
        with mock.patch(
            "agent_builds.runner.execute", side_effect=AssertionError("no runner")
        ), mock.patch(
            "agent_builds.plans.spawn", side_effect=AssertionError("no controller")
        ):
            code, output, errors = self.invoke(
                "task",
                "run",
                "missing",
                "--cwd",
                str(self.repo),
                "--detach",
                "--receipt-file",
                str(receipt),
            )
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("Unknown shortcut", errors)
        self.assertFalse(receipt.exists())
        self.assertEqual(self.store.rows(), [])
        self.assertEqual(plans.listing(self.store), [])

    def test_reviewed_task_receipt_precedes_controller_start_and_survives_failure(self):
        self.save_reviewed()
        receipt = self.receipt("reviewed")

        def fail_start(store, handle):
            value = json.loads(receipt.read_text())
            self.assertEqual(
                value,
                dict(
                    plan_id=handle,
                    cwd=str(self.repo),
                    state="created",
                    reviewed_task=True,
                ),
            )
            self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)
            current = plans.get(store, handle)
            self.assertEqual(current["state"], "queued")
            self.assertIsNone(current["owner_pid"])
            raise OSError("controller fixture failed to start")

        with mock.patch("agent_builds.plans.spawn", side_effect=fail_start) as spawn:
            code, output, errors = self.invoke(
                "task",
                "run",
                "check",
                "--cwd",
                str(self.repo),
                "--detach",
                "--receipt-file",
                str(receipt),
            )
        self.assertEqual(code, 2)
        self.assertIn("controller fixture failed", errors)
        value = json.loads(receipt.read_text())
        self.assertEqual(json.loads(output)["plan_id"], value["plan_id"])
        self.assertEqual(value["state"], "created")
        self.assertEqual(spawn.call_count, 1)
        self.assertEqual(len(plans.listing(self.store)), 1)
        self.assertEqual(plans.get(self.store, value["plan_id"])["state"], "queued")
        self.assertEqual(self.store.rows(), [])

    def test_invalid_receipt_paths_prevent_detached_and_foreground_jobs(self):
        invalid = self.invalid_receipts()
        for detached in (False, True):
            for receipt in invalid:
                with self.subTest(detached=detached, path=receipt.name):
                    arguments = [
                        "run",
                        "--cwd",
                        str(self.repo),
                        "--receipt-file",
                        str(receipt),
                    ]
                    if detached:
                        arguments.append("--detach")
                    result = self.cli(
                        *(
                            arguments
                            + ["--", sys.executable, "-c", "raise SystemExit(9)"]
                        )
                    )
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn(b"private queue path", result.stderr)
                    self.assertEqual(self.store.rows(), [])
                    self.assertEqual(plans.listing(self.store), [])
        self.assertEqual(self.receipt("existing").read_text(), "keep this receipt")
        self.assertTrue(self.receipt("symlink").is_symlink())
        self.assertFalse((self.root / "receipt-outside.json").exists())

    def test_invalid_receipts_prevent_reviewed_plan_creation(self):
        self.save_reviewed()
        with mock.patch(
            "agent_builds.plans.create", side_effect=AssertionError("no plan")
        ), mock.patch(
            "agent_builds.plans.spawn", side_effect=AssertionError("no controller")
        ):
            for receipt in self.invalid_receipts():
                with self.subTest(path=receipt.name):
                    code, output, errors = self.invoke(
                        "task",
                        "run",
                        "check",
                        "--cwd",
                        str(self.repo),
                        "--detach",
                        "--receipt-file",
                        str(receipt),
                    )
                    self.assertEqual(code, 2)
                    self.assertEqual(output, "")
                    self.assertIn("private queue path", errors)
        self.assertEqual(plans.listing(self.store), [])
        self.assertEqual(self.store.rows(), [])

    def test_atomic_publication_exposes_complete_private_json(self):
        receipt = self.receipt("atomic")
        original_link = os.link
        value = dict(job_id="fixture", cwd=str(self.repo), log_path="fixture.log")

        def inspect_link(source, target):
            self.assertEqual(json.loads(Path(source).read_text()), value)
            self.assertEqual(stat.S_IMODE(Path(source).stat().st_mode), 0o600)
            self.assertFalse(Path(target).exists())
            return original_link(source, target)

        with mock.patch("agent_builds.runner.os.link", side_effect=inspect_link):
            publish_receipt(self.store, receipt, value)
        self.assertEqual(json.loads(receipt.read_text()), value)
        self.assertEqual(list(self.store.root.glob("receipt-publish-*.tmp")), [])
        with self.assertRaisesRegex(ValueError, "new private queue path"):
            publish_receipt(self.store, receipt, dict(job_id="different"))
        self.assertEqual(json.loads(receipt.read_text()), value)

    def test_intervening_receipt_symlink_is_not_replaced(self):
        receipt = self.receipt("race")
        destination = self.root / "do-not-overwrite"
        destination.write_text("original")
        original_link = os.link

        def insert_symlink(source, target):
            Path(target).symlink_to(destination)
            return original_link(source, target)

        with mock.patch("agent_builds.runner.os.link", side_effect=insert_symlink):
            with self.assertRaises(FileExistsError):
                publish_receipt(self.store, receipt, dict(job_id="new"))
        self.assertTrue(receipt.is_symlink())
        self.assertEqual(destination.read_text(), "original")
        self.assertEqual(list(self.store.root.glob("receipt-publish-*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
