import signal
import threading
import time
import unittest
from unittest import mock

from agent_builds.runner import LocalDeadline


class DeadlineTests(unittest.TestCase):
    def test_expiry_signals_then_escalates_only_owned_group(self):
        killed = threading.Event()
        calls = []

        def send(pid, sig):
            calls.append((pid, sig))
            if sig == signal.SIGKILL:
                killed.set()

        guard = LocalDeadline(12345, time.monotonic()+.01, grace=.01)
        with mock.patch('agent_builds.runner.os.killpg', side_effect=send):
            guard.start()
            try:
                self.assertTrue(killed.wait(3))
            finally:
                guard.close()
        self.assertTrue(guard.expired.is_set())
        self.assertEqual(calls, [(12345, signal.SIGTERM), (12345, signal.SIGKILL)])
        self.assertFalse(guard.thread.is_alive())

    def test_close_joins_before_reap_and_prevents_late_signals(self):
        guard = LocalDeadline(12345, time.monotonic()+.05)
        with mock.patch('agent_builds.runner.os.killpg') as send:
            guard.start()
            guard.close()
            guard.close()  # finally/exception cleanup is idempotent
            time.sleep(.08)
            send.assert_not_called()
        self.assertFalse(guard.thread.is_alive())
        self.assertFalse(guard.expired.is_set())

    def test_missing_group_at_deadline_is_not_an_execution_or_retry(self):
        guard = LocalDeadline(12345, time.monotonic(), grace=.01)
        reached = threading.Event()

        def missing(*args):
            reached.set()
            raise ProcessLookupError()

        with mock.patch('agent_builds.runner.os.killpg', side_effect=missing):
            guard.start()
            try:
                self.assertTrue(reached.wait(3))
            finally:
                guard.close()
        self.assertTrue(guard.expired.is_set())
        self.assertFalse(guard.thread.is_alive())
