"""Read-only reattachment to one or more durable local job handles."""
import math
import time

from .store import TERMINAL


def wait(store, jobs, timeout=None, tail_lines=0):
    if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
        raise ValueError('Wait timeout must be positive')
    if not 0 <= tail_lines <= 200:
        raise ValueError('Log tails must contain between 0 and 200 lines')
    handles = list(dict.fromkeys(jobs))
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        with store.read_snapshot():
            rows = [store.get(job) for job in handles]
        complete = all(row['state'] in TERMINAL for row in rows)
        timed_out = not complete and deadline is not None and time.monotonic() >= deadline
        if complete or timed_out:
            if tail_lines:
                for row in rows:
                    try:
                        with store.log_path(row['id']).open('rb') as log:
                            log.seek(0, 2)
                            log.seek(max(0, log.tell() - 65536))
                            row['log_tail'] = log.read().decode('utf-8', errors='replace').splitlines()[-tail_lines:]
                    except FileNotFoundError:
                        row['log_tail'] = []
            code = 124 if timed_out else next((r['exit_code'] or 1 for r in rows if r['state'] != 'succeeded'), 0)
            return dict(complete=complete, timed_out=timed_out, jobs=rows, exit_code=code)
        time.sleep(min(.3, max(.01, deadline - time.monotonic())) if deadline else .3)
