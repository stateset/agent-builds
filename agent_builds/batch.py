"""Short foreground checks under one reservation, with independent results."""
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import time

from .profiles import name as valid_name


def validate(value):
    if not isinstance(value, dict) or set(value)-{'version','checks','keep_going'} or value.get('version',1)!=1:
        raise ValueError('Batch requires version 1, checks and optional keep_going')
    if type(value.get('keep_going', True)) is not bool:
        raise ValueError('keep_going must be a boolean')
    checks = value.get('checks')
    if not isinstance(checks,list) or not 1<=len(checks)<=32:
        raise ValueError('Batch requires 1 to 32 foreground checks')
    names, result = set(), []
    for check in checks:
        if not isinstance(check,dict) or set(check)!={'name','command'}:
            raise ValueError('Each check needs only name and literal command argv')
        name = check['name']
        if not isinstance(name,str):
            raise ValueError('Check name must be text')
        valid_name(name)
        if name in names:
            raise ValueError('Duplicate check name')
        names.add(name)
        argv = check['command']
        if not isinstance(argv,list) or not argv or not argv[0] or len(argv)>128 or any(
                not isinstance(arg,str) or '\0' in arg or len(arg)>4096 for arg in argv):
            raise ValueError('Check command must be a bounded nonempty argv array')
        result.append(dict(name=name,command=argv))
    value = dict(version=1,checks=result,keep_going=value.get('keep_going',True))
    if len(json.dumps(value).encode())>32768:
        raise ValueError('Batch definition exceeds 32 KiB')
    return value


def command(value):
    # Absolute launcher pins imports even when the selected checkout contains a
    # package named agent_builds. The actual check cwd remains the user's cwd.
    return [sys.executable,str(Path(__file__).resolve().parent.parent/'buildr'),
            'batch-worker',json.dumps(validate(value),separators=(',',':'))]


def run(encoded):
    from .store import Store
    definition = validate(json.loads(encoded))
    handle = os.environ.get('AGENT_BUILDS_JOB_ID')
    if not handle:
        raise ValueError('Batch workers require an existing queue reservation')
    store = Store()
    stopped, previous, child = [False], {}, None
    try:
        row = store.get(handle)
        if row['state']!='running' or row['backend']!='local' or row['argv'][-2:]!=['batch-worker',encoded]:
            raise ValueError('Batch definition does not match this running local job')
        from .budgets import worker_budget
        report = dict(schema='buildr-batch-v1',keep_going=definition['keep_going'],complete=False,
            checks=[dict(name=check['name'],state='pending',exit_code=None,worker_budget=worker_budget(check['command'],row['slots'])) for check in definition['checks']])
        def save():
            store.db.execute('UPDATE jobs SET batch=? WHERE id=?', (json.dumps(report),handle))
        def stop(signum, frame):
            stopped[0] = True
        for sig in (signal.SIGTERM,signal.SIGINT):
            previous[sig] = signal.signal(sig,stop)
        save()
        code = 0
        for check, result in zip(definition['checks'],report['checks']):
            if stopped[0] or (code and not definition['keep_going']):
                result.update(state='skipped',reason='batch stopped' if stopped[0] else 'earlier check failed')
                save()
                continue
            result.update(state='running',started=time.time())
            save()
            print(json.dumps(dict(batch_check=check['name'],event='started')),flush=True)
            try:
                # Share the outer worker's process group: cancellation/timeout
                # terminates all checks and descendants using existing recovery.
                child = subprocess.Popen(check['command'],stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
                with selectors.DefaultSelector() as selector:
                    selector.register(child.stdout,selectors.EVENT_READ)
                    while selector.get_map() or child.poll() is None:
                        for key, _ in selector.select(.1):
                            chunk=os.read(key.fileobj.fileno(),65536)
                            if chunk:
                                sys.stdout.buffer.write(chunk)
                                sys.stdout.buffer.flush()
                            else:
                                selector.unregister(key.fileobj)
                native=child.wait()
                child.stdout.close()
                native = native if native>=0 else 128-native
                result.update(state='succeeded' if native==0 else 'failed',exit_code=native,finished=time.time())
            except OSError as error:
                result.update(state='failed',exit_code=127,finished=time.time(),reason=type(error).__name__)
            code = code or result['exit_code']
            save()
            print(json.dumps(dict(batch_check=check['name'],event='finished',exit_code=result['exit_code'])),flush=True)
        report.update(complete=True,exit_code=130 if stopped[0] else code,finished=time.time())
        save()
        return report['exit_code']
    finally:
        for sig, handler in previous.items():
            signal.signal(sig,handler)
        store.close()


def summary(row):
    report = row.get('batch') or {}
    if not report:
        return {}
    result = dict(report)
    result.update(job_state=row['state'],job_exit_code=row.get('exit_code'))
    result['worker_exit_code'] = result.pop('exit_code',result.get('worker_exit_code'))
    result['checks'] = [dict(check) for check in report['checks']]
    # A killed/lost worker cannot attest to a partially recorded check outcome.
    if not report.get('complete') and row['state'] in ('succeeded','failed','cancelled','lost'):
        for check in result['checks']:
            if check['state']=='running':
                check.update(state='unknown',reason='Worker ended before recording a native result')
            elif check['state']=='pending':
                check.update(state='skipped',reason='Batch did not reach this check')
        result['interrupted'] = True
    return result
