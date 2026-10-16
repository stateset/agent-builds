"""Explicit SSH access to an independently scheduled dedicated build worker.

No source/credential synchronization, provisioning, or automatic retries.
The fixed remote command receives all variable data as JSON on stdin.
"""
import base64
import contextlib
import io
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
import uuid

from . import __version__
from .profiles import load
from .store import Store

PROTOCOL = 1


def rpc(worker, request):
    payload = dict(request, protocol=PROTOCOL, state_dir=worker['state_dir'])
    result = subprocess.run(['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
        '-o', 'ForwardAgent=no', '-o', 'ForwardX11=no', '-o', 'ClearAllForwardings=yes',
        '-o', 'PermitLocalCommand=no',
        '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=2',
        worker['host'], 'python3 -m agent_builds.remote'],
        input=json.dumps(payload), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=35)
    if result.returncode:
        raise ValueError('Worker connection failed (exit {}); remote outcome may be unknown'.format(result.returncode))
    try:
        response = json.loads(result.stdout)
    except ValueError:
        raise ValueError('Worker returned an invalid protocol response; remote outcome may be unknown')
    if response.get('protocol') != PROTOCOL or not response.get('ok'):
        raise ValueError(response.get('error', 'Worker protocol mismatch'))
    return response['result']


def journal(store, handle, record=None):
    if not re.fullmatch(r'[a-f0-9]{32}', handle):
        raise ValueError('Invalid remote dispatch handle')
    directory = store.root / 'dispatches'
    path = directory / (handle + '.json')
    if record is None:
        if not path.exists():
            raise ValueError('Unknown remote dispatch: ' + handle)
        return json.loads(path.read_text())
    directory.mkdir(mode=0o700, exist_ok=True)
    temporary = directory / (handle + '-' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x') as out:
            os.chmod(str(temporary), 0o600)
            json.dump(record, out, indent=2)
            out.flush()
            os.fsync(out.fileno())
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def dispatches(store):
    directory = store.root / 'dispatches'
    if not directory.is_dir():
        return []
    return sorted([json.loads(p.read_text()) for p in directory.glob('*.json')],
                  key=lambda r: r['created'], reverse=True)


def worker_for(store, name):
    worker = load(store).get('workers', {}).get(name)
    if not worker or not worker.get('dedicated_build_host'):
        raise ValueError('Configure an explicitly dedicated build worker first: ' + name)
    return worker


def submit(store, args):
    if getattr(args, 'locks', None):
        raise ValueError('Multiple locks currently require the local queue; no remote request was sent')
    worker = worker_for(store, args.worker)
    if not args.cwd.startswith('/'):
        raise ValueError('Remote checkout path must be absolute and already exist on the worker')
    command = list(args.command)
    if command and command[0] == '--':
        command.pop(0)
    if not command:
        raise ValueError('A remote command is required')
    handle = uuid.uuid4().hex
    record = dict(handle=handle, request_id=handle, worker=args.worker, worker_config=worker,
                  cwd=args.cwd, command=command, created=time.time(), state='sending', job_id=None)
    journal(store, handle, record)
    request = dict(action='submit', request_id=handle, cwd=args.cwd, command=command,
                   lane=args.lane, jobs=args.jobs, memory_gb=args.memory_gb,
                   timeout=args.timeout, min_free_gb=args.min_free_gb, scratch_gb=args.scratch_gb,
                   backend=args.backend, image=args.image)
    try:
        response = rpc(worker, request)
        record.update(response)
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        record.update(state='unknown', error=str(error))
    journal(store, handle, record)
    return record


def reconcile(store, handle):
    record = journal(store, handle)
    response = rpc(record['worker_config'], dict(action='lookup', request_id=record['request_id']))
    if response.get('job'):
        record.update(job_id=response['job']['id'], state=response['job']['state'], job=response['job'])
        record.pop('error', None)
    else:
        record['state'] = 'unknown'
        record['error'] = 'Worker has no matching job; inspect before submitting new work. No retry was attempted.'
    journal(store, handle, record)
    return record


def submission_args(request, config):
    """Validate every launch field before consuming a new request token."""
    from .__main__ import parser
    from .protection import validate_job
    if request.get('locks') or request.get('resource_keys'):
        raise ValueError('Remote protocol 1 does not support multiple locks; nothing was submitted')
    lane, backend = request.get('lane'), request.get('backend')
    if backend not in ('local', 'docker') or lane not in ('quick', 'heavy'):
        raise ValueError('Invalid worker backend or lane')
    command, cwd = request.get('command'), request.get('cwd')
    if (not isinstance(command, list) or not 1 <= len(command) <= 128 or
            any(not isinstance(item, str) or '\0' in item for item in command) or not command[0] or
            sum(len(item.encode())+1 for item in command) > 32768):
        raise ValueError('Remote command must be a bounded literal argv list')
    if not isinstance(cwd, str) or '\0' in cwd or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
        raise ValueError('Remote checkout must already exist at an absolute path')
    from .execution_policy import check_cwd
    check_cwd(cwd)
    args = parser().parse_args(['run', '--', 'placeholder'])
    args.cwd, args.command, args.lane, args.backend = cwd, command, lane, backend
    args.detach, args.events_json = True, True
    for field, default in (('jobs', config[lane+'_jobs']), ('memory_gb', 6 if lane=='heavy' else 2),
            ('timeout', config['timeout_seconds']), ('min_free_gb', 0), ('scratch_gb', 0)):
        value = request.get(field)
        value = default if value is None else value
        try:
            finite = type(value) in (int, float) and math.isfinite(value)
        except OverflowError:
            finite = False
        if (not finite or
                value < 0 or field not in ('min_free_gb','scratch_gb') and value == 0 or
                field == 'jobs' and type(value) is not int):
            raise ValueError('Invalid remote '+field)
        setattr(args, field, value)
    if args.jobs > config['cpu_slots'] or args.memory_gb > config['memory_gb']:
        raise ValueError('Remote reservation exceeds current budget')
    image = request.get('image')
    if image is not None and not isinstance(image, str):
        raise ValueError('Invalid Docker image')
    args.image = image or config['docker_image']
    if not isinstance(args.image, str) or '\0' in args.image or backend=='docker' and (not args.image or ',' in cwd):
        raise ValueError('Invalid Docker image or bind path')
    validate_job(dict(config, protected_quick=config['protected_quick_requested']), lane, args.jobs, args.memory_gb)
    return args


def serve(request):
    if not isinstance(request, dict) or request.get('protocol') != PROTOCOL:
        raise ValueError('Incompatible worker protocol')
    root = request.get('state_dir', '')
    if not isinstance(root, str) or not root.startswith('/'):
        raise ValueError('Worker state directory must be absolute')
    action = request.get('action')
    store = Store(root, read_only=action not in ('submit', 'cancel'))
    try:
        if action == 'ping':
            from .metrics import snapshot
            return dict(version=__version__, machine=snapshot(str(Path.home())), config=store.config())
        if action == 'submit':
            if request.get('locks') or request.get('resource_keys'):
                raise ValueError('Remote protocol 1 does not support multiple locks; nothing was submitted')
            token = request.get('request_id', '')
            if not re.fullmatch(r'[a-f0-9]{32}', token):
                raise ValueError('Invalid submission token')
            store.db.execute('CREATE TABLE IF NOT EXISTS remote_requests (token TEXT PRIMARY KEY, created REAL NOT NULL)')
            duplicate = store.db.execute('SELECT token FROM remote_requests WHERE token=?', (token,)).fetchone()
            if duplicate:
                return dict(state='unknown', request_id=token, note='Token already submitted; use lookup. No retry was attempted.')
            args = submission_args(request, store.config())
            with store.transaction():
                duplicate = store.db.execute('SELECT token FROM remote_requests WHERE token=?', (token,)).fetchone()
                if duplicate:
                    return dict(state='unknown', request_id=token, note='Token already submitted; use lookup. No retry was attempted.')
                args = submission_args(request, store.config())
                store.db.execute('INSERT INTO remote_requests VALUES (?,?)', (token, time.time()))
            from .runner import execute
            old_root = os.environ.get('AGENT_BUILDS_STATE')
            old_token = os.environ.get('AGENT_BUILDS_REMOTE_REQUEST_ID')
            old_nested = os.environ.pop('AGENT_BUILDS_JOB_ID', None)
            output = io.StringIO()
            try:
                os.environ['AGENT_BUILDS_STATE'] = root
                os.environ['AGENT_BUILDS_REMOTE_REQUEST_ID'] = token
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    execute(args)
            finally:
                for key, value in [('AGENT_BUILDS_STATE', old_root), ('AGENT_BUILDS_REMOTE_REQUEST_ID', old_token), ('AGENT_BUILDS_JOB_ID', old_nested)]:
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
            receipt = json.loads(output.getvalue().splitlines()[-1])
            return dict(job_id=receipt['job_id'], state='submitted', request_id=token)
        if action == 'lookup':
            token = request.get('request_id', '')
            if not re.fullmatch(r'[a-f0-9]{32}', token):
                raise ValueError('Invalid lookup token')
            if store.db is not None:
                for row in store.db.execute('SELECT * FROM jobs ORDER BY created DESC'):
                    if json.loads(row['herdr']).get('remote_request_id') == token:
                        return dict(job=store.describe(dict(row)))
            return dict(job=None)
        if action == 'status':
            with store.read_snapshot():
                return dict(counts=store.counts(), jobs=store.rows(limit=50, active=True))
        if action in ('get', 'logs', 'cancel'):
            job = request.get('job', '')
            row = store.get(job)
            if action == 'cancel':
                from .runner import cancel_job
                cancel_job(store, job)
                return store.get(job)
            if action == 'get':
                return row
            offset = request.get('offset', 0)
            if type(offset) is not int or offset < 0:
                raise ValueError('Invalid log offset')
            path = store.log_path(job)
            chunk = b''
            if path.exists():
                with path.open('rb') as log:
                    log.seek(offset)
                    chunk = log.read(65536)
            return dict(data=base64.b64encode(chunk).decode('ascii'), offset=offset + len(chunk), state=row['state'])
        raise ValueError('Unknown worker action')
    finally:
        store.close()


def main():
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ValueError('Worker request exceeds 64 KiB')
        result = serve(json.loads(raw))
        print(json.dumps(dict(protocol=PROTOCOL, ok=True, result=result)))
        return 0
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as error:
        print(json.dumps(dict(protocol=PROTOCOL, ok=False, error=str(error))))
        return 0


if __name__ == '__main__':
    sys.exit(main())
