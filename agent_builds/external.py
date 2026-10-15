"""Durable external completion watches; idle controllers reserve no build slots."""
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid
from argparse import Namespace

from .store import Store, TERMINAL, alive, identity

FINISHED = ('succeeded', 'failed', 'cancelled', 'uncertain', 'timed_out', 'exhausted')
STATES = ('queued', 'probing', 'waiting', 'cancelling')+FINISHED
NOTE = ('Only explicit repeatable read-only probes are polled. Exit 0 reports external completion; '
        '75 confirms pending; other exits stop this watch. Unknown probe outcomes are never retried. '
        'Cancelling or timing out a watch stops observation, not the external task. '
        'Idle watches hold no CPU, RAM, job slot, disk scratch or checkout lock.')


def validate(value, config):
    if not isinstance(value, dict) or set(value)-{'cwd', 'command', 'external_ref', 'read_only_probe',
            'name', 'interval', 'timeout', 'probe_timeout', 'max_probes', 'jobs', 'memory_gb', 'resource_key', 'track_source'}:
        raise ValueError('External watch contains unsupported fields')
    if value.get('read_only_probe') is not True:
        raise ValueError('Explicitly declare read_only_probe=true; submission/launch commands must never be repeated as probes')
    command = value.get('command')
    if not isinstance(command, list) or not 1 <= len(command) <= 128 or not command[0] or any(not isinstance(arg, str) or '\0' in arg or len(arg)>4096 for arg in command) or sum(map(len, command))>32768:
        raise ValueError('Probe must be a bounded nonempty literal argv array')
    cwd = value.get('cwd')
    if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
        raise ValueError('Probe cwd must be an existing absolute directory')
    result = dict(cwd=str(Path(cwd).resolve()), command=command, read_only_probe=True)
    for key, default, maximum in (('name', 'external completion', 100), ('external_ref', None, 1000)):
        text = value.get(key, default)
        if not isinstance(text, str) or not text.strip() or len(text)>maximum or any(ord(c)<32 or 127<=ord(c)<=159 for c in text):
            raise ValueError(key+' must be bounded nonempty text without control characters')
        result[key] = text
    for key, default, minimum, maximum in (('interval', 15, .1, 3600), ('timeout', 3600, .1, 86400),
            ('probe_timeout', 30, .1, 300), ('memory_gb', .5, .01, config['memory_gb'])):
        number = value.get(key, default)
        if type(number) not in (int, float) or not math.isfinite(number) or not minimum<=number<=maximum:
            raise ValueError(key+' must be finite and within supported bounds')
        result[key] = number
    for key, default, maximum in (('jobs', 1, config['cpu_slots']), ('max_probes', 120, 1000)):
        number = value.get(key, default)
        if type(number) is not int or not 1<=number<=maximum:
            raise ValueError(key+' must be a positive integer within supported bounds')
        result[key] = number
    if 'resource_key' in value:
        if not isinstance(value['resource_key'], str) or not value['resource_key'] or '\0' in value['resource_key'] or len(value['resource_key'])>4096:
            raise ValueError('resource_key must be bounded nonempty text')
        result['resource_key'] = value['resource_key']
    if type(value.get('track_source', True)) is not bool:
        raise ValueError('track_source must be boolean')
    result['track_source'] = value.get('track_source', True)
    return result


def available(store):
    return store.db is not None and store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='external_watches'").fetchone() is not None


def raw(store, handle):
    row = store.db.execute('SELECT * FROM external_watches WHERE id=?', (handle,)).fetchone() if available(store) else None
    if row is None:
        raise ValueError('Unknown external watch: '+handle)
    return dict(row)


def get(store, handle, limit=20):
    row = raw(store, handle)
    spec = json.loads(row.pop('definition'))
    now = row['finished'] or time.time()
    row.update(schema='buildr-external-watch-v1', definition=spec, complete=row['state'] in FINISHED,
        elapsed_seconds=max(0, now-row['created']),
        external_idle_seconds=row['idle_seconds']+(max(0, now-row['idle_since']) if row['idle_since'] is not None else 0),
        controller_alive=None if store.read_only else alive(row['owner_pid'], row['owner_start']), note=NOTE)
    totals = dict(probes=0, linked_jobs=0, local_reserved_seconds=0, queued_seconds=0, cpu_seconds=0,
                  usage_measured_jobs=0,usage_complete_jobs=0)
    probes = []
    # Read history in one query; listing many completed watches must not issue
    # one full job-description/configuration lookup per historical probe.
    query='''SELECT p.*,j.id AS linked_id,j.state AS job_state,j.started AS job_started,
        j.finished AS job_finished,j.created AS job_created,j.exit_code AS native_code,
        j.slots AS job_slots,j.usage AS job_usage FROM external_probes p
        LEFT JOIN jobs j ON j.id=p.job_id WHERE p.watch_id=? ORDER BY p.ordinal'''
    for item in store.db.execute(query, (handle,)):
        data=dict(item)
        probe={key:data[key] for key in ('watch_id','ordinal','request_key','job_id','created','settled','exit_code')}
        totals['probes'] += 1
        if probe['job_id']:
            if data['linked_id']:
                usage=json.loads(data['job_usage'] or '{}')
                totals['linked_jobs'] += 1
                totals['local_reserved_seconds'] += max(0,(data['job_finished'] or time.time())-data['job_started']) if data['job_started'] else 0
                totals['queued_seconds'] += max(0,(data['job_started'] or data['job_finished'] or time.time())-data['job_created'])
                totals['cpu_seconds'] += usage.get('cpu_seconds',0)
                totals['usage_measured_jobs'] += bool(usage.get('samples',0))
                totals['usage_complete_jobs'] += usage.get('complete') is True
                probe.update(state=data['job_state'], native_exit_code=data['native_code'], log_path=str(store.log_path(probe['job_id'])),
                    reserved_cpu_slots=data['job_slots'] if data['job_state'] in ('running','cancelling') else 0,
                    semantic_result='pending' if probe['settled'] and data['job_state']=='failed' and data['native_code']==75 else
                        'completion_reported' if probe['settled'] and data['job_state']=='succeeded' and data['native_code']==0 else 'inspect_native_result')
            else:
                probe.update(state='unknown', reserved_cpu_slots=None)
        probes.append(probe)
    row.update(probes=probes[-limit:], probe_count=totals['probes'], probes_truncated=len(probes)>limit,
               accounting=totals, accounting_complete=totals['usage_complete_jobs']==totals['linked_jobs']==totals['probes'])
    row['current_job_id'] = probes[-1]['job_id'] if probes and not probes[-1]['settled'] else None
    row['external_outcome'] = 'completion_reported' if row['state']=='succeeded' else 'pending_reported' if probes and probes[-1]['settled'] and probes[-1].get('native_exit_code')==75 else 'unknown'
    return row


def listing(store, active=False, limit=20):
    if type(limit) is not int or not 1<=limit<=100:
        raise ValueError('Watch list limit must be between 1 and 100')
    if not available(store):
        return []
    where = " WHERE state NOT IN ('succeeded','failed','cancelled','uncertain','timed_out','exhausted')" if active else ''
    return [get(store, row[0], limit=3) for row in store.db.execute('SELECT id FROM external_watches'+where+' ORDER BY created DESC LIMIT ?', (limit,))]


def counts(store):
    result = dict.fromkeys(STATES,0)
    if available(store):
        result.update({row[0]:row[1] for row in store.db.execute('SELECT state,COUNT(*) FROM external_watches GROUP BY state')})
    return result


def create(store, definition):
    if os.environ.get('AGENT_BUILDS_JOB_ID'):
        raise ValueError('Start external watches outside queued jobs; each probe uses the shared queue')
    spec = validate(definition, store.config())
    handle, now = uuid.uuid4().hex[:12], time.time()
    with store.transaction():
        store.db.execute('INSERT INTO external_watches (id,created,definition,state,next_probe_at) VALUES (?,?,?,?,?)',
                         (handle, now, json.dumps(spec), 'queued', now))
    return handle


def spawn(store, handle):
    if os.environ.get('AGENT_BUILDS_JOB_ID'):
        raise ValueError('Watch controllers must run outside queued jobs')
    row = raw(store, handle)
    if row['state'] in FINISHED:
        raise ValueError('Terminal or uncertain watches cannot be retried; inspect their probe handles')
    if alive(row['owner_pid'], row['owner_start']):
        raise ValueError('External watch controller is already running')
    env = dict(os.environ, AGENT_BUILDS_STATE=str(store.root))
    env['PYTHONPATH'] = str(Path(__file__).resolve().parent.parent)+os.pathsep+env.get('PYTHONPATH', '')
    with (store.root/'logs'/('watch-'+handle+'.log')).open('ab') as log:
        os.chmod(log.name, 0o600)
        return subprocess.Popen([sys.executable, '-m', 'agent_builds', 'watch-worker', handle],
            env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)


def close_idle(store, row, now):
    seconds = row['idle_seconds']+(max(0, now-row['idle_since']) if row['idle_since'] is not None else 0)
    store.db.execute('UPDATE external_watches SET idle_seconds=?,idle_since=NULL WHERE id=?', (seconds, row['id']))


def finish(store, row, state, code, reason):
    now = time.time()
    close_idle(store, row, now)
    store.db.execute('UPDATE external_watches SET state=?,exit_code=?,finished=?,reason=?,next_probe_at=NULL WHERE id=?',
                     (state, code, now, reason, row['id']))


def prepare(store, handle):
    """Reserve one durable probe intent before any submission side effect."""
    from .runner import resource_key
    from .source import capture
    initial = raw(store, handle)
    spec = json.loads(initial['definition'])
    now = time.time()
    if initial['state'] in FINISHED or initial['cancel_requested'] or initial['next_probe_at'] is None or now<initial['next_probe_at'] or now>=initial['created']+spec['timeout']:
        return None
    source = capture(spec['cwd']) if spec['track_source'] else None
    resource = spec.get('resource_key') or resource_key(Path(spec['cwd']))
    with store.transaction():
        row = raw(store, handle)
        spec, now = json.loads(row['definition']), time.time()
        previous = store.db.execute('SELECT * FROM external_probes WHERE watch_id=? ORDER BY ordinal DESC LIMIT 1', (handle,)).fetchone()
        if previous and previous['settled'] is None:
            return dict(previous)
        if row['state'] in FINISHED or row['cancel_requested'] or now>=row['created']+spec['timeout'] or row['next_probe_at'] is None or now<row['next_probe_at']:
            return None
        number = previous['ordinal']+1 if previous else 1
        if number>spec['max_probes']:
            finish(store, row, 'exhausted', 75, 'Probe limit reached; last confirmed observation is still pending')
            return None
        close_idle(store, row, now)
        key = 'watch-{}-{}'.format(handle, number)
        job = store.submit(Path(spec['cwd']), spec['command'], 'local', 'quick', spec['jobs'],
            spec['memory_gb'], resource, '', spec['probe_timeout'], herdr=dict(external_watch_id=handle, probe_ordinal=number))
        if source is not None:
            store.record_source(job, dict(submission=source))
        token = uuid.uuid4().hex
        store.db.execute('INSERT INTO external_probes (watch_id,ordinal,request_key,created,job_id,launch_token) VALUES (?,?,?,?,?,?)',
                         (handle, number, key, now, job, token))
        store.db.execute("UPDATE external_watches SET state='probing',next_probe_at=NULL WHERE id=?", (handle,))
        return dict(store.db.execute('SELECT * FROM external_probes WHERE watch_id=? AND ordinal=?', (handle, number)).fetchone())


def dispatch(store, row, probe):
    with store.transaction():
        current = raw(store, row['id'])
        record = store.db.execute('SELECT * FROM external_probes WHERE watch_id=? AND ordinal=?', (row['id'], probe['ordinal'])).fetchone()
        if record['launch_attempted'] or record['attached'] or current['cancel_requested'] or current['state'] in FINISHED:
            return None
        # Persist before Popen. A crash in the uncertain launch window never
        # retries this worker; recovery reconciles the existing job handle.
        store.db.execute('UPDATE external_probes SET launch_attempted=1 WHERE watch_id=? AND ordinal=?', (row['id'], probe['ordinal']))
    env = dict(os.environ, AGENT_BUILDS_STATE=str(store.root))
    env['PYTHONPATH'] = str(Path(__file__).resolve().parent.parent)+os.pathsep+env.get('PYTHONPATH', '')
    try:
        with (store.root/'logs'/('watch-'+row['id']+'.log')).open('ab') as log:
            os.chmod(log.name, 0o600)
            return subprocess.Popen([sys.executable, '-m', 'agent_builds', 'watch-probe-worker', probe['job_id'], probe['launch_token']],
                env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    except OSError as error:
        store.finish(probe['job_id'], 'failed', 2, 'Probe worker could not launch: '+type(error).__name__+'; not retried')


def attach_job(store, job, token):
    from . import __version__
    from .scheduler import POLICY_VERSION
    with store.transaction():
        probe = store.db.execute('SELECT * FROM external_probes WHERE job_id=?', (job,)).fetchone()
        if probe is None or probe['launch_token']!=token or probe['attached'] or not probe['launch_attempted']:
            raise ValueError('Prepared external probe is already attached or has an invalid token')
        row = store.get(job)
        if row['state']!='queued':
            raise ValueError('Prepared probe outcome changed; work will not be retried')
        store.db.execute('UPDATE external_probes SET attached=1 WHERE job_id=?', (job,))
        store.db.execute('UPDATE jobs SET owner_pid=?,owner_start=?,runner_version=?,policy_version=? WHERE id=?',
                         (os.getpid(), identity(os.getpid()), __version__, POLICY_VERSION, job))
        watch = raw(store, probe['watch_id'])
        if watch['cancel_requested']:
            store.db.execute('UPDATE jobs SET cancel_requested=1 WHERE id=?', (job,))
    return Namespace(cwd=row['cwd'], command=row['argv'], lane=row['lane'], backend='local',
        jobs=row['slots'], memory_gb=row['memory_gb'], timeout=row['timeout'], image='', resource_key=row['resource_key'],
        min_free_gb=0, scratch_gb=0, detach=False, events_json=True, notify=False, receipt_file=None,
        track_source=json.loads(watch['definition'])['track_source'], source_policy='report', quiet_output=True), job


def step(store, handle):
    """Observe settled results and persist intent. Never repeat an unknown probe."""
    row = raw(store, handle)
    if row['state'] in FINISHED:
        return
    spec, now = json.loads(row['definition']), time.time()
    if not row['stop_kind'] and (row['cancel_requested'] or now>=row['created']+spec['timeout']):
        kind = 'user' if row['cancel_requested'] else 'deadline'
        store.db.execute("UPDATE external_watches SET cancel_requested=1,stop_kind=?,state='cancelling' WHERE id=?", (kind, handle))
        row = raw(store, handle)
    probe = store.db.execute('SELECT * FROM external_probes WHERE watch_id=? ORDER BY ordinal DESC LIMIT 1', (handle,)).fetchone()
    if probe and probe['settled'] is None:
        probe = dict(probe)
        try:
            job = store.get(probe['job_id'])
        except ValueError:
            with store.transaction():
                finish(store, row, 'uncertain', 1, 'Probe job is missing; it was not retried')
            return
        if job['state'] not in TERMINAL:
            if row['cancel_requested']:
                if not probe['launch_attempted'] and not probe['attached']:
                    # Known unlaunched intent, serialized against dispatch.
                    with store.transaction():
                        current = store.db.execute('SELECT * FROM external_probes WHERE job_id=?', (job['id'],)).fetchone()
                        if not current['launch_attempted'] and not current['attached']:
                            store.finish(job['id'], 'cancelled', 130, 'Watch stopped before probe launch')
                else:
                    from .runner import cancel_job
                    cancel_job(store, job['id'])
            elif not probe['launch_attempted']:
                return dispatch(store, row, probe)
            return
        with store.transaction():
            row = raw(store, handle)
            store.db.execute('UPDATE external_probes SET settled=?,exit_code=? WHERE watch_id=? AND ordinal=? AND settled IS NULL',
                             (now, job['exit_code'], handle, probe['ordinal']))
            if job['state']=='lost' or job['exit_code'] is None:
                finish(store, row, 'uncertain', 1, 'Probe outcome unknown; inspect '+job['id']+' before any new observation')
            elif row['cancel_requested']:
                finish(store, row, 'timed_out' if row['stop_kind']=='deadline' else 'cancelled',
                       124 if row['stop_kind']=='deadline' else 130, 'Observation stopped; external task was not cancelled')
            elif job['state']=='succeeded' and job['exit_code']==0:
                finish(store, row, 'succeeded', 0, 'Explicit completion probe reported success')
            elif job['state']=='failed' and job['exit_code']==75:
                store.db.execute("UPDATE external_watches SET state='waiting',reason='Confirmed pending; no local reservation between probes',idle_since=?,next_probe_at=? WHERE id=?",
                                 (now, now+spec['interval'], handle))
                if probe['ordinal']>=spec['max_probes']:
                    finish(store, raw(store, handle), 'exhausted', 75, 'Probe limit reached; external task still reported pending')
            else:
                finish(store, row, 'cancelled' if job['state']=='cancelled' else 'failed', job['exit_code'],
                       'Probe stopped with native exit {}; external task outcome is not inferred'.format(job['exit_code']))
        return
    if row['cancel_requested']:
        with store.transaction():
            finish(store, raw(store, handle), 'timed_out' if row['stop_kind']=='deadline' else 'cancelled',
                   124 if row['stop_kind']=='deadline' else 130, 'Observation stopped; external task was not cancelled')
        return
    probe = prepare(store, handle)
    if probe:
        return dispatch(store, raw(store, handle), probe)


def control(handle):
    if os.environ.get('AGENT_BUILDS_JOB_ID'):
        raise ValueError('Watch controllers must run outside queued jobs')
    store, stop, previous = Store(), [False], {}
    children = {}
    token = identity(os.getpid())
    try:
        with store.transaction():
            row = raw(store, handle)
            if row['state'] in FINISHED:
                return
            if alive(row['owner_pid'], row['owner_start']):
                raise ValueError('Another controller owns this external watch')
            store.db.execute('UPDATE external_watches SET owner_pid=?,owner_start=?,reason=? WHERE id=?',
                             (os.getpid(), token, 'Controller monitoring durable probes', handle))
        def stop_controller(signum, frame):
            stop[0] = True
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, stop_controller)
        while not stop[0]:
            store.recover()
            for job, child in list(children.items()):
                if child.poll() is not None:
                    attached = store.db.execute('SELECT attached FROM external_probes WHERE job_id=?', (job,)).fetchone()[0]
                    if not attached and store.get(job)['state'] not in TERMINAL:
                        store.finish(job, 'failed', child.returncode or 1, 'Probe worker exited before attachment; not retried')
                    del children[job]
            child = step(store, handle)
            if child is not None:
                current = store.db.execute('SELECT job_id FROM external_probes WHERE watch_id=? ORDER BY ordinal DESC LIMIT 1', (handle,)).fetchone()
                children[current['job_id']] = child
            if raw(store, handle)['state'] in FINISHED:
                return
            time.sleep(.1)
    except BaseException as error:
        store.db.execute('UPDATE external_watches SET reason=? WHERE id=? AND owner_pid=? AND owner_start=?',
            ('Controller stopped: '+type(error).__name__+'; inspect handles and use watch resume', handle, os.getpid(), token))
        raise
    finally:
        store.db.execute('UPDATE external_watches SET owner_pid=NULL,owner_start=NULL WHERE id=? AND owner_pid=? AND owner_start=?',
                         (handle, os.getpid(), token))
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        store.close()


def cancel(store, handle):
    with store.transaction():
        row = raw(store, handle)
        if row['state'] in FINISHED:
            return
        store.db.execute("UPDATE external_watches SET cancel_requested=1,stop_kind=CASE WHEN stop_kind='' THEN 'user' ELSE stop_kind END,state='cancelling' WHERE id=?", (handle,))
        probe = store.db.execute('SELECT * FROM external_probes WHERE watch_id=? AND settled IS NULL ORDER BY ordinal DESC LIMIT 1', (handle,)).fetchone()
        if probe is not None and not probe['launch_attempted'] and not probe['attached']:
            store.finish(probe['job_id'], 'cancelled', 130, 'Watch cancelled before probe launch')
    if probe is not None:
        from .runner import cancel_job
        cancel_job(store, probe['job_id'])
    if not alive(row['owner_pid'], row['owner_start']):
        return spawn(store, handle)


def wait(store, handle, timeout=None):
    if timeout is not None and (type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout<=0):
        raise ValueError('Wait timeout must be positive')
    deadline = time.monotonic()+timeout if timeout is not None else None
    while True:
        with store.read_snapshot():
            row = get(store, handle)
        if row['complete'] or deadline is not None and time.monotonic()>=deadline:
            return dict(complete=row['complete'], timed_out=not row['complete'], exit_code=row['exit_code'] if row['complete'] else 124, watch=row)
        time.sleep(.1)


def lines(row):
    spec = row['definition']
    result = ['{} | {} | {}'.format(row['id'], row['state'], spec['name']),
              'External reference: '+spec['external_ref'],
              '{} probes | {:.1f}s idle without reservations | {:.1f}s local reservations'.format(
                  row['probe_count'], row['external_idle_seconds'], row['accounting']['local_reserved_seconds']), row['reason']]
    for probe in row['probes']:
        result.append('#{} {} | {} | exit {} | {}'.format(probe['ordinal'], probe.get('job_id') or probe['request_key'],
                      probe.get('state', 'submission intent'), probe.get('native_exit_code'), probe.get('log_path', '')))
    return result+['', NOTE]
