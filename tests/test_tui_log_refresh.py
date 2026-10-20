"""Log acquisition must not delay dashboard input or cross user intent."""

from concurrent.futures import Future
import curses
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest
from unittest import mock

import test_tui_browser as fixtures

from agent_builds.store import Store
from agent_builds.tui_logs import LogView, TailCache


def completed(loader, request):
    future = Future()
    try:
        future.set_result(loader(request))
    except Exception as error:
        future.set_exception(error)
    return future


def frame_text(frame):
    return "\n".join(frame.values())


class PendingLogs:
    def __init__(self):
        self.calls = []

    def __call__(self, loader, request):
        future = Future()
        self.calls.append((loader, request, future))
        return future

    def finish(self, index=-1, error=None):
        loader, request, future = self.calls[index]
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(loader(request))


class TuiLogRefreshTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TuiBrowserTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root, self.store, self.jobs = (
            self.fixture.root,
            self.fixture.store,
            self.fixture.jobs,
        )
        self.before = list(
            map(tuple, self.store.db.execute("SELECT * FROM jobs ORDER BY id"))
        )
        self.config_before = self.store.config_path.read_bytes()
        self.addCleanup(self.assert_read_only)
        for index, job in enumerate(self.jobs):
            self.store.log_path(job).write_text("private job {} output\n".format(index))

    def assert_read_only(self):
        self.assertEqual(
            list(map(tuple, self.store.db.execute("SELECT * FROM jobs ORDER BY id"))),
            self.before,
        )
        self.assertEqual(self.store.config_path.read_bytes(), self.config_before)

    def dashboard(self, keys, dimensions=(24, 100), **kwargs):
        with mock.patch.object(
            TailCache, "read", side_effect=AssertionError("synchronous inline log read")
        ), mock.patch.object(
            LogView, "poll", side_effect=AssertionError("synchronous focused log read")
        ), mock.patch.object(
            Store, "_ensure_schema", side_effect=AssertionError("queue initialized")
        ):
            return self.fixture.dashboard(keys, dimensions, **kwargs)

    def test_focused_rechecks_unchanged_metadata_and_end_forces_immediate_content(self):
        from agent_builds.tui_log_reads import LogResult

        for size in ((24, 100), (8, 20)):
            calls, now, content = [], [100], ["OLD"]
            signature = (1, 2, 3, 4, 5)

            def submit(loader, request):
                if request.key[0] != "focused":
                    return completed(loader, request)
                calls.append(request)
                future = Future()
                fresh = request.force_content or request.known_signature != signature
                future.set_result(
                    LogResult(request, signature, tuple(content) if fresh else None)
                )
                return future

            steps = iter([ord("l"), -1, -1, curses.KEY_END, ord("q")])

            def key():
                value = next(steps)
                if value == -1:
                    now[0] = 100.6 if now[0] == 100 else 105
                    content[:] = ["NEW"]
                elif value == curses.KEY_END:
                    content[:] = ["END"]
                return value

            with self.subTest(size=size):
                frames = self.dashboard(
                    key, size, clock=lambda: now[0], log_submit=submit
                )
            self.assertIn("OLD", frame_text(frames[2]))
            self.assertNotIn("NEW", frame_text(frames[2]))
            self.assertIn("NEW", frame_text(frames[3]))
            self.assertIn("END", frame_text(frames[-1]))
            self.assertEqual(
                [request.force_content for request in calls], [True, False, True, True]
            )

    def test_empty_states_remain_visible_with_pending_job_status_in_minimum_panes(self):
        for size in ((24, 100), (8, 20)):
            for mode, expected in (
                ("empty", "Empty log snapshot"),
                ("missing", "Log unavailable"),
                ("pending", "Loading log"),
                ("held", "No captured output"),
            ):
                path = self.store.log_path(self.jobs[0])
                path.write_bytes(b"")
                if mode == "missing":
                    path.unlink()
                pending = PendingLogs()
                state = Future()
                keys = [ord("l")] + ([ord(" ")] if mode == "held" else []) + [ord("q")]
                with self.subTest(size=size, mode=mode):
                    frames = self.dashboard(
                        keys,
                        size,
                        log_submit=pending
                        if mode in ("pending", "held")
                        else completed,
                        log_status_submit=lambda loader, key: state,
                    )
                self.assertEqual(frames[-1][3], expected)
                self.assertIn("State loading", frames[-1][2])
                self.assertFalse(state.done())
                if size[1] > 20:
                    self.assertIn("rows 0-0/0", frames[-1][0])

    def test_empty_placeholder_tracks_append_truncate_and_held_snapshot(self):
        for size in ((24, 100), (8, 20)):
            path = self.store.log_path(self.jobs[0])
            path.write_bytes(b"")
            now = [100]
            ticks = [0]
            steps = iter([ord("l"), -1, -1, ord(" "), -1, curses.KEY_END, ord("q")])

            def key():
                value = next(steps)
                if value == -1:
                    ticks[0] += 1
                    now[0] += 1
                    path.write_text({1: "OUTPUT\n", 2: "", 3: "AFTER\n"}[ticks[0]])
                return value

            with self.subTest(size=size):
                frames = self.dashboard(key, size, clock=lambda: now[0])
            self.assertEqual(frames[1][3], "Empty log snapshot")
            self.assertEqual(frames[2][3], "OUTPUT")
            for frame in frames[3:6]:
                self.assertEqual(frame[3], "Empty log snapshot")
            self.assertIn("HELD", frames[5][0])
            self.assertEqual(frames[-1][3], "AFTER")

    def test_empty_browser_does_not_advertise_loading_an_unselected_log(self):
        for size in ((24, 100), (8, 20)):
            with self.subTest(size=size):
                frames = self.dashboard(
                    [ord("/"), ord("l"), ord("q")], size, query="no-matching-job"
                )
            self.assertEqual(frames[-1][3], "No job selected")
            self.assertNotIn("Loading log", frame_text(frames[-1]))
            self.assertNotIn("Empty log snapshot", frame_text(frames[-1]))

    def test_pending_logs_allow_selection_help_resize_and_quit_in_both_layouts(self):
        for mode in ("inline", "focused"):
            for initial_size in ((24, 100), (12, 40)):
                pending = PendingLogs()
                size = [initial_size]
                keys = iter(
                    (
                        ord("j") if mode == "inline" else ord("l"),
                        curses.KEY_RESIZE,
                        ord("?"),
                        ord("p"),
                        ord("q"),
                        ord("q"),
                    )
                )

                def key():
                    value = next(keys)
                    if value == curses.KEY_RESIZE:
                        size[0] = (12, 40) if size[0][1] >= 65 else (24, 100)
                    return value

                with self.subTest(mode=mode, initial_size=initial_size):
                    frames = self.dashboard(
                        key,
                        dimensions=lambda: size[0],
                        log_submit=pending,
                    )
                    self.assertEqual(len(frames), 6)
                    self.assertTrue(all(frames))
                    self.assertIn("Loading log", frame_text(frames[1]))
                    self.assertIn("Loading log", frame_text(frames[2]))
                    self.assertIn("BUILDR HELP", frame_text(frames[3]))
                    self.assertIn("BUILDR HELP", frame_text(frames[4]))
                    self.assertIn("Loading log", frame_text(frames[-1]))
                    if mode == "inline":
                        self.assertIn(self.jobs[1], frame_text(frames[1]))
                    else:
                        self.assertIn("LOG LIVE", frame_text(frames[1]))
                    self.assertEqual(len(pending.calls), 1)
                    self.assertFalse(pending.calls[0][2].done())
                    self.assertEqual(self.fixture.prompt_calls, [])

    def test_late_log_results_and_errors_do_not_cross_job_or_aba_selection(self):
        for dimensions in ((24, 100), (12, 40)):
            for return_to_first in (False, True):
                for fail in (False, True):
                    pending = PendingLogs()
                    first_log = self.store.log_path(self.jobs[0])
                    first_log.write_text("OBSOLETE_A_OUTPUT\n")
                    target = self.jobs[0] if return_to_first else self.jobs[1]
                    self.store.log_path(self.jobs[1]).write_text("CURRENT_JOB_OUTPUT\n")
                    keys = iter(
                        [ord("j")]
                        + ([ord("k")] if return_to_first else [])
                        + [-1, -1, ord("q")]
                    )
                    finishes = [0]

                    def key():
                        value = next(keys)
                        if value == -1:
                            if finishes[0] == 0:
                                pending.finish(
                                    0,
                                    OSError("obsolete read") if fail else None,
                                )
                                first_log.write_text("CURRENT_JOB_OUTPUT\n")
                            else:
                                self.assertEqual(len(pending.calls), 2)
                                pending.finish(1)
                            finishes[0] += 1
                        return value

                    with self.subTest(
                        dimensions=dimensions, aba=return_to_first, fail=fail
                    ):
                        frames = self.dashboard(key, dimensions, log_submit=pending)
                        self.assertEqual(len(pending.calls), 2)
                        first_request = pending.calls[0][1]
                        current_request = pending.calls[1][1]
                        self.assertEqual(current_request.key[1], target)
                        self.assertGreater(current_request.epoch, first_request.epoch)
                        for frame in frames:
                            self.assertNotIn("OBSOLETE_A_OUTPUT", frame_text(frame))
                            self.assertNotIn("Cannot read log", frame_text(frame))
                        self.assertIn("CURRENT_JOB_OUTPUT", frame_text(frames[-1]))
                        self.assertIn(target, frame_text(frames[-1]))

    def test_hold_and_search_reject_pending_output_until_end_requests_a_new_read(self):
        for dimensions in ((24, 100), (12, 40)):
            for action in (ord(" "), ord("/")):
                for fail in (False, True):
                    path = self.store.log_path(self.jobs[0])
                    path.write_text("BASE needle output\n")
                    pending, focused_calls = PendingLogs(), []
                    now, idle_keys = [100], [0]
                    keys = iter(
                        (ord("l"), -1, action, -1, curses.KEY_END, -1, ord("q"))
                    )

                    def submit(loader, request):
                        if request.key[0] == "focused":
                            focused_calls.append(request)
                            if len(focused_calls) > 1:
                                return pending(loader, request)
                        return completed(loader, request)

                    def key():
                        value = next(keys)
                        if value == -1:
                            idle_keys[0] += 1
                            if idle_keys[0] == 1:
                                now[0] = 101
                            elif idle_keys[0] == 2:
                                path.write_text("OBSOLETE_HELD_OUTPUT\n")
                                pending.finish(
                                    0,
                                    OSError("obsolete held read") if fail else None,
                                )
                            else:
                                self.assertEqual(len(pending.calls), 2)
                                path.write_text("LIVE_AFTER_END\n")
                                pending.finish(1)
                        return value

                    with self.subTest(dimensions=dimensions, action=action, fail=fail):
                        frames = self.dashboard(
                            key,
                            dimensions,
                            log_submit=submit,
                            clock=lambda: now[0],
                            query="needle",
                        )
                        for frame in frames[3:5]:
                            text = frame_text(frame)
                            self.assertIn("HELD", text)
                            self.assertIn("BASE needle output", text)
                            self.assertNotIn("OBSOLETE_HELD_OUTPUT", text)
                            self.assertNotIn("Cannot read log", text)
                        self.assertEqual(len(focused_calls), 3)
                        self.assertGreater(
                            focused_calls[-1].epoch, focused_calls[-2].epoch
                        )
                        self.assertIn("LOG LIVE", frame_text(frames[-1]))
                        self.assertIn("LIVE_AFTER_END", frame_text(frames[-1]))
                        if action == ord("/"):
                            self.assertEqual(len(self.fixture.prompt_calls), 1)

    def test_help_expanded_jobs_and_other_views_do_not_submit_log_reads(self):
        for dimensions in ((24, 100), (12, 40)):
            requests, now = [], [100]
            steps = iter(
                (
                    (9, 1),
                    (-1, 1),
                    (ord("?"), 1),
                    (-1, 1),
                    (ord("q"), 1),
                    (-1, 1),
                    (9, 1),
                    (-1, 2),
                    (ord("V"), 3),
                    (-1, 3),
                    (ord("q"), 3),
                )
            )

            def submit(loader, request):
                requests.append(request)
                return completed(loader, request)

            def key():
                value, expected = next(steps)
                self.assertEqual(len(requests), expected)
                now[0] += 1
                return value

            with self.subTest(dimensions=dimensions), mock.patch(
                "agent_builds.tui.choose_view", return_value=3
            ):
                frames = self.dashboard(
                    key,
                    dimensions,
                    log_submit=submit,
                    clock=lambda: now[0],
                )
                self.assertEqual(len(requests), 3)
                self.assertTrue(all(request.key[0] == "inline" for request in requests))
                self.assertIn("DETAILS", frame_text(frames[-1]))

    def test_end_discards_read_started_before_hold_even_after_follow_resumes(self):
        for dimensions in ((24, 100), (12, 40)):
            for fail in (False, True):
                path = self.store.log_path(self.jobs[0])
                path.write_text("ACCEPTED_BEFORE_HOLD\n")
                pending, focused_calls = PendingLogs(), []
                now, idle_keys = [100], [0]
                keys = iter((ord("l"), -1, ord(" "), curses.KEY_END, -1, -1, ord("q")))

                def submit(loader, request):
                    if request.key[0] == "focused":
                        focused_calls.append(request)
                        if len(focused_calls) > 1:
                            return pending(loader, request)
                    return completed(loader, request)

                def key():
                    value = next(keys)
                    if value == -1:
                        idle_keys[0] += 1
                        if idle_keys[0] == 1:
                            now[0] = 106
                        elif idle_keys[0] == 2:
                            path.write_text("OBSOLETE_BEFORE_HOLD\n")
                            pending.finish(
                                0, OSError("obsolete read") if fail else None
                            )
                        else:
                            self.assertEqual(len(pending.calls), 2)
                            path.write_text("FRESH_AFTER_END\n")
                            pending.finish(1)
                    return value

                with self.subTest(dimensions=dimensions, fail=fail):
                    frames = self.dashboard(
                        key,
                        dimensions,
                        log_submit=submit,
                        clock=lambda: now[0],
                    )
                    self.assertIn("HELD", frame_text(frames[3]))
                    self.assertIn("LOG LIVE", frame_text(frames[4]))
                    for frame in frames[3:6]:
                        self.assertIn("ACCEPTED_BEFORE_HOLD", frame_text(frame))
                        self.assertNotIn("Cannot read log", frame_text(frame))
                    self.assertTrue(
                        all(
                            "OBSOLETE_BEFORE_HOLD" not in frame_text(frame)
                            for frame in frames
                        )
                    )
                    self.assertEqual(len(focused_calls), 3)
                    self.assertTrue(
                        all(request.force_content for request in focused_calls)
                    )
                    self.assertIn("FRESH_AFTER_END", frame_text(frames[-1]))

    def test_current_log_error_is_visible_and_retries_are_throttled(self):
        for mode in ("inline", "focused"):
            for dimensions in ((24, 100), (12, 40)):
                errors = []

                def submit(loader, request):
                    if request.key[0] == mode:
                        errors.append(request)
                        future = Future()
                        future.set_exception(OSError("fixture log unavailable"))
                        return future
                    return completed(loader, request)

                keys = ([ord("l")] if mode == "focused" else []) + [
                    -1,
                    -1,
                    ord("q"),
                ]
                with self.subTest(mode=mode, dimensions=dimensions):
                    frames = self.dashboard(keys, dimensions, log_submit=submit)
                    self.assertEqual(len(errors), 1)
                    self.assertIn("Cannot read log (OSError)", frame_text(frames[-1]))
                    self.assertNotIn("Traceback", frame_text(frames[-1]))

    @unittest.skipUnless(os.name == "posix", "requires a private pseudo-terminal")
    def test_native_tui_quits_with_a_stalled_log_worker_after_search_and_resize(self):
        import errno
        import fcntl
        import pty
        import selectors
        import signal
        import struct
        import termios
        import time

        from tui_terminal import TerminalScreen

        ready_path = self.root / "log-read-started"
        script = textwrap.dedent(
            """
            from pathlib import Path
            import sys
            from threading import Event
            from unittest import mock
            from agent_builds import tui
            from agent_builds.tui_reads import submit_read

            blocked = Event()
            def stalled(request):
                Path(sys.argv[2]).write_text('started')
                blocked.wait()

            def submit(loader, request):
                return submit_read(stalled, request)

            with mock.patch('agent_builds.tui.submit_log_read', side_effect=submit):
                tui.launch(sys.argv[1])
            assert not blocked.is_set()
            """
        )
        master, slave = pty.openpty()
        selector = selectors.DefaultSelector()
        child = None
        terminal = TerminalScreen(20, 100)
        env = dict(
            os.environ, AGENT_BUILDS_STATE=str(self.store.root), XDG_CONFIG_HOME=str(Path(self.store.root).parent / "user-config"), TERM="xterm-256color"
        )
        env.pop("AGENT_BUILDS_JOB_ID", None)
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 20, 100, 0, 0))
            child = subprocess.Popen(
                [sys.executable, "-c", script, str(self.root), str(ready_path)],
                cwd=str(Path(__file__).resolve().parent.parent),
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=env,
                start_new_session=True,
            )
            os.close(slave)
            slave = None
            selector.register(master, selectors.EVENT_READ)

            def wait_for(needle, require_ready=False):
                deadline = time.monotonic() + 5
                output = b""
                while time.monotonic() < deadline:
                    for unused, events in selector.select(0.05):
                        try:
                            data = os.read(master, 65536)
                        except OSError as error:
                            if error.errno == errno.EIO:
                                data = b""
                            else:
                                raise
                        output += data
                        terminal.feed(data)
                    if needle in terminal.text() and (
                        not require_ready or ready_path.exists()
                    ):
                        return
                    if child.poll() is not None:
                        break
                self.fail(
                    "Missing {!r}; screen={!r}; raw={!r}".format(
                        needle, terminal.text(), output[-2000:]
                    )
                )

            wait_for("Loading log", require_ready=True)
            os.write(master, b"l")
            wait_for("LOG LIVE")
            os.write(master, b"/")
            wait_for("Find in loaded log")
            os.write(master, b"needle\n")
            wait_for("Find 0/0: needle")
            wait_for("HELD")
            terminal.resize(12, 40)
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 12, 40, 0, 0))
            os.kill(child.pid, signal.SIGWINCH)
            os.write(master, b"q")
            self.assertEqual(child.wait(timeout=5), 0)
            self.assertEqual(ready_path.read_text(), "started")
        finally:
            selector.close()
            if child is not None and child.poll() is None:
                child.kill()
                child.wait(timeout=5)
            if slave is not None:
                os.close(slave)
            os.close(master)
