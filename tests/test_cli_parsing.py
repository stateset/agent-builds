"""Selected run parsing preserves the complete public parser's behavior."""

import argparse
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from agent_builds.__main__ import parser


class RunParsingTests(unittest.TestCase):
    def parse(self, arguments, fast):
        output, error = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            try:
                result = vars(parser(run_only=fast).parse_args(arguments))
            except SystemExit as exit:
                result = exit.code
        return result, output.getvalue(), error.getvalue()

    def test_namespace_help_and_errors_match_full_parser(self):
        cases = [
            ["run", "--", "python3", "-c", 'print("literal $value")'],
            [
                "run",
                "--cwd",
                "/tmp/with space",
                "--lane",
                "quick",
                "--backend",
                "docker",
                "--jobs",
                "4",
                "--memory-gb",
                "2.5",
                "--timeout",
                "12",
                "--lock",
                "a",
                "--lock",
                "b",
                "--resource-key",
                "out",
                "--image",
                "image",
                "--notify",
                "--track-source",
                "--input-file",
                "a",
                "--input-file",
                "b",
                "--min-free-gb",
                "1",
                "--scratch-gb",
                "2",
                "--events-json",
                "--detach",
                "--",
                "echo",
                "--lane",
                "literal",
            ],
            ["run", "--help"],
            ["run", "--jobs", "invalid"],
            ["run", "--backend", "invalid"],
            ["run", "--memory-gb"],
            ["run", "--unknown"],
            ["run"],
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                self.assertEqual(
                    self.parse(arguments, False), self.parse(arguments, True)
                )

    def test_main_dispatch_preserves_native_exit_and_literal_command(self):
        from agent_builds.__main__ import main

        with mock.patch.object(
            sys,
            "argv",
            ["buildr", "run", "--jobs", "2", "--", "echo", "$literal", "--help"],
        ), mock.patch("agent_builds.runner.execute", return_value=37) as execute:
            self.assertEqual(main(), 37)
        args = execute.call_args.args[0]
        self.assertEqual(args.command, ["--", "echo", "$literal", "--help"])
        self.assertEqual(args.jobs, 2)

    def test_fast_path_builds_fewer_argument_definitions(self):
        original = argparse.ArgumentParser.add_argument
        calls = []

        def record(command, *args, **kwargs):
            calls.append(args)
            return original(command, *args, **kwargs)

        with mock.patch.object(argparse.ArgumentParser, "add_argument", record):
            parser()
            full = len(calls)
            calls.clear()
            parser(run_only=True)
        self.assertLess(len(calls), full // 2)

    def test_help_and_invalid_run_need_no_runner_import_or_queue_state(self):
        script = """
import importlib.abc, sys
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in ('agent_builds.runner', 'agent_builds.store', 'agent_builds.metrics'):
            raise AssertionError('unneeded import: ' + fullname)
sys.meta_path.insert(0, Guard())
from agent_builds.__main__ import main
main()
"""
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "missing"
            env = dict(os.environ, AGENT_BUILDS_STATE=str(state))
            for arguments, code in [
                (["run", "--help"], 0),
                (["run", "--jobs", "bad"], 2),
                (["--help"], 0),
                (["--version"], 0),
            ]:
                with self.subTest(arguments=arguments):
                    result = subprocess.run(
                        [sys.executable, "-c", script] + arguments,
                        env=env,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    self.assertEqual(result.returncode, code, result.stderr.decode())
                    self.assertFalse(state.exists())
