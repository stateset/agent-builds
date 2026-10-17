"""Bounded immutable log reads keep worker lifetimes separate from UI state."""

from concurrent.futures import Future
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from unittest import mock

from agent_builds import tui_log_reads, tui_logs
from agent_builds.tui_log_reads import LogReads, LogRequest, LogResult, load_log
from agent_builds.tui_logs import LogView, TailCache, MAX_BYTES, TAIL_BYTES, signature
from agent_builds.tui_reads import submit_read


def eager(loader, request):
    future = Future()
    try:
        future.set_result(loader(request))
    except Exception as error:
        future.set_exception(error)
    return future


class LogLoaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "job.log"

    def request(self, mode="focused", known=None, force=False):
        return LogRequest(1, (mode, "job", str(self.path)), known, force)

    def test_requests_results_and_line_payloads_are_immutable(self):
        self.path.write_text("first\nlast\n")
        request = self.request()
        result = load_log(request)
        self.assertEqual(result.lines, ("first", "last"))
        self.assertIs(result.request, request)
        with self.assertRaises(AttributeError):
            request.epoch = 2
        with self.assertRaises(AttributeError):
            result.error = "changed"
        with self.assertRaises(TypeError):
            result.lines[0] = "changed"

    def test_unchanged_metadata_skips_content_open_unless_explicitly_rechecked(self):
        self.path.write_text("old\n")
        initial = load_log(self.request())
        before = self.path.stat()
        self.path.write_text("new\n")
        with mock.patch.object(Path, "stat", return_value=before), mock.patch(
            "agent_builds.tui_log_reads.read_recent", wraps=tui_logs.read_recent
        ) as read:
            unchanged = load_log(self.request(known=initial.signature))
            self.assertIsNone(unchanged.lines)
            read.assert_not_called()
            forced = load_log(self.request(known=initial.signature, force=True))
        self.assertEqual(forced.lines, ("new",))
        read.assert_called_once_with(str(self.path), MAX_BYTES)

    def test_modes_keep_existing_line_cleaning_and_empty_snapshot_semantics(self):
        self.path.write_bytes(b"old\rnew\n\x1b[31merror\x1b[0m\n\tend\n")
        inline = load_log(self.request("inline"))
        focused = load_log(self.request())
        self.assertEqual(inline.lines, ("old", "new", "\x1b[31merror\x1b[0m", "\tend"))
        self.assertEqual(focused.lines, ("new", "error", "    end"))
        self.path.write_bytes(b"")
        empty = load_log(self.request(known=focused.signature))
        self.assertEqual(empty.lines, ())
        self.assertIsNotNone(empty.signature)
        self.assertIsNone(empty.error)

    def test_rotation_truncation_and_stat_open_race_use_descriptor_identity(self):
        self.path.write_text("original\n")
        first = load_log(self.request())
        before = self.path.stat()
        self.path.rename(self.root / "old.log")
        self.path.write_text("replacement\n")
        observed = signature(os.stat(str(self.path)))
        with mock.patch.object(Path, "stat", return_value=before):
            replacement = load_log(self.request(force=True))
        self.assertEqual(replacement.lines, ("replacement",))
        self.assertEqual(replacement.signature, observed)
        self.assertNotEqual(replacement.signature, first.signature)
        self.path.write_text("x\n")
        truncated = load_log(self.request(known=replacement.signature))
        self.assertEqual(truncated.lines, ("x",))
        self.path.write_text("regrown\nlast\n")
        regrown = load_log(self.request(known=truncated.signature))
        self.assertEqual(regrown.lines, ("regrown", "last"))

    def test_content_reads_keep_mode_bounds_even_when_file_grows_during_read(self):
        self.path.write_bytes(
            b"old-private-start\n" + b"line\n" * MAX_BYTES + b"latest\n"
        )
        original = tui_logs.os.fdopen
        limits = []

        def opened(*args, **kwargs):
            handle = original(*args, **kwargs)
            wrapper = mock.MagicMock(wraps=handle)
            wrapper.__enter__.return_value = wrapper
            wrapper.__exit__.side_effect = lambda *unused: handle.close()

            def read(limit):
                limits.append(limit)
                with self.path.open("ab") as writer:
                    writer.write(b"appended\n" * 1000)
                return handle.read(limit)

            wrapper.read.side_effect = read
            return wrapper

        with mock.patch("agent_builds.tui_logs.os.fdopen", side_effect=opened):
            for mode, bound in (("inline", TAIL_BYTES), ("focused", MAX_BYTES)):
                result = load_log(self.request(mode))
                self.assertTrue(result.clipped)
                self.assertLessEqual(sum(len(line) + 1 for line in result.lines), bound)
                self.assertNotIn("old-private-start", result.lines)
                self.assertIsNone(result.error)
        self.assertEqual(limits, [TAIL_BYTES, MAX_BYTES])

    def test_nonregular_and_missing_files_are_reported_and_descriptors_close(self):
        missing = load_log(self.request())
        self.assertEqual(missing.lines, ())
        self.assertIn("not available yet", missing.error)
        self.assertFalse(self.path.exists())
        os.mkfifo(str(self.path))
        descriptors = []
        original = tui_logs.os.fdopen

        def opened(descriptor, *args, **kwargs):
            descriptors.append(descriptor)
            return original(descriptor, *args, **kwargs)

        with mock.patch("agent_builds.tui_logs.os.fdopen", side_effect=opened):
            for name, request in (
                ("fifo", self.request()),
                ("directory", LogRequest(1, ("inline", "directory", str(self.root)))),
            ):
                with self.subTest(path=name):
                    before = len(descriptors)
                    result = load_log(request)
                    self.assertEqual(result.error, "Log is not a regular file")
                    self.assertEqual(len(descriptors), before + 1)
                    # Check each failure immediately, before another open can
                    # reuse the descriptor number and conceal a leaked handle.
                    with self.assertRaises(OSError):
                        os.fstat(descriptors[-1])

    def test_fdopen_construction_failure_closes_the_owned_descriptor(self):
        self.path.write_text("fixture\n")
        descriptors = []

        def fail(descriptor, *unused, **kwargs):
            descriptors.append(descriptor)
            raise OSError("fixture fdopen failure")

        with mock.patch("agent_builds.tui_logs.os.fdopen", side_effect=fail):
            result = load_log(self.request())
        self.assertIn("OSError", result.error)
        self.assertEqual(len(descriptors), 1)
        with self.assertRaises(OSError):
            os.fstat(descriptors[0])

    def test_all_filesystem_operations_run_on_the_worker_thread(self):
        self.path.write_text("thread-local read\n")
        observed, submitted = [], []
        stat, opened, fstat = Path.stat, tui_logs.os.open, tui_logs.os.fstat

        def trace(name, operation):
            def call(*args, **kwargs):
                observed.append((name, threading.get_ident()))
                return operation(*args, **kwargs)

            return call

        def submit(loader, request):
            future = submit_read(loader, request)
            submitted.append(future)
            return future

        reads = LogReads(submit=submit)
        with mock.patch.object(Path, "stat", trace("stat", stat)), mock.patch(
            "agent_builds.tui_logs.os.open", side_effect=trace("open", opened)
        ), mock.patch(
            "agent_builds.tui_logs.os.fstat", side_effect=trace("fstat", fstat)
        ):
            result = reads.get(self.request().key, None, False, 0)
            submitted[0].result(timeout=5)
            if result is None:
                result = reads.get(self.request().key, None, False, 0.1)
        self.assertEqual(result.lines, ("thread-local read",))
        self.assertEqual({name for name, unused in observed}, {"stat", "open", "fstat"})
        self.assertEqual(len({owner for unused, owner in observed}), 1)
        self.assertTrue(
            all(owner != threading.get_ident() for unused, owner in observed)
        )


class LogCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.requests, self.futures = [], []

        def submit(unused_loader, request):
            self.requests.append(request)
            future = Future()
            self.futures.append(future)
            return future

        self.reads = LogReads(submit=submit)
        self.first = ("inline", "first", "/fixture/first.log")
        self.second = ("focused", "second", "/fixture/second.log")

    def finish(self, index, *lines, **kwargs):
        self.futures[index].set_result(
            LogResult(self.requests[index], (1, 2, 3, 4, 5), tuple(lines), **kwargs)
        )

    def test_one_pending_worker_has_no_backlog_and_a_b_a_discards_the_old_epoch(self):
        self.assertIsNone(self.reads.get(self.first, None, False, 0))
        original_epoch = self.reads.epoch
        for now in range(1, 200):
            self.assertIsNone(self.reads.get(self.first, None, False, now))
        self.reads.get(self.second, None, False, 201)
        self.reads.get(self.first, None, True, 202)
        self.assertEqual(len(self.requests), 1)
        self.assertGreater(self.reads.epoch, original_epoch)
        self.finish(0, "obsolete first")
        self.assertIsNone(self.reads.get(self.first, None, True, 203))
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[1].epoch, self.reads.epoch)
        self.assertTrue(self.requests[1].force_content)
        self.finish(1, "current first")
        result = self.reads.get(self.first, None, False, 204)
        self.assertEqual(result.lines, ("current first",))
        self.assertIsNone(self.reads.get(self.first, result.signature, False, 204.1))
        self.assertEqual(len(self.requests), 2)

    def test_hidden_worker_drains_errors_without_refreshing_or_exposing_them(self):
        self.reads.get(self.first, None, False, 0)
        self.reads.get(None, None, False, 1)
        epoch = self.reads.epoch
        for now in range(2, 50):
            self.assertIsNone(self.reads.get(None, None, False, now))
        self.assertEqual(self.reads.epoch, epoch)
        self.futures[0].set_exception(OSError("old job failed"))
        self.assertIsNone(self.reads.get(None, None, False, 50))
        self.assertIsNone(self.reads.future)
        self.assertIsNone(self.reads.error)
        self.assertEqual(len(self.requests), 1)
        self.assertIsNone(self.reads.get(self.second, None, False, 51))
        self.assertEqual(len(self.requests), 2)

    def test_hold_and_end_invalidate_pending_results_for_the_same_source(self):
        self.reads.get(self.second, None, False, 0)
        self.reads.invalidate()  # hold before the pending read completes
        self.reads.hide()
        self.reads.invalidate()  # End requests a new live observation
        self.futures[0].set_exception(OSError("before hold"))
        self.assertIsNone(self.reads.get(self.second, None, False, 1))
        self.assertIsNone(self.reads.error)
        self.assertEqual(len(self.requests), 2)
        self.assertNotEqual(self.requests[0].epoch, self.requests[1].epoch)
        self.finish(1, "after End")
        self.assertEqual(
            self.reads.get(self.second, None, False, 2).lines, ("after End",)
        )

    def test_errors_are_scoped_throttled_and_cleared_by_success(self):
        self.reads.get(self.first, None, False, 0)
        self.futures[0].set_exception(PermissionError("fixture permission denied"))
        failed = self.reads.get(self.first, None, False, 0.1)
        self.assertIn("PermissionError", failed.error)
        self.assertEqual(failed.lines, ())
        self.assertIn("PermissionError", self.reads.status(0.2))
        self.assertIsNone(self.reads.get(self.first, None, False, 0.59))
        self.assertEqual(len(self.requests), 1)
        self.reads.get(self.first, None, False, 0.61)
        self.finish(1, "recovered")
        self.assertIsNone(self.reads.get(self.first, None, False, 0.7).error)
        self.assertIsNone(self.reads.error)

    def test_worker_start_failure_is_returned_once_and_retry_is_throttled(self):
        def fail(*unused):
            raise RuntimeError("cannot start fixture thread")

        with mock.patch.object(self.reads, "submit", side_effect=fail) as submit:
            result = self.reads.get(self.first, None, False, 0)
            self.assertIn("RuntimeError", result.error)
            self.assertIsNone(self.reads.get(self.first, None, False, 0.1))
            submit.assert_called_once()
            self.assertIn(
                "RuntimeError", self.reads.get(self.first, None, False, 0.5).error
            )
        self.assertEqual(submit.call_count, 2)

    def test_unknown_mode_never_starts_a_worker(self):
        with self.assertRaisesRegex(ValueError, "Unknown log read mode"):
            self.reads.get(("unknown", "job", "/fixture"), None, False, 0)
        self.assertEqual(self.requests, [])


class LogAdoptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "job.log"
        self.path.write_text("first hit\nlast\n")
        self.inline = ("inline", "job", str(self.path))
        self.focused = ("focused", "job", str(self.path))

    def test_cache_adoption_and_paging_do_not_access_files_and_reject_other_sources(
        self,
    ):
        inline = load_log(LogRequest(1, self.inline))
        focused = load_log(LogRequest(1, self.focused))
        tail, view = TailCache(), LogView()
        with mock.patch.object(
            Path, "stat", side_effect=AssertionError("UI stat")
        ), mock.patch(
            "agent_builds.tui_logs.os.open", side_effect=AssertionError("UI open")
        ):
            tail.select("job", self.path)
            view.select("job", self.path)
            self.assertTrue(tail.apply(inline, 0))
            self.assertTrue(view.apply(focused, 0))
            self.assertEqual(tail.page(1), ["last"])
            self.assertEqual(tail.page(0), [])
            self.assertEqual(view.page(40, 3), ["first hit", "last"])
            view.page(5, 1)
            other = LogResult(
                LogRequest(2, ("focused", "other", str(self.path))), lines=("wrong",)
            )
            self.assertFalse(view.apply(other, 1))
            self.assertFalse(tail.apply(focused, 1))
            self.assertEqual(tail.page(2), ["first hit", "last"])
        self.assertTrue(tail.loaded)
        self.assertTrue(view.loaded)

    def test_unchanged_result_preserves_wrapping_search_and_content_recheck_timestamp(
        self,
    ):
        tail, view = TailCache(), LogView()
        tail.select("job", self.path)
        view.select("job", self.path)
        first = load_log(LogRequest(1, self.focused))
        view.apply(first, 0)
        view.search("hit", 8, 2)
        view.end()
        view.page(8, 2)
        wrapped, matches = view.wrapped, view.matches
        unchanged = LogResult(
            LogRequest(1, self.focused, first.signature), first.signature
        )
        with mock.patch.object(
            view, "_find_matches", side_effect=AssertionError("unchanged content")
        ):
            self.assertTrue(view.apply(unchanged, 1))
        self.assertIs(view.wrapped, wrapped)
        self.assertIs(view.matches, matches)
        self.assertEqual(view.query, "hit")
        self.assertEqual(view.observed, 1)
        view.end()
        self.assertEqual(view.observed, 1)
        tail.apply(load_log(LogRequest(1, self.inline)), 0)
        tail.apply(LogResult(LogRequest(1, self.inline), tail.signature), 4)
        self.assertEqual(tail.last_content_read, 0)
        self.assertEqual(tail.last_poll, 4)

    def test_held_search_snapshot_rejects_late_data_then_end_adopts_new_content(self):
        view = LogView()
        view.select("job", self.path)
        view.apply(load_log(LogRequest(1, self.focused)), 0)
        view.search("hit", 40, 3)
        original = (view.lines[:], view.wrapped[:], view.top, view.match_index)
        self.path.write_text("new hit\nnew last\n")
        late = load_log(LogRequest(1, self.focused))
        self.assertFalse(view.apply(late, 1))
        self.assertEqual(
            (view.lines, view.wrapped, view.top, view.match_index), original
        )
        view.end()
        self.assertTrue(view.apply(late, 2))
        self.assertEqual(view.query, "hit")
        self.assertEqual(view.matches, [0])
        self.assertEqual(view.page(40, 3), ["new hit", "new last"])

    def test_inline_forced_content_recheck_uses_last_actual_read_not_metadata_polls(
        self,
    ):
        tail, reads = TailCache(), LogReads(submit=eager)
        tail.select("job", self.path)
        first = reads.get(self.inline, tail.signature, True, 0)
        tail.apply(first, 0)
        before = self.path.stat()
        self.path.write_text("later hit\nlater\n")
        with mock.patch.object(Path, "stat", return_value=before), mock.patch(
            "agent_builds.tui_log_reads.read_recent", wraps=tui_logs.read_recent
        ) as read:
            for now in (0.5, 4.5):
                tail.apply(reads.get(self.inline, tail.signature, False, now), now)
            read.assert_not_called()
            self.assertEqual(tail.page(1), ["last"])
            now = 5
            due = now - tail.last_content_read >= tui_logs.TAIL_RECHECK_SECONDS
            tail.apply(reads.get(self.inline, tail.signature, due, now), now)
            read.assert_called_once_with(str(self.path), TAIL_BYTES)
        self.assertEqual(tail.page(1), ["later"])
        self.assertEqual(tail.last_content_read, 5)

    def test_matching_read_errors_clear_only_current_cache_and_recover_on_retry(self):
        tail = TailCache()
        tail.select("job", self.path)
        tail.apply(load_log(LogRequest(1, self.inline)), 0)
        self.path.unlink()
        tail.apply(load_log(LogRequest(1, self.inline)), 1)
        self.assertFalse(tail.loaded)
        self.assertEqual(tail.page(3), [])
        self.assertIn("not available yet", tail.error)
        self.path.write_text("available again\n")
        tail.apply(load_log(LogRequest(1, self.inline)), 2)
        self.assertTrue(tail.loaded)
        self.assertIsNone(tail.error)
        self.assertEqual(tail.page(3), ["available again"])


class LogWorkerExitTests(unittest.TestCase):
    def test_interpreter_can_exit_while_one_log_read_is_stalled(self):
        source = textwrap.dedent("""\
            from threading import Event
            from unittest import mock
            from agent_builds.tui_log_reads import LogReads
            entered, blocked = Event(), Event()
            def stalled(request):
                entered.set()
                blocked.wait()
            with mock.patch('agent_builds.tui_log_reads.load_log', stalled):
                reads = LogReads()
                reads.get(('inline', 'fixture', '/never-opened'), None, False, 0)
                assert entered.wait(2), 'worker did not start'
                print('daemon log read pending', flush=True)
        """)
        root = str(Path(__file__).resolve().parent.parent)
        env = dict(
            os.environ, PYTHONPATH=root + os.pathsep + os.environ.get("PYTHONPATH", "")
        )
        result = subprocess.run(
            [sys.executable, "-c", source],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("daemon log read pending", result.stdout)


if __name__ == "__main__":
    unittest.main()
