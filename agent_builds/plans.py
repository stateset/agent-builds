"""Durable dependency plans. Controllers hold no CPU or memory reservations."""
from argparse import Namespace
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

from .profiles import name as valid_name
from .source import capture, changed, digest_file
from .store import Store, TERMINAL, alive, identity

STAGE_TERMINAL = ('succeeded', 'failed', 'cancelled', 'lost', 'skipped')
PLAN_TERMINAL = ('succeeded', 'failed', 'cancelled')


def validate(definition, cwd, config):
    if not isinstance(definition, dict) or set(definition) - {'version', 'name', 'stages', 'max_parallel', 'source_policy'}:
        raise ValueError('Plan must be an object with version, name, stages, max_parallel and/or source_policy')
    if type(definition.get('version', 1)) is not int or definition.get('version', 1) != 1:
        raise ValueError('Unsupported plan version')
    root = Path(cwd).expanduser().resolve()
    if not root.is_dir():
        raise ValueError('Plan checkout must already exist')
    parallel = definition.get('max_parallel', 1)
    if type(parallel) is not int or not 1 <= parallel <= 16:
        raise ValueError('max_parallel must be between 1 and 16')
    title = definition.get('name', 'verification')
    if not isinstance(title, str) or not title.strip() or len(title) > 100:
        raise ValueError('Plan name must contain 1 to 100 characters')
    policy = definition.get('source_policy', 'report')
    if policy not in ('report', 'require_unchanged'):
        raise ValueError('source_policy must be report or require_unchanged')
    raw = definition.get('stages')
    if not isinstance(raw, list) or not 1 <= len(raw) <= 64:
        raise ValueError('A plan requires 1 to 64 stages')
    stages = []
    known = set()
    fields = {'name', 'command', 'after', 'cwd', 'lane', 'jobs', 'memory_gb', 'timeout', 'backend',
              'image', 'min_free_gb', 'scratch_gb', 'resource_key', 'artifacts', 'source_policy', 'when'}
    for stage in raw:
        if not isinstance(stage, dict) or set(stage) - fields:
            raise ValueError('Stage contains unsupported fields')
        name = stage.get('name')
        if not isinstance(name, str):
            raise ValueError('Every stage needs a name')
        valid_name(name)
        if name in known:
            raise ValueError('Duplicate stage: ' + name)
        command = stage.get('command')
        if not isinstance(command, list) or not command or not command[0] or any(not isinstance(arg, str) or '\0' in arg for arg in command):
            raise ValueError('Stage command must be a nonempty argv array')
        after = stage.get('after', [stages[-1]['name']] if stages else [])
        if not isinstance(after, list) or any(not isinstance(item, str) for item in after) or len(set(after)) != len(after):
            raise ValueError('after must be a list of unique stage names')
        location = stage.get('cwd', '.')
        if not isinstance(location, str):
            raise ValueError('Stage cwd must be a path')
        location = (root / location).resolve()
        if not location.is_dir() or (location != root and root not in location.parents):
            raise ValueError('Stage cwd must be inside the plan checkout')
        lane = stage.get('lane', 'quick')
        backend = stage.get('backend', 'local')
        if lane not in ('quick', 'heavy') or backend not in ('local', 'docker'):
            raise ValueError('Invalid stage lane or backend')
        slots = stage.get('jobs', config[lane+'_jobs'])
        memory = stage.get('memory_gb', 6 if lane == 'heavy' else 2)
        timeout = stage.get('timeout', config['timeout_seconds'])
        if type(slots) is not int or not 1 <= slots <= config['cpu_slots']:
            raise ValueError('Stage CPU slots exceed machine budget')
        if type(memory) not in (int, float) or not math.isfinite(memory) or not 0 < memory <= config['memory_gb']:
            raise ValueError('Stage memory exceeds machine budget')
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('Stage timeout must be positive')
        disk = {key: stage.get(key, 0) for key in ('min_free_gb', 'scratch_gb')}
        if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in disk.values()):
            raise ValueError('Disk reservations must be finite and nonnegative')
        artifacts = stage.get('artifacts', [])
        if not isinstance(artifacts, list) or len(artifacts) > 32:
            raise ValueError('artifacts must contain at most 32 relative file paths')
        for artifact in artifacts:
            if (not isinstance(artifact, str) or not artifact or '\0' in artifact or
                    Path(artifact).is_absolute() or '..' in Path(artifact).parts):
                raise ValueError('Artifacts must be relative file paths inside the stage checkout')
        source_policy = stage.get('source_policy', policy)
        if source_policy not in ('report', 'require_unchanged'):
            raise ValueError('Invalid stage source policy')
        when = stage.get('when','success')
        if when not in ('success','always'):
            raise ValueError('Stage when must be success or always')
        image = stage.get('image', config['docker_image'])
        if not isinstance(image, str) or (backend == 'docker' and (not image or ',' in str(location))):
            raise ValueError('Docker stages require an image and a supported bind path')
        resource = stage.get('resource_key')
        if resource is not None and (not isinstance(resource, str) or not resource or '\0' in resource):
            raise ValueError('resource_key must be nonempty text')
        stages.append(dict(name=name, command=command, after=after, cwd=str(location), lane=lane,
            backend=backend, jobs=slots, memory_gb=memory, timeout=timeout, image=image,
            resource_key=resource, artifacts=artifacts, source_policy=source_policy, when=when, **disk))
        known.add(name)
    graph = {s['name']: s['after'] for s in stages}
    done = set()
    def visit(name, visiting):
        if name not in graph:
            raise ValueError('Unknown dependency: ' + name)
        if name in visiting:
            raise ValueError('Plan contains a dependency cycle')
        if name in done:
            return
        for dependency in graph[name]:
            visit(dependency, visiting | {name})
        done.add(name)
    for name in graph:
        visit(name, set())
    return dict(version=1, name=title, cwd=str(root), max_parallel=parallel, source_policy=policy, stages=stages)


def read_manifest(path, cwd, config):
    with Path(path).expanduser().open('rb') as manifest:
        raw = manifest.read(65537)
    if len(raw) > 65536:
        raise ValueError('Plan manifest exceeds 64 KiB')
    return validate(json.loads(raw), cwd, config)


def available(store):
    return store.db is not None and store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='plans'").fetchone() is not None


def get(store, plan):
    raw = store.db.execute('SELECT * FROM plans WHERE id=?', (plan,)).fetchone() if available(store) else None
    if raw is None:
        raise ValueError('Unknown plan: ' + plan)
    result = dict(raw)
    result['definition'] = json.loads(result['definition'])
    result['source'] = json.loads(result['source'])
    result['stages'] = []
    for raw in store.db.execute('SELECT * FROM plan_stages WHERE plan_id=? ORDER BY ordinal', (plan,)):
        stage = {key: raw[key] for key in ('name', 'ordinal', 'state', 'reason', 'job_id')}
        stage['definition'] = json.loads(raw['definition'])
        stage['evidence'] = json.loads(raw['evidence'])
        stage['job'] = store.get(stage['job_id']) if stage['job_id'] else None
        result['stages'].append(stage)
    result['controller_alive'] = None if store.read_only else alive(result['owner_pid'], result['owner_start'])
    result['elapsed_seconds'] = max(0, (result['finished'] or time.time())-result['created'])
    result['exit_code'] = exit_code(result) if result['state'] in PLAN_TERMINAL else None
    return result


def listing(store, active=False, limit=50):
    if not available(store):
        return []
    where = " WHERE state NOT IN ('succeeded','failed','cancelled')" if active else ''
    ids = [row[0] for row in store.db.execute('SELECT id FROM plans'+where+' ORDER BY created DESC LIMIT ?', (limit,))]
    return [get(store, plan) for plan in ids]


def exit_code(plan):
    if plan['state'] == 'succeeded':
        return 0
    if plan['state'] == 'cancelled':
        return 130
    for stage in plan['stages']:
        if stage['state'] in ('failed', 'cancelled', 'lost'):
            return (stage['job']['exit_code'] if stage['job'] else None) or 1
    return 1


def create(store, definition):
    if os.environ.get('AGENT_BUILDS_JOB_ID'):
        raise ValueError('Do not run a plan inside a queued job. The plan queues each stage itself.')
    source = capture(definition['cwd'])
    plan = uuid.uuid4().hex[:12]
    with store.transaction():
        store.db.execute('INSERT INTO plans (id,name,cwd,definition,created,state,source) VALUES (?,?,?,?,?,?,?)',
            (plan, definition['name'], definition['cwd'], json.dumps(definition), time.time(), 'queued', json.dumps(source)))
        for index, stage in enumerate(definition['stages']):
            store.db.execute('INSERT INTO plan_stages (plan_id,name,ordinal,definition) VALUES (?,?,?,?)',
                             (plan, stage['name'], index, json.dumps(stage)))
    return plan


def spawn(store, plan):
    if os.environ.get('AGENT_BUILDS_JOB_ID'):
        raise ValueError('Plan controllers must run outside queued jobs')
    current = get(store, plan)
    if current['state'] in PLAN_TERMINAL:
        raise ValueError('Terminal plans cannot be retried. Submit a new plan explicitly.')
    if alive(current['owner_pid'], current['owner_start']):
        raise ValueError('Plan controller is already running')
    env = os.environ.copy()
    env['AGENT_BUILDS_STATE'] = str(store.root)
    env['PYTHONPATH'] = str(Path(__file__).resolve().parent.parent)+os.pathsep+env.get('PYTHONPATH', '')
    path = store.root / 'logs' / ('plan-'+plan+'.log')
    with path.open('ab') as log:
        os.chmod(str(path), 0o600)
        return subprocess.Popen([sys.executable, '-m', 'agent_builds', 'plan-worker', plan],
            env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)


def attach_job(store, job, token):
    from . import __version__
    from .scheduler import POLICY_VERSION
    with store.transaction():
        stage = store.db.execute('SELECT * FROM plan_stages WHERE job_id=?', (job,)).fetchone()
        if stage is None or stage['launch_token'] != token or stage['attached']:
            raise ValueError('Prepared stage was already attached or has an invalid launch token')
        row = store.get(job)
        if row['state'] != 'queued':
            raise ValueError('Prepared stage outcome changed; work will not be retried')
        store.db.execute('UPDATE plan_stages SET attached=1 WHERE job_id=?', (job,))
        store.db.execute('UPDATE jobs SET owner_pid=?,owner_start=?,runner_version=?,policy_version=? WHERE id=?',
                         (os.getpid(), identity(os.getpid()), __version__, POLICY_VERSION, job))
    definition = json.loads(stage['definition'])
    return Namespace(cwd=row['cwd'], command=row['argv'], lane=row['lane'], backend=row['backend'],
        jobs=row['slots'], memory_gb=row['memory_gb'], timeout=row['timeout'], image=row['image'],
        resource_key=row['resource_key'], min_free_gb=row['min_free_gb'], scratch_gb=row['scratch_gb'],
        detach=False, events_json=True, notify=False, receipt_file=None, track_source=True,
        source_policy=definition['source_policy'], quiet_output=True), job


def prepare(store, plan, stage):
    from .runner import resource_key
    spec = stage['definition']
    resource = spec['resource_key'] or resource_key(spec['cwd'])
    token = uuid.uuid4().hex
    with store.transaction():
        raw = store.db.execute('SELECT state FROM plan_stages WHERE plan_id=? AND name=?', (plan['id'], stage['name'])).fetchone()
        current = store.db.execute('SELECT cancel_requested FROM plans WHERE id=?', (plan['id'],)).fetchone()
        if raw['state'] != 'pending' or current['cancel_requested']:
            return None
        job = store.submit(Path(spec['cwd']), spec['command'], spec['backend'], spec['lane'],
            spec['jobs'], spec['memory_gb'], resource, spec['image'], spec['timeout'],
            min_free_gb=spec['min_free_gb'], scratch_gb=spec['scratch_gb'])
        store.db.execute('UPDATE jobs SET source=? WHERE id=?', (json.dumps({'submission': plan['source']}), job))
        store.db.execute("UPDATE plan_stages SET state='launching',job_id=?,launch_token=? WHERE plan_id=? AND name=?",
                         (job, token, plan['id'], stage['name']))
    return job, token


def finish_stage(store, plan, stage):
    job = stage['job']
    artifacts = []
    for filename in stage['definition']['artifacts']:
        path = Path(stage['definition']['cwd']) / filename
        root = Path(stage['definition']['cwd'])
        if root not in path.resolve().parents:
            evidence = dict(path=str(path), status='outside_checkout', sha256=None)
        else:
            evidence = digest_file(path)
        artifacts.append(evidence)
    state = job['state']
    reason = job['reason'] or ''
    if state == 'succeeded' and any(item['status'] != 'captured' for item in artifacts):
        state, reason = 'failed', 'required artifact missing or could not be fingerprinted; command exit remains 0'
    source = job['source']
    if state == 'succeeded' and stage['definition']['source_policy'] == 'require_unchanged':
        if source.get('changed_during_run') is not False:
            state, reason = 'failed', 'source changed during execution or identity could not be verified; command exit remains 0'
    evidence = dict(captured_at=time.time(), command_exit_code=job['exit_code'],
        source=source, artifacts=artifacts, log=digest_file(job['log_path']))
    store.db.execute('UPDATE plan_stages SET state=?,reason=?,evidence=? WHERE plan_id=? AND name=?',
                     (state, reason, json.dumps(evidence), plan['id'], stage['name']))


def cancel(store, plan):
    with store.transaction():
        current = get(store, plan)
        if current['state'] in PLAN_TERMINAL:
            return
        store.db.execute("UPDATE plans SET cancel_requested=1,state='cancelling' WHERE id=?", (plan,))
        store.db.execute("UPDATE plan_stages SET state='cancelled',reason='plan cancelled before submission' WHERE plan_id=? AND state='pending' AND job_id IS NULL", (plan,))
    from .runner import cancel_job
    for stage in current['stages']:
        if stage['job'] and stage['job']['state'] not in TERMINAL:
            cancel_job(store, stage['job_id'])
    if not alive(current['owner_pid'], current['owner_start']):
        # Explicit cancellation also authorizes monitoring the cancellation to
        # completion when its original controller is gone. No stage is retried.
        spawn(store, plan)


def control(plan_id):
    store = Store()
    children = {}
    cancelled = [False]
    previous = {}
    try:
        with store.transaction():
            plan = get(store, plan_id)
            if plan['state'] in PLAN_TERMINAL:
                return
            if alive(plan['owner_pid'], plan['owner_start']):
                raise ValueError('Another controller owns this plan')
            store.db.execute("UPDATE plans SET owner_pid=?,owner_start=?,state='running',reason='' WHERE id=?",
                (os.getpid(), identity(os.getpid()), plan_id))
        def on_signal(signum, frame):
            cancelled[0] = True
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, on_signal)
        while True:
            store.recover()
            plan = get(store, plan_id)
            if cancelled[0] and not plan['cancel_requested']:
                cancel(store, plan_id)
                plan = get(store, plan_id)
            for job, child in list(children.items()):
                if child.poll() is not None:
                    attached = store.db.execute('SELECT attached FROM plan_stages WHERE job_id=?', (job,)).fetchone()[0]
                    if not attached and store.get(job)['state'] not in TERMINAL:
                        store.finish(job, 'failed', child.returncode or 1, 'stage worker exited before attachment; not retried')
                    del children[job]
            for stage in plan['stages']:
                if stage['job'] and stage['state'] not in STAGE_TERMINAL:
                    if stage['job']['state'] in TERMINAL:
                        finish_stage(store, plan, stage)
                    else:
                        store.db.execute('UPDATE plan_stages SET state=? WHERE plan_id=? AND name=?',
                            (stage['job']['state'], plan_id, stage['name']))
            plan = get(store, plan_id)
            states = {s['name']: s['state'] for s in plan['stages']}
            active = sum(s['job'] is not None and s['state'] not in STAGE_TERMINAL for s in plan['stages'])
            for stage in plan['stages']:
                if stage['state'] != 'pending':
                    if plan['cancel_requested'] and stage['job'] and stage['job']['state'] not in TERMINAL:
                        from .runner import cancel_job
                        cancel_job(store, stage['job_id'])
                    continue
                dependencies = stage['definition']['after']
                failed = [d for d in dependencies if states[d] in STAGE_TERMINAL and states[d] != 'succeeded']
                always = stage['definition'].get('when','success')=='always'
                if plan['cancel_requested'] or (failed and not always):
                    state = 'cancelled' if plan['cancel_requested'] else 'skipped'
                    reason = 'plan cancelled before stage submission' if plan['cancel_requested'] else 'prerequisite did not succeed: '+', '.join(failed)
                    store.db.execute('UPDATE plan_stages SET state=?,reason=? WHERE plan_id=? AND name=?', (state, reason, plan_id, stage['name']))
                elif all(states[d] in STAGE_TERMINAL if always else states[d]=='succeeded' for d in dependencies) and active < plan['definition']['max_parallel']:
                    prepared = prepare(store, plan, stage)
                    if prepared is None:
                        continue
                    job, token = prepared
                    env = os.environ.copy()
                    env['AGENT_BUILDS_STATE'] = str(store.root)
                    env['PYTHONPATH'] = str(Path(__file__).resolve().parent.parent)+os.pathsep+env.get('PYTHONPATH', '')
                    try:
                        with (store.root/'logs'/('plan-'+plan_id+'.log')).open('ab') as log:
                            os.chmod(log.name, 0o600)
                            child = subprocess.Popen([sys.executable, '-m', 'agent_builds', 'stage-worker', job, token],
                                env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
                        children[job] = child
                    except OSError as error:
                        store.finish(job, 'failed', 2, 'stage worker could not launch: '+type(error).__name__)
                    active += 1
            plan = get(store, plan_id)
            if all(s['state'] in STAGE_TERMINAL for s in plan['stages']):
                state = 'cancelled' if plan['cancel_requested'] else 'succeeded' if all(s['state']=='succeeded' for s in plan['stages']) else 'failed'
                store.db.execute('UPDATE plans SET state=?,finished=?,reason=? WHERE id=?',
                    (state, time.time(), '' if state == 'succeeded' else 'Inspect per-stage results; commands are never automatically retried', plan_id))
                return
            time.sleep(.2)
    except BaseException as error:
        # A crash does not cancel or retry already dispatched jobs. Explicit resume
        # can monitor them and dispatch only stages that have never been submitted.
        if alive((get(store, plan_id))['owner_pid'], (get(store, plan_id))['owner_start']) and get(store, plan_id)['owner_pid'] == os.getpid():
            store.db.execute('UPDATE plans SET reason=? WHERE id=?', ('controller stopped: '+type(error).__name__+': '+str(error)[:300]+'; use plan resume to reconcile', plan_id))
        raise
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        store.close()


def wait(store, plan, timeout=None):
    if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
        raise ValueError('Plan wait timeout must be positive')
    deadline = None if timeout is None else time.monotonic()+timeout
    while True:
        with store.read_snapshot():
            current = get(store, plan)
        complete = current['state'] in PLAN_TERMINAL
        timed_out = not complete and deadline is not None and time.monotonic() >= deadline
        if complete or timed_out:
            return dict(complete=complete, timed_out=timed_out, plan=current, exit_code=current['exit_code'] if complete else 124)
        time.sleep(.2)


def export(store, plan):
    from . import __version__
    with store.read_snapshot():
        current = get(store, plan)
    return dict(schema='agent-builds-evidence-v1', application_version=__version__, exported_at=time.time(),
        plan=current, note='Evidence records live-checkout observations, not an immutable source snapshot. Missing or incomplete fingerprints are explicit. No environment is collected and no source contents are exported; literal command arguments and declared artifact paths are included.')


def write_export(path, report):
    path = Path(path).expanduser().absolute()
    temporary = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with temporary.open('x') as out:
            os.chmod(str(temporary), 0o600)
            json.dump(report, out, indent=2)
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        # Publish exclusively, so an evidence export cannot overwrite source,
        # artifacts, a queue database, or an earlier report by mistake.
        os.link(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()
