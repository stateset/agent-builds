"""History inspection must never block the dashboard's input loop."""

from concurrent.futures import Future
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_builds import tui_history, tui_text
from agent_builds.store import Store


class ManualPool:
    def __init__(self):
        self.calls = []

    def submit(self, function, key):
        future = Future()
        self.calls.append((function, key, future))
        return future

    def shutdown(self, wait=False):
        pass


def report(command="cargo test", count=20):
    return dict(
        history_limit=1000,
        groups=[
            dict(
                jobs=i + 1,
                wait_p95_seconds=2,
                average_runtime_seconds=3,
                command_display=command + " check-" + str(i),
            )
            for i in range(count)
        ],
    )


class HistoryReadTests(unittest.TestCase):
    def setUp(self):
        self.pool = ManualPool()
        self.read = tui_history.HistoryRead(lambda key: key, submit=self.pool.submit)

    def finish(self, result=None, error=None):
        future = self.pool.calls[-1][2]
        if error:
            future.set_exception(error)
        else:
            future.set_result(result)

    def test_worker_returns_values_and_exceptions(self):
        self.assertEqual(
            tui_history.submit_read(lambda key: key, "/repo").result(timeout=3), "/repo"
        )

        def fail(key):
            raise OSError("read failed")

        with self.assertRaisesRegex(OSError, "read failed"):
            tui_history.submit_read(fail, "/repo").result(timeout=3)

    def test_interpreter_exits_while_read_worker_is_stalled(self):
        import os
        import subprocess
        import sys

        script = """
from threading import Event
from agent_builds.tui_history import HistoryRead
started, blocked = Event(), Event()
def slow(key):
    started.set()
    blocked.wait()
read = HistoryRead(slow)
assert read.get('/fixture', 0) is None
assert started.wait(timeout=3)
assert not read.future.done()
print('pending read abandoned at exit', flush=True)
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(Path(__file__).resolve().parent.parent),
            env=dict(os.environ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn(b"pending read abandoned at exit", result.stdout)

    def test_worker_start_failure_is_visible_and_retry_is_throttled(self):
        with mock.patch.object(
            self.read, "submit", side_effect=OSError("thread unavailable")
        ) as submit:
            self.assertIsNone(self.read.get("/a", 0))
            self.assertIn(
                "History unavailable: thread unavailable", self.read.status(0)
            )
            self.read.get("/a", 2)
            self.assertEqual(submit.call_count, 1)
        self.read.get("/a", 3)
        data = report()
        self.finish(data)
        self.assertIs(self.read.get("/a", 4), data)
        self.assertIsNone(self.read.error)

    def test_pending_read_never_waits_or_accumulates_backlog(self):
        for now in range(100):
            self.assertIsNone(self.read.get("/a", now))
        self.assertEqual(len(self.pool.calls), 1)
        self.assertIn("Loading", self.read.status(100))
        data = report()
        self.finish(data)
        self.assertIs(self.read.get("/a", 100), data)
        self.read.get("/a", 102.9)
        self.assertEqual(len(self.pool.calls), 1)
        self.read.get("/a", 103)
        self.assertEqual(len(self.pool.calls), 2)
        self.assertIn("refreshing", self.read.status(103))

    def test_switch_checkout_discards_old_result_and_starts_current_read(self):
        self.read.get("/a", 0)
        self.read.get("/b", 1)
        self.finish(report("old checkout"))
        self.assertIsNone(self.read.get("/b", 2))
        self.assertEqual([call[1] for call in self.pool.calls], ["/a", "/b"])
        data = report("current checkout")
        self.finish(data)
        self.assertIs(self.read.get("/b", 3), data)

    def test_hidden_view_drains_result_without_refreshing(self):
        self.read.get("/a", 0)
        self.read.get(None, 1)
        self.finish(report())
        self.assertIsNone(self.read.get(None, 100))
        self.assertEqual(len(self.pool.calls), 1)
        self.assertIsNone(self.read.observed)
        self.read.get("/b", 101)
        self.assertEqual(len(self.pool.calls), 2)

    def test_error_is_visible_with_throttled_retry_and_no_wrong_checkout_data(self):
        self.read.get("/a", 0)
        data = report()
        self.finish(data)
        self.read.get("/a", 1)
        self.read.get("/a", 4)
        self.finish(error=OSError("read busy"))
        self.assertIs(self.read.get("/a", 5), data)
        self.assertIn("Stale history: read busy", self.read.status(5))
        self.read.get("/a", 7)
        self.assertEqual(len(self.pool.calls), 2)
        self.read.get("/a", 8)
        self.assertEqual(len(self.pool.calls), 3)
        self.assertIsNone(self.read.get("/b", 9))
        self.assertNotIn("Stale", self.read.status(9))
        self.finish(error=RuntimeError("old checkout failure"))
        self.read.get("/b", 10)
        self.assertIsNone(self.read.error)

    def test_initial_failure_is_labelled_and_success_clears_it(self):
        self.read.get("/a", 0)
        self.finish(error=ValueError("cannot inspect"))
        self.read.get("/a", 1)
        self.assertIn("History unavailable: cannot inspect", self.read.status(1))
        self.read.get("/a", 4)
        self.finish(report())
        self.read.get("/a", 5)
        self.assertIsNone(self.read.error)
        self.assertIn("Snapshot 2s ago", self.read.status(7))

    def test_loader_uses_read_only_connection_and_does_not_initialize_missing_state(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            self.assertEqual(tui_history.inspect_history(root, "/repo")["groups"], [])
            self.assertFalse(root.exists())
            writer = Store(root)
            writer.close()
            before = (
                (root / "queue.sqlite3").read_bytes()
                if (root / "queue.sqlite3").exists()
                else None
            )
            original = Store.__init__
            opened = []

            def observe(reader, *args, **kwargs):
                opened.append(kwargs.get("read_only"))
                original(reader, *args, **kwargs)

            with mock.patch.object(Store, "__init__", observe):
                result = tui_history.inspect_history(root, "/repo")
            self.assertEqual(opened, [True])
            self.assertEqual(result["groups"], [])
            if before is not None:
                self.assertEqual((root / "queue.sqlite3").read_bytes(), before)


class HistoryViewTests(unittest.TestCase):
    def test_wrapping_cached_and_scroll_clamped_after_resize_and_checkout_switch(self):
        view = tui_history.HistoryView()
        data = report()
        with mock.patch.object(
            view, "lines", wraps=view.lines
        ) as formatted, mock.patch(
            "agent_builds.tui_text.wrap_line", wraps=tui_text.wrap_line
        ) as wrapped:
            first = view.page("/a", data, 19, 2)
            calls = wrapped.call_count
            self.assertEqual(view.page("/a", data, 19, 2), first)
            self.assertEqual(formatted.call_count, 1)
            self.assertEqual(wrapped.call_count, calls)
            view.end()
            view.page("/a", data, 19, 2)
            self.assertGreater(view.offset, 0)
            view.scroll(-1, pages=True)
            page = view.page("/a", data, 19, 2)
            self.assertNotEqual(page, first)
            view.page("/a", data, 120, 500)
            self.assertEqual(view.offset, 0)
            self.assertEqual(formatted.call_count, 1)
            view.end()
            view.page("/b", data, 19, 2)
            self.assertEqual(view.offset, 0)
            self.assertIn("/b", "".join(view.wrapped))
            self.assertNotIn("/a", "".join(view.wrapped))

    def test_controls_are_sanitized_and_oversized_output_is_labelled(self):
        view = tui_history.HistoryView()
        data = report("\x1b[31mred\x1b[0m\x00" + "界" * 70000, 1)
        view.page("/repo", data, 19, 3)
        self.assertNotIn("\x1b", "".join(view.wrapped))
        self.assertNotIn("\x00", "".join(view.wrapped))
        self.assertLess(sum(map(len, view.logical)), tui_history.MAX_CHARS + 1000)
        self.assertIn("History clipped", " ".join(view.wrapped))
        view.end()
        self.assertIn("full results", " ".join(view.page("/repo", data, 100, 3)))

    def test_loading_and_empty_window_reset_previous_lines(self):
        view = tui_history.HistoryView()
        view.page("/a", report(), 80, 3)
        view.end()
        self.assertEqual(view.page("/a", None, 80, 3), [])
        self.assertEqual(view.position(), "0/0")
        self.assertIn(
            "No completed jobs", " ".join(view.page("/b", report(count=0), 80, 10))
        )
