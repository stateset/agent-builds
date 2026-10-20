"""Real dashboard input confirms one private submission without owning its lifetime."""

import errno
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import tempfile
from threading import Event
import time
import unittest

from agent_builds.store import Store, TERMINAL
from tui_terminal import TerminalScreen


def process_identity(pid):
    try:
        raw = Path("/proc/{}/stat".format(pid)).read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    fields = raw[raw.rfind(")") + 2 :].split()
    return dict(pid=pid, start=fields[19], state=fields[0])


def identity_is_live(record):
    current = process_identity(record["pid"])
    return bool(
        current
        and current["start"] == record["start"]
        and current["state"] not in ("Z", "X")
    )


def kill_owned_group(record):
    if not record or not identity_is_live(record):
        return
    try:
        if os.getpgid(record["pid"]) == record["pid"]:
            os.killpg(record["pid"], signal.SIGKILL)
        else:
            os.kill(record["pid"], signal.SIGKILL)
    except ProcessLookupError:
        pass


@unittest.skipUnless(
    os.name == "posix" and Path("/proc").is_dir(),
    "requires Linux PTY and process identities",
)
class TuiSubmissionTerminalTests(unittest.TestCase):
    def test_real_new_job_receipt_and_history_survive_dashboard_quit(self):
        import fcntl
        import pty
        import selectors
        import struct
        import termios

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkout, state = root / "checkout", root / "state"
            checkout.mkdir()
            store = Store(state)
            store.update_config(
                dict(
                    cpu_slots=2,
                    memory_gb=4,
                    max_running=2,
                    max_heavy=1,
                    quick_reserve_slots=0,
                    quick_reserve_jobs=0,
                    quick_reserve_memory_gb=0,
                    quick_jobs=1,
                    heavy_jobs=1,
                    min_free_gb=0,
                    min_available_gb=0,
                    nice=0,
                    timeout_seconds=40,
                )
            )
            marker, release = root / "native-started.json", root / "release-native"
            wait_loop = "while not release.exists() and time.monotonic() < deadline:\n    time.sleep(0.02)"
            native_code = ";".join(
                (
                    "import json,os,sys,time",
                    "from pathlib import Path",
                    "pid=os.getpid()",
                    "raw=Path('/proc/{}/stat'.format(pid)).read_text()",
                    "start=raw[raw.rfind(')')+2:].split()[19]",
                    "marker=Path(sys.argv[1])",
                    "temporary=marker.with_suffix('.tmp')",
                    "temporary.write_text(json.dumps(dict(pid=pid,start=start,job_id=os.environ.get('AGENT_BUILDS_JOB_ID'))))",
                    "temporary.replace(marker)",
                    "print('receipt log fixture',flush=True)",
                    "release=Path(sys.argv[2])",
                    "deadline=time.monotonic()+30",
                    "exec({!r})".format(wait_loop),
                    "time.sleep(1.5)",
                    "sys.exit(7)",
                )
            )
            native_argv = [sys.executable, "-c", native_code, str(marker), str(release)]
            command = shlex.join(native_argv)
            self.assertNotIn("\n", command)
            env = dict(os.environ, AGENT_BUILDS_STATE=str(state), XDG_CONFIG_HOME=str(Path(state).parent / "user-config"), TERM="xterm-256color")
            env.pop("AGENT_BUILDS_JOB_ID", None)
            env.pop("HERDR_ENV", None)
            env.pop("HERDR_PANE_ID", None)
            env.pop("CARGO_TARGET_DIR", None)
            master, slave = pty.openpty()
            selector = selectors.DefaultSelector()
            terminal = TerminalScreen(30, 200)
            child, ui_identity, completed = None, None, False
            raw_output = bytearray()
            deadline = time.monotonic() + 20
            pending = Event()

            def jobs():
                return list(store.db.execute("SELECT * FROM jobs ORDER BY created,id"))

            def pump():
                for unused, events in selector.select(0.05):
                    try:
                        data = os.read(master, 65536)
                    except OSError as error:
                        if error.errno == errno.EIO:
                            data = b""
                        else:
                            raise
                    raw_output.extend(data)
                    terminal.feed(data)

            def wait_screen(predicate, description):
                while time.monotonic() < deadline:
                    pump()
                    text = terminal.text()
                    if predicate(text):
                        return text
                    if child.poll() is not None:
                        break
                self.fail(
                    "Missing {}; rendered={!r}; raw={!r}".format(
                        description, terminal.text(), bytes(raw_output[-3000:])
                    )
                )

            def wait_condition(predicate, description):
                while time.monotonic() < deadline:
                    if predicate():
                        return
                    pending.wait(0.02)
                self.fail("Timed out waiting for " + description)

            def answer(heading, value):
                wait_screen(
                    lambda text: "BUILDR INPUT" in text and heading in text, heading
                )
                os.write(master, b"\x15" + value.encode("utf-8") + b"\n")

            try:
                fcntl.ioctl(
                    slave, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 200, 0, 0)
                )
                child = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "agent_builds",
                        "tui",
                        "--cwd",
                        str(checkout),
                    ],
                    cwd=str(Path(__file__).resolve().parent.parent),
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    env=env,
                    start_new_session=True,
                )
                ui_identity = process_identity(child.pid)
                os.close(slave)
                slave = None
                selector.register(master, selectors.EVENT_READ)
                wait_screen(
                    lambda text: "JOBS" in text and "No jobs yet" in text,
                    "empty jobs view",
                )
                os.write(master, b"n")
                answer("Working directory", str(checkout))
                answer("Command (arguments; no implicit shell)", command)
                answer("Lane: quick or heavy", "quick")
                answer("Backend: local or docker", "local")
                text = wait_screen(
                    lambda rendered: re.search(
                        r"Submitted job ([0-9a-f]{12})\b", rendered
                    ),
                    "confirmed submission handle",
                )
                handle = re.search(r"Submitted job ([0-9a-f]{12})\b", text).group(1)
                wait_condition(marker.exists, "native fixture startup")
                native = json.loads(marker.read_text())
                self.assertEqual(native["job_id"], handle)
                self.assertTrue(identity_is_live(native))
                self.assertEqual(len(jobs()), 1)
                receipt_paths = list(state.glob("receipt-tui-*.json"))
                journal_paths = list(state.glob("attempt-tui-*.json"))
                self.assertEqual(len(receipt_paths), 1)
                self.assertEqual(len(journal_paths), 1)
                receipt_path, journal_path = receipt_paths[0], journal_paths[0]
                receipt = json.loads(receipt_path.read_text())
                self.assertEqual(receipt["job_id"], handle)
                self.assertEqual(receipt["cwd"], str(checkout))
                self.assertEqual(
                    receipt["log_path"], str(state / "logs" / (handle + ".log"))
                )
                journal = json.loads(journal_path.read_text())
                self.assertEqual(
                    journal["argv"][journal["argv"].index("--") + 1 :], native_argv
                )
                self.assertEqual(
                    journal["key"], receipt_path.stem[len("receipt-tui-") :]
                )
                os.write(master, b"U")
                wait_screen(
                    lambda rendered: (
                        "SUBMISSIONS" in rendered
                        and "Submitted job " + handle in rendered
                        and "Receipt: " + str(receipt_path) in rendered
                    ),
                    "submission history and durable receipt path",
                )
                self.assertTrue(identity_is_live(native))
                os.write(master, b"\t")
                wait_screen(
                    lambda rendered: (
                        "SUBMISSIONS 1/1" in rendered and "Enter " + handle in rendered
                    ),
                    "selected confirmed receipt",
                )
                os.write(master, b"\n")
                wait_screen(
                    lambda rendered: (
                        "LOG LIVE" in rendered
                        and handle + " running" in rendered
                        and any(
                            line.strip() == "receipt log fixture"
                            for line in rendered.splitlines()
                        )
                    ),
                    "exact receipt job log",
                )
                os.write(master, b"b")
                wait_screen(
                    lambda rendered: "SUBMISSIONS 1/1" in rendered,
                    "retained receipt selection",
                )
                terminal.resize(8, 20)
                fcntl.ioctl(
                    master, termios.TIOCSWINSZ, struct.pack("HHHH", 8, 20, 0, 0)
                )
                os.kill(child.pid, signal.SIGWINCH)
                wait_screen(
                    lambda rendered: (
                        "SUBMISSIONS 1/1" in rendered and "Enter " + handle in rendered
                    ),
                    "selected receipt in minimum pane",
                )
                os.write(master, b"\n")
                wait_screen(
                    lambda rendered: "LOG LIVE" in rendered, "narrow receipt log"
                )
                os.write(master, b"b")
                wait_screen(
                    lambda rendered: "SUBMISSIONS 1/1" in rendered,
                    "back to selected receipt in minimum pane",
                )
                os.write(master, b"q")
                self.assertEqual(
                    child.wait(timeout=max(0.1, deadline - time.monotonic())), 0
                )
                self.assertTrue(
                    identity_is_live(native), "native job stopped with dashboard"
                )
                row = jobs()[0]
                self.assertEqual(row["id"], handle)
                self.assertEqual(row["state"], "running")
                self.assertFalse(row["cancel_requested"])
                selector.unregister(master)
                release.write_text("finish after dashboard exit")
                wait_condition(
                    lambda: jobs()[0]["state"] in TERMINAL, "native exit after UI quit"
                )
                row = jobs()[0]
                self.assertEqual(row["state"], "failed")
                self.assertEqual(row["exit_code"], 7)
                self.assertFalse(row["cancel_requested"])
                self.assertEqual(json.loads(row["command"]), native_argv)
                self.assertEqual(len(jobs()), 1)
                wait_condition(
                    lambda: json.loads(journal_path.read_text())["state"] == "finished",
                    "submission helper journal completion",
                )
                self.assertEqual(json.loads(journal_path.read_text())["returncode"], 0)
                self.assertEqual(len(list(state.glob("receipt-tui-*.json"))), 1)
                self.assertEqual(len(list(state.glob("attempt-tui-*.json"))), 1)
                self.assertTrue(
                    (
                        state / "logs" / ("submission-tui-" + journal["key"] + ".log")
                    ).is_file()
                )
                wait_condition(
                    lambda: (
                        not identity_is_live(
                            dict(pid=row["owner_pid"], start=row["owner_start"])
                        )
                    ),
                    "private runner shutdown",
                )
                completed = True
            finally:
                if not completed:
                    release.write_text("failure cleanup")
                    for row in jobs():
                        if row["child_pid"] and row["child_start"]:
                            kill_owned_group(
                                dict(pid=row["child_pid"], start=row["child_start"])
                            )
                        if row["owner_pid"] and row["owner_start"]:
                            kill_owned_group(
                                dict(pid=row["owner_pid"], start=row["owner_start"])
                            )
                    # Helpers are identified by this unique state root and their
                    # exact module argv; unrelated Python processes are excluded.
                    for path in Path("/proc").iterdir():
                        if not path.name.isdigit():
                            continue
                        helper_identity = process_identity(int(path.name))
                        if helper_identity is None:
                            continue
                        try:
                            argv = (path / "cmdline").read_bytes().split(b"\0")
                        except (FileNotFoundError, ProcessLookupError, PermissionError):
                            continue
                        if (
                            len(argv) >= 5
                            and argv[1:3] == [b"-m", b"agent_builds.tui_submissions"]
                            and argv[3] == os.fsencode(state)
                        ):
                            kill_owned_group(helper_identity)
                    kill_owned_group(ui_identity)
                if child is not None and child.poll() is None:
                    child.kill()
                    child.wait(timeout=2)
                selector.close()
                if slave is not None:
                    os.close(slave)
                os.close(master)
                store.close()
