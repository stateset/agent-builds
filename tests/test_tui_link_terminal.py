"""Real terminal navigation stays usable while a linked-job reader is stalled."""

import errno
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

from agent_builds import external
from agent_builds.store import Store


@unittest.skipUnless(os.name == "posix", "requires a private pseudo-terminal")
class LinkedLogTerminalTests(unittest.TestCase):
    def test_stalled_link_can_show_help_resize_cancel_and_quit_without_writes(self):
        self.check_stalled_link("watch")

    def test_stalled_direct_jump_keeps_prompt_help_resize_and_quit_usable(self):
        self.check_stalled_link("jump")

    def check_stalled_link(self, route):
        import fcntl
        import json
        import pty
        import struct
        import termios

        from tui_terminal import TerminalScreen

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "state")
            self.addCleanup(store.close)
            store.update_config(dict(min_free_gb=0, min_available_gb=0, nice=0))
            env = dict(
                os.environ,
                AGENT_BUILDS_STATE=str(store.root),
                XDG_CONFIG_HOME=str(Path(store.root).parent / "user-config"),
                TERM="xterm-256color",
            )
            env.pop("AGENT_BUILDS_JOB_ID", None)
            # Persist one watch and its probe intent. Neither API starts a
            # controller or a native command; the fixture job stays queued.
            with mock.patch.dict(os.environ, env, clear=True):
                watch = external.create(
                    store,
                    dict(
                        name="terminal-link-fixture",
                        cwd=str(root),
                        command=["true"],
                        read_only_probe=True,
                        external_ref="fixture:terminal-link",
                        resource_key="terminal-link-fixture",
                        track_source=False,
                    ),
                )
                probe = external.prepare(store, watch)
            self.assertIsNotNone(probe)
            job_id = probe["job_id"]

            def snapshot():
                tables = (
                    "jobs",
                    "plans",
                    "plan_stages",
                    "external_watches",
                    "external_probes",
                )
                return (
                    {
                        table: [
                            tuple(row)
                            for row in store.db.execute(
                                "SELECT * FROM " + table + " ORDER BY rowid"
                            )
                        ]
                        for table in tables
                    },
                    store.config_path.read_bytes(),
                )

            before = snapshot()
            started = root / "link-reader-started.json"
            script = textwrap.dedent(
                """
                import contextlib
                import json
                from pathlib import Path
                import sys
                from threading import Event
                from unittest import mock

                from agent_builds import tui
                from agent_builds.store import Store
                from agent_builds.tui_data import LazyStore
                from agent_builds.tui_reads import submit_read
                from agent_builds.tui_submissions import SubmissionTracker

                blocked = Event()
                def stalled(request):
                    ready = Path(sys.argv[2])
                    temporary = ready.with_suffix('.tmp')
                    temporary.write_text(json.dumps(request))
                    temporary.replace(ready)
                    blocked.wait()
                    raise AssertionError('fixture worker was released')

                def submit(loader, request):
                    return submit_read(stalled, request)

                def forbidden(*args, **kwargs):
                    raise AssertionError('terminal inspection attempted a write or command')

                with contextlib.ExitStack() as guards:
                    guards.enter_context(mock.patch.object(
                        LazyStore, 'writable', side_effect=forbidden))
                    guards.enter_context(mock.patch.object(
                        SubmissionTracker, 'start', side_effect=forbidden))
                    for name in ('_ensure_schema', 'recover', 'submit'):
                        guards.enter_context(mock.patch.object(
                            Store, name, side_effect=forbidden))
                    guards.enter_context(mock.patch(
                        'subprocess.Popen', side_effect=forbidden))
                    guards.enter_context(mock.patch(
                        'agent_builds.tui.submit_link_read', side_effect=submit))
                    tui.launch(sys.argv[1])
                assert not blocked.is_set()
                """
            )
            master, slave = pty.openpty()
            selector = selectors.DefaultSelector()
            child = None
            terminal = TerminalScreen(20, 100)
            try:
                fcntl.ioctl(
                    slave, termios.TIOCSWINSZ, struct.pack("HHHH", 20, 100, 0, 0)
                )
                child = subprocess.Popen(
                    [sys.executable, "-c", script, str(root), str(started)],
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

                def wait_for(predicate, description):
                    deadline = time.monotonic() + 20
                    while time.monotonic() < deadline:
                        for unused, events in selector.select(0.05):
                            try:
                                data = os.read(master, 65536)
                            except OSError as error:
                                if error.errno != errno.EIO:
                                    raise
                                data = b""
                            terminal.feed(data)
                        if predicate(terminal.text()):
                            return
                        if child.poll() is not None:
                            break
                    self.fail(
                        "Missing {}; child={}; rendered screen={!r}".format(
                            description, child.poll(), terminal.text()
                        )
                    )

                def see(text):
                    wait_for(lambda screen: text in screen, repr(text))

                see("BUILDR")
                if route == "jump":
                    os.write(master, b"J")
                    see("Open log: full job ID")
                    self.assertFalse(started.exists())
                    os.write(master, job_id.encode("ascii") + b"\n")
                else:
                    os.write(master, b"V")
                    see("BUILDR VIEWS")
                    os.write(master, b"External\n")
                    see("terminal-link-fixture")
                    self.assertFalse(started.exists())
                    os.write(master, b"\n")
                origin_heading = "JOBS" if route == "jump" else "EXTERNAL"
                wait_for(
                    lambda screen: (
                        "Opening log " + job_id in screen and started.exists()
                    ),
                    "pending linked log with its actual worker started",
                )
                request = json.loads(started.read_text())
                self.assertEqual(request[1:], ["browser", job_id])
                os.write(master, b"?")
                see("BUILDR HELP")
                if route == "jump":
                    os.write(master, b"/")
                    see("Find help (blank clears)")
                    os.write(master, b"blocker\n")
                    see("Find: blocker")
                os.write(master, b"q")
                see("Opening log " + job_id)
                terminal.resize(8, 20)
                fcntl.ioctl(
                    master, termios.TIOCSWINSZ, struct.pack("HHHH", 8, 20, 0, 0)
                )
                os.kill(child.pid, signal.SIGWINCH)
                wait_for(
                    lambda screen: origin_heading in screen and "Opening log" in screen,
                    "compact watch view and pending notice after resize",
                )
                if route == "jump":
                    os.write(master, b"?")
                    see("Find: blocker")
                    os.write(master, b"/")
                    see("BUILDR INPUT")
                    os.write(master, b"\x15missing-help-text\n")
                    see("No matching help")
                    os.write(master, b"q")
                    see("Opening log")
                # Navigation cancels only the open intent, not its daemon read.
                os.write(master, b"j")
                wait_for(
                    lambda screen: (
                        origin_heading in screen and "Opening log" not in screen
                    ),
                    "cancelled linked-log intent in the compact watch view",
                )
                os.write(master, b"q")
                self.assertEqual(child.wait(timeout=8), 0)
                self.assertEqual(json.loads(started.read_text()), request)
                self.assertEqual(snapshot(), before)
            finally:
                selector.close()
                if child is not None and child.poll() is None:
                    # Popen still owns this unreaped session leader. Signal
                    # only its private process group, never a real agent pane.
                    try:
                        if os.getpgid(child.pid) == child.pid:
                            os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    child.wait(timeout=8)
                if slave is not None:
                    os.close(slave)
                os.close(master)
                store.close()
