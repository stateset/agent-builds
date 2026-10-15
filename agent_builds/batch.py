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
    if (not isinstance(value, dict) or set(value)-{'version','checks','keep_going','max_parallel'} or
            type(value.get('version',1)) is not int or value.get('version',1)!=1):
        raise ValueError('Batch requires version 1, checks and optional keep_going/max_parallel')
    if type(value.get('keep_going', True)) is not bool:
        raise ValueError('keep_going must be a boolean')
    parallel = value.get('max_parallel', 1)
    if type(parallel) is not int or not 1 <= parallel <= 16:
        raise ValueError('Batch max_parallel must be an integer from 1 to 16')
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
    # Preserve the legacy encoding when parallelism is not requested.
    if parallel != 1:
        value['max_parallel'] = parallel
    if len(json.dumps(value).encode())>32768:
        raise ValueError('Batch definition exceeds 32 KiB')
    return value


def command(value):
    # Absolute launcher pins imports even when the selected checkout contains a
    # package named agent_builds. The actual check cwd remains the user's cwd.
    return [sys.executable,str(Path(__file__).resolve().parent.parent/'buildr'),
            'batch-worker',json.dumps(validate(value),separators=(',',':'))]


def worker_slots(definition, slots):
    parallel = definition.get('max_parallel', 1)
    if type(slots) is not int or slots < parallel:
        raise ValueError('Batch max_parallel must fit the CPU reservation; increase --jobs or lower max_parallel')
    return slots // min(parallel, len(definition['checks']))


def run(encoded):
    from .store import Store
    definition = validate(json.loads(encoded))
    handle = os.environ.get('AGENT_BUILDS_JOB_ID')
    if not handle:
        raise ValueError('Batch workers require an existing queue reservation')
    store = Store()
    stopped, previous = [False], {}
    active = {}
    selector = selectors.DefaultSelector()
    try:
        row = store.get(handle)
        if row['state']!='running' or row['backend']!='local' or row['argv'][-2:]!=['batch-worker',encoded]:
            raise ValueError('Batch definition does not match this running local job')
        slots = worker_slots(definition, row['slots'])
        parallel = min(definition.get('max_parallel', 1), len(definition['checks']))
        from .budgets import worker_budget
        report = dict(schema='buildr-batch-v1',keep_going=definition['keep_going'],complete=False,
            max_parallel=parallel, slots_per_check=slots, memory_scope='one aggregate reservation; no per-check memory limit',
            checks=[dict(name=check['name'],state='pending',exit_code=None,
                         worker_budget=worker_budget(check['command'],slots)) for check in definition['checks']])
        def save():
            store.db.execute('UPDATE jobs SET batch=? WHERE id=?', (json.dumps(report),handle))
        def stop(signum, frame):
            stopped[0] = True
        def event(index, kind, **fields):
            print(json.dumps(dict(batch_check=definition['checks'][index]['name'],event=kind,**fields)),flush=True)
        for sig in (signal.SIGTERM,signal.SIGINT):
            previous[sig] = signal.signal(sig,stop)
        env = dict(os.environ, CARGO_BUILD_JOBS=str(slots), RUST_TEST_THREADS=str(slots))
        failed = False
        pending = 0
        save()
        while pending < len(definition['checks']) or active:
            # Reap/record finished checks before admitting additional work. A
            # failure stops future launches, while already-running siblings finish.
            for index, item in list(active.items()):
                native = item['child'].poll()
                if native is not None and native != 0:
                    failed = True
                if native is None or not item['eof']:
                    continue
                code = native if native >= 0 else 128-native
                report['checks'][index].update(state='succeeded' if code==0 else 'failed',
                                               exit_code=code,finished=time.time())
                item['child'].stdout.close()
                del active[index]
                save()
                event(index, 'finished', exit_code=code)
            while pending < len(definition['checks']) and len(active) < parallel:
                if stopped[0] or (failed and not definition['keep_going']):
                    for result in report['checks'][pending:]:
                        result.update(state='skipped',reason='batch stopped' if stopped[0] else 'earlier check failed')
                    pending = len(definition['checks'])
                    save()
                    break
                index = pending
                pending += 1
                result = report['checks'][index]
                result.update(state='running',started=time.time())
                save()
                event(index, 'started')
                try:
                    # All siblings stay in the outer native worker's group:
                    # timeout/cancel/crash cleanup retains its existing boundary.
                    child = subprocess.Popen(definition['checks'][index]['command'],env=env,
                        stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
                    active[index] = dict(child=child,eof=False)
                    selector.register(child.stdout,selectors.EVENT_READ,index)
                except OSError as error:
                    failed = True
                    result.update(state='failed',exit_code=127,finished=time.time(),reason=type(error).__name__)
                    save()
                    event(index, 'finished', exit_code=127)
            for key, unused in selector.select(.05 if active else 0):
                chunk = os.read(key.fileobj.fileno(),65536)
                if chunk:
                    if parallel > 1:
                        sys.stdout.buffer.write(('\n[buildr check: '+definition['checks'][key.data]['name']+']\n').encode())
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
                else:
                    selector.unregister(key.fileobj)
                    active[key.data]['eof'] = True
            if active and not selector.get_map():
                time.sleep(.01)
        # Deterministic aggregate: first failed native result in manifest order,
        # independent of timing. Keep all individual outcomes in the report.
        code = next((item['exit_code'] for item in report['checks'] if item['exit_code']), 0)
        report.update(complete=True,exit_code=130 if stopped[0] else code,finished=time.time())
        save()
        return report['exit_code']
    finally:
        selector.close()
        for item in active.values():
            item['child'].stdout.close()
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
