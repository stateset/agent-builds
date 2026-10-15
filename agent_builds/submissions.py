"""Durable, idempotent submissions shared by MCP and external watches."""
import hashlib
import json
import os
import re
from pathlib import Path
import time

from . import plans
from .runner import submit_detached
from .store import Store

def request_row(store, key):
    if store.db is None or not store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mcp_requests'").fetchone():
        raise ValueError('Unknown submission request key')
    row = store.db.execute('SELECT * FROM mcp_requests WHERE request_key=?', (key,)).fetchone()
    if row is None:
        raise ValueError('Unknown submission request key')
    return dict(row)


def request_result(store, row):
    result = json.loads(row['result'])
    if row['state'] != 'submitted' and result.get('receipt_path'):
        path = Path(result['receipt_path'])
        if path.parent == store.root and path.name.startswith('receipt-mcp-'):
            try:
                if path.stat().st_size <= 8192:
                    receipt = json.loads(path.read_text())
                    job = store.get(receipt['job_id'])
                    result.update(job_id=job['id'], log_path=job['log_path'], recovered_from_receipt=True)
            except (OSError, ValueError, KeyError, TypeError):
                pass
    confirmed = row['state'] == 'submitted' or result.get('recovered_from_receipt') is True
    return dict(request_key=row['request_key'], kind=row['kind'],
                submission_state='submitted' if confirmed else 'uncertain',
                replay_safe=True, result=result,
                note='Keep this handle. Do not create a new request key to retry uncertain work. Disconnecting does not cancel it.')


def reserve(store, kind, arguments, initial):
    digest = payload_digest(kind, arguments)
    key = arguments['request_key']
    with store.transaction():
        previous = store.db.execute('SELECT * FROM mcp_requests WHERE request_key=?', (key,)).fetchone()
        if previous:
            if previous['payload_sha256'] != digest or previous['kind'] != kind:
                raise ValueError('Request key is already bound to different arguments; nothing was submitted')
            return False
        store.db.execute('INSERT INTO mcp_requests VALUES (?,?,?,?,?,?)',
                         (key, kind, digest, time.time(), 'in_flight', json.dumps(initial)))
    return True


def record(store, key, state, result):
    store.db.execute('UPDATE mcp_requests SET state=?,result=? WHERE request_key=?', (state, json.dumps(result), key))


def payload_digest(kind, arguments):
    return hashlib.sha256(json.dumps(dict(kind=kind, arguments=arguments), sort_keys=True, allow_nan=False).encode()).hexdigest()


def submit(name, arguments):
    if name not in ('buildr_submit', 'buildr_plan_submit', 'buildr_watch_submit'):
        raise ValueError('Unsupported durable submission kind')
    if not isinstance(arguments.get('request_key'), str) or not re.fullmatch(r'[A-Za-z0-9._:-]{8,128}', arguments['request_key']):
        raise ValueError('Request key must be 8 to 128 letters, numbers, dots, underscores, colons or hyphens')
    if os.environ.get('AGENT_BUILDS_JOB_ID'):
        raise ValueError('Already inside a queued job. MCP preserves the nested-submission guard.')
    from .__main__ import parser
    with_reader = Store(read_only=True)
    try:
        # Reconnects must work even if budgets or checkout paths changed after
        # submission. Compare the original arguments before validating new work.
        try:
            previous = request_row(with_reader, arguments['request_key'])
        except ValueError:
            previous = None
        if previous is not None:
            if previous['payload_sha256'] != payload_digest(name, arguments) or previous['kind'] != name:
                raise ValueError('Request key is already bound to different arguments; nothing was submitted')
            return request_result(with_reader, previous)
        root = Path(arguments['cwd']).expanduser()
        if not root.is_absolute() or not root.is_dir():
            raise ValueError('cwd must be an existing absolute directory')
        config = with_reader.config()
        if name == 'buildr_watch_submit':
            from . import external
            definition = external.validate({key:value for key,value in arguments.items() if key!='request_key'}, config)
        elif name == 'buildr_plan_submit':
            definition = plans.validate(arguments['manifest'], root, config)
        else:
            if not arguments['command'][0] or sum(len(x) for x in arguments['command']) > 32768:
                raise ValueError('Command must have an executable and at most 32 KiB of arguments')
            lane = arguments.get('lane', 'quick')
            jobs = arguments.get('jobs', config[lane+'_jobs'])
            memory = arguments.get('memory_gb', 6 if lane == 'heavy' else 2)
            if jobs > config['cpu_slots'] or memory > config['memory_gb']:
                raise ValueError('Requested reservation exceeds the current machine budget')
            if arguments.get('backend') == 'docker' and not arguments.get('image', config['docker_image']):
                raise ValueError('Docker jobs require an explicit/configured image')
            args = parser().parse_args(['run', '--', 'placeholder'])
            for key in ('cwd', 'lane', 'jobs', 'memory_gb', 'timeout', 'backend', 'image', 'resource_key', 'locks', 'min_free_gb', 'scratch_gb'):
                if key in arguments:
                    setattr(args, key, arguments[key])
            from .locks import normalize
            normalize(args.resource_key or 'default', args.locks)
            args.lane = lane
            args.jobs, args.memory_gb = jobs, memory
            args.track_source = arguments.get('track_source', True)
            args.command = arguments['command']
    finally:
        with_reader.close()
    store = Store()
    try:
        key = arguments['request_key']
        receipt = store.root/('receipt-mcp-'+hashlib.sha256(key.encode()).hexdigest()+'.json')
        result = dict(receipt_path=str(receipt)) if name == 'buildr_submit' else {}
        if reserve(store, name, arguments, result):
            try:
                if name == 'buildr_submit':
                    result.update(submit_detached(args, receipt=receipt, retain_receipt=True))
                elif name == 'buildr_watch_submit':
                    result['watch_id'] = external.create(store, definition)
                    record(store, key, 'in_flight', result)
                    external.spawn(store, result['watch_id'])
                else:
                    result['plan_id'] = plans.create(store, definition)
                    # Save the handle before spawning, so a server crash cannot
                    # cause a reconnect to create the same plan a second time.
                    record(store, key, 'in_flight', result)
                    plans.spawn(store, result['plan_id'])
                record(store, key, 'submitted', result)
            except Exception as error:
                result.update(error=str(error)[:1000], automatic_retry=False)
                record(store, key, 'uncertain', result)
        return request_result(store, request_row(store, key))
    finally:
        store.close()


