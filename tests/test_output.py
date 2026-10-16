"""Reader disconnects cannot stop execution or replace durable native outcomes."""

import errno
import io
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from agent_builds import runner
from agent_builds.__main__ import parser
from agent_builds.output import OutputRelay
from agent_builds.store import Store


class OutputTests(unittest.TestCase):
    def test_closed_observer_is_not_retried_and_other_stream_stays_writable(self):
        relay, broken, other = OutputRelay(), mock.Mock(), io.StringIO()
        broken.write.side_effect = BrokenPipeError()
        broken.fileno.side_effect = io.UnsupportedOperation()
        self.assertFalse(relay.write(broken, "first"))
        self.assertFalse(relay.write(broken, "second"))
        broken.write.assert_called_once_with("first")
        self.assertTrue(relay.write(other, "readable\n"))
        self.assertEqual(other.getvalue(), "readable\n")

    def test_non_pipe_write_and_flush_errors_still_propagate(self):
        for operation in ("write", "flush"):
            relay, stream = OutputRelay(), mock.Mock()
            getattr(stream, operation).side_effect = OSError(
                errno.ENOSPC, "fixture full"
            )
            with self.assertRaises(OSError) as raised:
                relay.write(stream, "data")
            self.assertEqual(raised.exception.errno, errno.ENOSPC)
            self.assertEqual(relay.closed, {})


class OutputRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "state")
        self.store.update_config(dict(min_free_gb=0, min_available_gb=0, nice=0))
        self.env = dict(os.environ, AGENT_BUILDS_STATE=str(self.store.root))
        self.env.pop("AGENT_BUILDS_JOB_ID", None)  # private fixture runners only
        self.processes = []

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=8)
            for stream in (process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
        self.store.close()
        self.temp.cleanup()

    def disconnected_run(
        self, code, events_json=False, close_output=False, after_running=False
    ):
        marker = self.root / ("attempt-" + str(len(self.processes)))
        release = marker.with_suffix(".release")
        source = "\n".join(
            [
                "from pathlib import Path; import os,time",
                "p=Path(" + repr(str(marker)) + ")",
                "p.write_text(p.read_text()+'attempt\\n' if p.exists() else 'attempt\\n')",
                "release=Path(" + repr(str(release)) + ")",
                "deadline=time.monotonic()+10",
                "while not release.exists():",
                " if time.monotonic()>deadline: raise SystemExit(99)",
                " time.sleep(.01)",
                "os.write(1,b'native output\\n')",
                "raise SystemExit(" + str(code) + ")",
            ]
        )
        args = [
            sys.executable,
            "-m",
            "agent_builds",
            "run",
            "--cwd",
            str(self.root),
            "--lane",
            "quick",
            "--jobs",
            "1",
            "--memory-gb",
            ".25",
            "--timeout",
            "10",
        ]
        if events_json:
            args.append("--events-json")
        args += ["--", sys.executable, "-c", source]
        process = subprocess.Popen(
            args,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self.processes.append(process)
        events = process.stderr if events_json else process.stdout
        # Read the accepted handle before dropping the observer. Waiting on a
        # real lifecycle boundary avoids sleeps as an output/launch handshake.
        with selectors.DefaultSelector() as selector:
            selector.register(events, selectors.EVENT_READ)
            self.assertTrue(selector.select(10), "No queue receipt")
            first = events.readline()
        handle = (
            json.loads(first)["job_id"] if events_json else first.decode().split()[1]
        )
        if after_running:
            with selectors.DefaultSelector() as selector:
                selector.register(events, selectors.EVENT_READ)
                self.assertTrue(selector.select(10), "No running event")
                line = events.readline()
            self.assertIn(b"running", line)
        closed = process.stdout if close_output else events
        closed.close()
        if closed is process.stdout:
            process.stdout = None
        else:
            process.stderr = None
        release.touch()
        # Drain the surviving observer while waiting. Even lifecycle messages
        # may exceed a small pipe buffer if admission reports a temporary wait.
        stdout, stderr = process.communicate(timeout=15)
        self.assertEqual(process.returncode, code)
        if close_output:
            remaining = [json.loads(line) for line in stderr.splitlines()]
            self.assertIn("running", [event["state"] for event in remaining])
            self.assertEqual(remaining[-1]["exit_code"], code)
        elif events_json:
            self.assertIn(b"native output\n", stdout)
        row = self.store.get(handle)
        self.assertEqual(row["exit_code"], code)
        self.assertEqual(row["state"], "succeeded" if code == 0 else "failed")
        self.assertTrue(row["usage"]["complete"])
        self.assertEqual(marker.read_text(), "attempt\n")
        self.assertIn(b"native output\n", self.store.log_path(handle).read_bytes())
        return row

    def test_closed_plain_lifecycle_reader_preserves_execution_and_exit(self):
        for code in (0, 7):
            self.disconnected_run(code)

    def test_closed_json_event_reader_keeps_native_stdout_and_outcome(self):
        for code in (0, 7):
            self.disconnected_run(code, events_json=True)

    def test_closed_native_output_reader_does_not_break_json_events_or_exit(self):
        for code in (0, 7):
            self.disconnected_run(code, events_json=True, close_output=True)

    def test_closed_completion_reader_cannot_replace_already_committed_outcome(self):
        self.disconnected_run(7, after_running=True)

    def test_post_completion_error_does_not_rewrite_durable_success_or_failure(self):
        for code in (0, 7):
            args = parser().parse_args(
                [
                    "run",
                    "--cwd",
                    str(self.root),
                    "--lane",
                    "quick",
                    "--jobs",
                    "1",
                    "--memory-gb",
                    ".25",
                    "--notify",
                    "--",
                    sys.executable,
                    "-c",
                    "raise SystemExit(" + str(code) + ")",
                ]
            )
            with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(
                runner,
                "notify",
                side_effect=RuntimeError("fixture notification failure"),
            ), mock.patch("sys.stdout", io.StringIO()):
                with self.assertRaisesRegex(
                    RuntimeError, "fixture notification failure"
                ):
                    runner.execute(args)
            row = self.store.rows(limit=1)[0]
            self.assertEqual(row["state"], "succeeded" if code == 0 else "failed")
            self.assertEqual(row["exit_code"], code)
            self.assertEqual(row["reason"], "")
            self.assertTrue(row["usage"]["complete"])


if __name__ == "__main__":
    unittest.main()
