"""Owned terminal workflows with synthetic jobs; no agents or jobs are started."""

import argparse
import errno
import fcntl
import json
import math
import statistics
import os
from pathlib import Path
import pty
import selectors
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import textwrap
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
from verify_tui_responsiveness import identity
from agent_builds.experiments import parent_budget
from tui_terminal import TerminalScreen


FIXTURE = textwrap.dedent("""
    import contextlib, json, os, runpy, sys
    from pathlib import Path
    from unittest.mock import patch
    from agent_builds import tui
    from agent_builds.store import Store
    from agent_builds.tui_data import LazyStore
    root=Path(sys.argv[1]); count=int(sys.argv[2]); result=Path(sys.argv[3])
    state=root/'state'
    store=Store(state)
    store.update_config(dict(min_free_gb=0,min_available_gb=0,nice=0))
    jobs=[]
    for index in range(count):
        job=store.submit(root,['true'],'local','quick',1,.1,'fixture-'+str(index),'',5)
        store.db.execute('UPDATE jobs SET created=? WHERE id=?',(100+index,job))
        jobs.append(job)
    for job in jobs[1:3]:
        store.db.execute("UPDATE jobs SET state='failed',exit_code=1 WHERE id=?",(job,))
    store.db.execute('UPDATE jobs SET reason=?,wait_context=? WHERE id=?',
        ('repository already building',json.dumps(dict(measured_at=100,blocking_job=dict(id=jobs[1],elapsed_seconds=0,wait_seconds=0,command_display='fixture'))),jobs[0]))
    store.log_path(jobs[1]).write_text('RECORDED_BLOCKER_OUTPUT\\n')
    store.log_path(jobs[2]).write_text('FAILED_STAGE_OUTPUT\\n')
    plan=dict(id='f'*12,name='WORKFLOW_PLAN',state='failed',reason='',elapsed_seconds=0,controller_alive=None,
        stages=[dict(name='verification',state='failed',reason='',job_id=jobs[2],job=store.get(jobs[2]))])
    store.close()
    def snapshot():
        reader=Store(state,read_only=True)
        try:
            return [list(row) for row in reader.db.execute('SELECT * FROM jobs ORDER BY id')]
        finally:
            reader.close()
    before=snapshot(); config=(state/'config.json').read_bytes()
    os.environ['AGENT_BUILDS_STATE']=str(state)
    os.environ['XDG_CONFIG_HOME']=str(root/'user-config')
    def forbidden(*args,**kwargs):
        raise AssertionError('Native command or queue write from workflow')
    original=tui.read_tool
    sys.argv=['buildr','tui','--cwd',str(root)]
    with contextlib.ExitStack() as guards:
        guards.enter_context(patch.object(LazyStore,'writable',side_effect=forbidden))
        for name in ('_ensure_schema','submit','recover'):
            guards.enter_context(patch.object(Store,name,side_effect=forbidden))
        guards.enter_context(patch('subprocess.Popen',side_effect=forbidden))
        guards.enter_context(patch.object(tui,'read_tool',side_effect=lambda root,key: [plan] if key[0]=='plans' else original(root,key)))
        try:
            runpy.run_module('agent_builds',run_name='__main__')
        except SystemExit as outcome:
            assert outcome.code in (None,0), outcome.code
    assert before==snapshot() and config==(state/'config.json').read_bytes()
    preference=root/'user-config/buildr/tui.json'
    saved=preference.exists()
    if saved:
        assert json.loads(preference.read_text())==dict(version=1,focused=True,table=False)
    result.write_text(json.dumps(dict(queue_unchanged=True,native_commands=0,jobs=count,selected_job=jobs[0],stage_job=jobs[2],layout_saved=saved)))
""")


def measure(jobs, height, width):
    with tempfile.TemporaryDirectory(prefix="buildr-tui-workflow-") as directory:
        root = Path(directory)
        report = root / "result.json"
        env = dict(os.environ, TERM="xterm-256color", PYTHONPATH=str(ROOT))
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
        screen = TerminalScreen(height, width)
        child = None
        samples = []
        try:
            fcntl.ioctl(
                slave, termios.TIOCSWINSZ, struct.pack("HHHH", height, width, 0, 0)
            )
            child = subprocess.Popen(
                [sys.executable, "-c", FIXTURE, str(root), str(jobs), str(report)],
                cwd=root,
                env=env,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
            )
            os.close(slave)
            slave = None
            selector.register(master, selectors.EVENT_READ)

            def wait_for(predicate):
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    for unused, events in selector.select(0.01):
                        try:
                            data = os.read(master, 65536)
                        except OSError as error:
                            if error.errno != errno.EIO:
                                raise
                            data = b""
                        screen.feed(data)
                    if predicate(screen.text()):
                        return
                    if child.poll() is not None:
                        break
                raise AssertionError(
                    "Expected terminal state missing: " + screen.text()[-1600:]
                )

            def action(keys, predicate, label):
                started = time.monotonic()
                os.write(master, keys)
                wait_for(predicate)
                samples.append(dict(action=label, seconds=time.monotonic() - started))

            def menu(query, predicate, label):
                action(b":", lambda t: "BUILDR ACTIONS" in t, "menu-" + label)
                if width < 65 and query == "why waiting":
                    action(query.encode(), lambda t: "←→ 1/" in t, "context-start")
                    action(b"\x1bOC", lambda t: "←→ 2/" in t, "context-forward")
                    action(b"\x1bOD", lambda t: "←→ 1/" in t, "context-back")
                    action(b"\r", predicate, label)
                else:
                    action(query.encode() + b"\r", predicate, label)

            wait_for(
                lambda t: (
                    "BUILDR " in t
                    and "Loading queue snapshot" not in t
                    and ("JOBS" in t or "Jobs" in t or "jobs" in t)
                )
            )
            menu(
                "why waiting",
                lambda t: "WHY" in t and "ACTIONS" not in t,
                "diagnose-wait",
            )
            action(b"g", lambda t: "RECORDED_BLOCKER" in t, "recorded-blocker-log")
            action(
                b"b",
                lambda t: "WHY" in t and "ACTIONS" not in t and "LOG LIVE" not in t,
                "return-to-diagnosis",
            )
            menu("plans", lambda t: "PLANS" in t and "ACTIONS" not in t, "find-plan")
            action(b"]", lambda t: "failed > verif" in t, "select-failed-stage")
            action(b"\r", lambda t: "FAILED_STAGE_OUTPUT" in t, "failed-stage-log")
            action(b"b", lambda t: "failed > verif" in t, "return-to-stage")
            if width >= 65:
                menu("focus reading", lambda t: "[focus:" in t, "focus-content")
                action(b"Z", lambda t: "Layout saved" in t, "save-display-layout")
                menu("new job", lambda t: "Working directory" in t, "review-new-job")
                action(
                    b"\x1b",
                    lambda t: "failed > verif" in t and "Working directory" not in t,
                    "abort-submission",
                )
            else:
                action(b":", lambda t: "BUILDR ACTIONS" in t, "minimum-menu")
                action(
                    b"new job\r",
                    lambda t: "Resize to 65x15" in t,
                    "minimum-disabled-review",
                )
                action(
                    b"\x1b",
                    lambda t: "failed > verif" in t,
                    "cancel-disabled-menu",
                )
            os.write(master, b"q")
            assert child.wait(timeout=8) == 0
            result = json.loads(report.read_text())
            assert (
                result["jobs"] == jobs
                and result["queue_unchanged"]
                and result["native_commands"] == 0
            )
            return dict(
                result, height=height, width=width, native_exit=0, samples=samples
            )
        finally:
            if child is not None and child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=8)
            selector.close()
            os.close(master)
            if slave is not None:
                os.close(slave)


def run(output, max_action_seconds=5.0):
    if not math.isfinite(max_action_seconds) or max_action_seconds <= 0:
        raise ValueError("Use a finite positive action budget")
    output = Path(output).resolve()
    if output.exists() or ROOT == output or ROOT in output.parents:
        raise ValueError("Use a new output outside the checkout")
    budget = parent_budget(1)
    expected = identity(ROOT)
    result = dict(
        result="incomplete",
        outer_reservation=budget,
        source=expected,
        max_action_seconds=max_action_seconds,
        records=[],
        note="Owned xterm PTYs with 30/100 synthetic stored jobs. No real agents, native jobs or coding-productivity measurement.",
    )
    try:
        for jobs in (30, 100):
            for height, width in ((24, 100), (8, 20)):
                result["records"].append(measure(jobs, height, width))
                print(
                    json.dumps(
                        dict(jobs=jobs, height=height, width=width, result="passed")
                    ),
                    flush=True,
                )
        times = sorted(
            sample["seconds"]
            for record in result["records"]
            for sample in record["samples"]
        )
        result["timing"] = dict(
            samples=len(times),
            median_seconds=statistics.median(times),
            p95_seconds=times[math.ceil(0.95 * len(times)) - 1],
            max_seconds=max(times),
            p95_method="nearest-rank",
        )
        assert all(
            math.isfinite(value) and 0 <= value <= max_action_seconds for value in times
        ), "Workflow action exceeded the controlled response budget"
        assert all(
            record["layout_saved"] == (record["width"] >= 65)
            for record in result["records"]
        )
        assert identity(ROOT) == expected
        result["result"] = "passed"
        result["source_changed"] = False
    except BaseException as error:
        result["result"] = "failed"
        result["error"] = str(error)[:2000]
        raise
    finally:
        output.write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-action-seconds", type=float, default=5.0)
    args = parser.parse_args()
    result = run(args.output, args.max_action_seconds)
    print(
        json.dumps(
            dict(
                result=result["result"],
                source=result["source"],
                workflows=len(result["records"]),
            )
        )
    )
