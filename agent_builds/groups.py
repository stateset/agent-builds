"""Durable observation groups for existing jobs. No dispatch or cancellation."""
import json
import time
import uuid

from .completion import wait as wait_jobs
from .store import TERMINAL

NOTE = ('This group observes native job outcomes only. It does not resubmit work, '
        'impose dependencies, verify plan artifacts or establish external build completion.')


def available(store):
    return store.db is not None and store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_groups'").fetchone() is not None


def create(store, jobs, name='verification group'):
    handles = list(dict.fromkeys(jobs))
    if not 1 <= len(handles) <= 64:
        raise ValueError('A group requires 1 to 64 existing job handles')
    if not isinstance(name, str) or not name.strip() or len(name) > 100:
        raise ValueError('Group name must contain 1 to 100 characters')
    handle = uuid.uuid4().hex[:12]
    with store.transaction():
        for job in handles:
            store.get(job)
        store.db.execute('INSERT INTO job_groups (id,name,created,jobs) VALUES (?,?,?,?)',
                         (handle, name, time.time(), json.dumps(handles)))
    return handle


def definition(store, handle):
    row = store.db.execute('SELECT * FROM job_groups WHERE id=?', (handle,)).fetchone() if available(store) else None
    if row is None:
        raise ValueError('Unknown group: '+handle)
    result = dict(row)
    result['jobs'] = json.loads(result['jobs'])
    return result


def describe(group, rows, tail_lines=0):
    counts = {}
    jobs = []
    for row in rows:
        counts[row['state']] = counts.get(row['state'], 0)+1
        job = {key:row[key] for key in ('id','state','cwd','command_display','exit_code','wait_seconds','elapsed_seconds','log_path')}
        job['policy_version'] = row.get('policy_version') or 'unrecorded'
        job['runner_version'] = row['runner_version']
        job['source_changed_before_start'] = row['source'].get('changed_since_submission')
        job['source_changed_during_run'] = row['source'].get('changed_during_run')
        if tail_lines and 'log_tail' in row:
            job['log_tail'] = row['log_tail']
        jobs.append(job)
    complete = all(row['state'] in TERMINAL for row in rows)
    code = next((r['exit_code'] or 1 for r in rows if r['state']!='succeeded'),0) if complete else None
    state = ('succeeded' if code==0 else 'failed') if complete else 'active'
    return dict(schema='agent-builds-job-group-v1', id=group['id'], name=group['name'], created=group['created'],
                observed_at=time.time(), state=state, complete=complete, exit_code=code, counts=counts, jobs=jobs,
                result_scope='native_job_outcomes', note=NOTE)


def get(store, handle):
    group = definition(store, handle)
    return describe(group, [store.get(job) for job in group['jobs']])


def listing(store, limit=50):
    if type(limit) is not int or not 1<=limit<=200:
        raise ValueError('Group list limit must be between 1 and 200')
    if not available(store):
        return []
    handles = [row[0] for row in store.db.execute('SELECT id FROM job_groups ORDER BY created DESC,id DESC LIMIT ?', (limit,))]
    return [get(store, handle) for handle in handles]


def wait(store, handle, timeout=None, tail_lines=0):
    with store.read_snapshot():
        group = definition(store, handle)
    result = wait_jobs(store, group['jobs'], timeout, tail_lines)
    return dict(complete=result['complete'], timed_out=result['timed_out'], exit_code=result['exit_code'],
                group=describe(group, result['jobs'], tail_lines))


def export(store, handle):
    from . import __version__
    with store.read_snapshot():
        group = definition(store, handle)
        rows = [store.get(job) for job in group['jobs']]
    return dict(schema='agent-builds-group-evidence-v1', application_version=__version__, exported_at=time.time(),
                group=describe(group, rows), jobs=rows, note=NOTE+' Existing source observations are retained; missing historical evidence is not reconstructed.')


def text(result):
    current = result.get('group', result)
    output = ['{} {} | exit {} | {}'.format(current['id'], current['state'],current['exit_code'],current['name'])]
    for job in current['jobs']:
        output += ['{} {} | exit {} | {}'.format(job['id'],job['state'],job['exit_code'],job['log_path'])]
        output += job.get('log_tail',[])
    if result.get('timed_out'):
        output += ['Wait timed out; existing jobs continue.']
    return output+[NOTE]
