"""Paired native-terminal observations; never use real agent panes or execute jobs."""

import argparse
import errno
import fcntl
import json
import math
import os
from pathlib import Path
import pty
import selectors
import signal
import statistics
import struct
import subprocess
import sys
import tempfile
import termios
import textwrap
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from agent_builds.experiments import parent_budget
from agent_builds.plans import write_export
from tui_terminal import TerminalScreen


def identity(root):
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True, timeout=10
    ).strip()
    assert not subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=root, text=True, timeout=10
    ).strip()
    return head


def pressure():
    result = {}
    for name in ("cpu", "io", "memory"):
        try:
            result[name] = Path("/proc/pressure/" + name).read_text()
        except OSError as error:
            result[name] = dict(unavailable=type(error).__name__)
    return result


def measure(root, expected, mode, pair):
    assert identity(root) == expected
    with tempfile.TemporaryDirectory(prefix="buildr-native-ui-study-") as directory:
        fixture = Path(directory)
        state, marker, counters = (
            fixture / "missing-state",
            fixture / "read-started",
            fixture / "counters.json",
        )
        code = textwrap.dedent("""
            import contextlib
            import json
            from pathlib import Path
            import runpy
            import sys
            from threading import Event
            import textwrap
            from unittest.mock import patch
            from agent_builds import tui
            from agent_builds.store import Store
            from agent_builds.tui_data import LazyStore

            blocked = Event()
            marker_path = Path(sys.argv[1])
            def stalled(*args):
                marker_path.write_text('started')
                blocked.wait()
                raise AssertionError('Read unexpectedly released')
            calls = [0]
            sizes = set()
            painting = tui.paint
            def painted(screen, y, x, text, *args):
                if str(text).startswith('BUILDR HELP'):
                    sizes.add(screen.getmaxyx())
                return painting(screen, y, x, text, *args)
            wrapping = textwrap.wrap
            def wrapped(text, *args, **kwargs):
                if text in tui.HELP:
                    calls[0] += 1
                return wrapping(text, *args, **kwargs)
            def forbidden(*args, **kwargs):
                raise AssertionError('Unexpected command or queue write')
            output = Path(sys.argv[2])
            sys.argv = ['buildr', 'tui', '--cwd', sys.argv[3]]
            try:
                with contextlib.ExitStack() as guards:
                    guards.enter_context(patch.object(tui, 'read_browser', side_effect=stalled))
                    guards.enter_context(patch.object(tui, 'paint', side_effect=painted))
                    guards.enter_context(patch.object(textwrap, 'wrap', side_effect=wrapped))
                    guards.enter_context(patch.object(LazyStore, 'writable', side_effect=forbidden))
                    for name in ('_ensure_schema', 'submit', 'recover'):
                        guards.enter_context(patch.object(Store, name, side_effect=forbidden))
                    guards.enter_context(patch('subprocess.Popen', side_effect=forbidden))
                    runpy.run_module('agent_builds', run_name='__main__')
            finally:
                output.write_text(json.dumps({'help_wrap_calls': calls[0], 'read_still_stalled': not blocked.is_set(), 'observed_help_sizes': sorted(sizes)}))
        """)
        env = dict(
            os.environ,
            TERM="xterm-256color",
            PYTHONPATH=str(root),
            AGENT_BUILDS_STATE=str(state),
        )
        for name in (
            "AGENT_BUILDS_JOB_ID",
            "HERDR_ENV",
            "HERDR_PANE_ID",
            "HERDR_TAB_ID",
            "HERDR_WORKSPACE_ID",
        ):
            env.pop(name, None)
        master, slave = pty.openpty()
        selector = selectors.DefaultSelector()
        screen = TerminalScreen(24, 100)
        child = None
        observations = []
        before = pressure()
        try:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
            child = subprocess.Popen(
                [sys.executable, "-c", code, str(marker), str(counters), str(fixture)],
                cwd=fixture,
                env=env,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
            )
            os.close(slave)
            slave = None
            selector.register(master, selectors.EVENT_READ)

            def drain():
                for unused, events in selector.select(0.01):
                    try:
                        data = os.read(master, 65536)
                    except OSError as error:
                        if error.errno != errno.EIO:
                            raise
                        data = b""
                    screen.feed(data)

            def wait_for(predicate):
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    drain()
                    if predicate(screen.text()):
                        return
                    if child.poll() is not None:
                        break
                raise AssertionError("Private terminal did not reach expected state")

            wait_for(lambda text: "Loading queue snapshot" in text and marker.exists())

            def action(key, predicate, label):
                started = time.monotonic()
                os.write(master, key)
                wait_for(predicate)
                observations.append(
                    dict(action=label, seconds=time.monotonic() - started)
                )

            for width, height in ((100, 24), (20, 8)):
                if width == 20:
                    screen.resize(height, width)
                    fcntl.ioctl(
                        master,
                        termios.TIOCSWINSZ,
                        struct.pack("HHHH", height, width, 0, 0),
                    )
                    os.kill(child.pid, signal.SIGWINCH)
                    # Consume the resize repaint before timing another key.
                    until = time.monotonic() + 0.4
                    while time.monotonic() < until:
                        drain()
                for unused in range(5):
                    action(
                        b"?",
                        lambda text: "BUILDR HELP" in text,
                        "open-help-{}".format(width),
                    )
                    action(
                        b"q",
                        lambda text: "BUILDR HELP" not in text,
                        "close-help-{}".format(width),
                    )
            action(b"?", lambda text: "BUILDR HELP" in text, "open-help-idle")
            idle = time.monotonic()
            while time.monotonic() - idle < 2:
                drain()
            action(b"q", lambda text: "BUILDR HELP" not in text, "close-help-idle")
            started = time.monotonic()
            os.write(master, b"q")
            assert child.wait(timeout=8) == 0
            quit_seconds = time.monotonic() - started
            counts = json.loads(counters.read_text())
            assert counts["read_still_stalled"] and not state.exists()
            assert counts["observed_help_sizes"] == [[8, 20], [24, 100]]
            assert identity(root) == expected
            timings = sorted(row["seconds"] for row in observations)
            return dict(
                mode=mode,
                pair=pair,
                source=expected,
                samples=observations,
                median_seconds=statistics.median(timings),
                p95_seconds=timings[math.ceil(0.95 * len(timings)) - 1],
                p95_method="nearest-rank",
                sample_count=len(timings),
                max_seconds=max(timings),
                quit_seconds=quit_seconds,
                **counts,
                state_remained_missing=True,
                native_exit=0,
                pressure_before=before,
                pressure_after=pressure(),
            )
        finally:
            if child is not None and child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait(timeout=8)
            selector.close()
            os.close(master)
            if slave is not None:
                os.close(slave)


def run(baseline, output):
    budget = parent_budget(1)
    baseline = Path(baseline).expanduser().resolve()
    output = Path(output).expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("Use a new report path outside both checkouts")
    output = output.resolve()
    roots = {"baseline": baseline, "candidate": ROOT.resolve()}
    if baseline == roots["candidate"]:
        raise ValueError("Baseline must be a separate checkout")
    if (
        output.exists()
        or output.is_symlink()
        or any(root == output or root in output.parents for root in roots.values())
    ):
        raise ValueError("Use a new report path outside both checkouts")
    expected = {mode: identity(root) for mode, root in roots.items()}
    for root in roots.values():
        if not (root / "agent_builds/tui.py").is_file():
            raise ValueError("Both checkouts must contain Buildr source")
    result = dict(
        schema="buildr-native-ui-study-v1",
        result="incomplete",
        sources={
            mode: dict(checkout=str(root), commit=expected[mode])
            for mode, root in roots.items()
        },
        outer_reservation=budget,
        python_version=sys.version,
        records=[],
        source_changed=None,
        note="Paired owned PTYs at 100x24 and 20x8 with a real stalled daemon read and missing private state. Timings include local scheduling and terminal parser costs; these observations do not prove real agent productivity or a latency guarantee. No native jobs or real agent panes were used.",
    )
    try:
        for pair, modes in enumerate(
            (("baseline", "candidate"), ("candidate", "baseline"))
        ):
            for mode in modes:
                result["current_arm"] = dict(mode=mode, pair=pair)
                record = measure(roots[mode], expected[mode], mode, pair)
                result["records"].append(record)
                print(
                    json.dumps(
                        {
                            k: record[k]
                            for k in (
                                "mode",
                                "pair",
                                "median_seconds",
                                "p95_seconds",
                                "max_seconds",
                                "help_wrap_calls",
                                "quit_seconds",
                            )
                        }
                    ),
                    flush=True,
                )
        result["source_changed"] = any(
            identity(root) != expected[mode] for mode, root in roots.items()
        )
        if result["source_changed"]:
            raise ValueError("Source changed during the study")
        result["result"] = "passed"
        result.pop("current_arm", None)
    except BaseException as error:
        result["result"] = (
            "interrupted"
            if isinstance(error, (KeyboardInterrupt, SystemExit))
            else "failed"
        )
        result["error"] = dict(type=type(error).__name__, message=str(error)[:1000])
        raise
    finally:
        write_export(output, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-checkout", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    run(args.baseline_checkout, args.output)


if __name__ == "__main__":
    main()
