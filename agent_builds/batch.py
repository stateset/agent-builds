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
        if not isinstance(check,dict) or set(check)-{'name','command','jobs'} or not {'name','command'}<=set(check):
            raise ValueError('Each check needs name, literal command argv and optional jobs')
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
        item=dict(name=name,command=argv)
        if 'jobs' in check:
            if type(check['jobs']) is not int or not 1<=check['jobs']<=64:
                raise ValueError('Check jobs must be an integer from 1 to 64')
            item['jobs']=check['jobs']
        result.append(item)
    if any('jobs' in check for check in result):
        for check in result:
            check.setdefault('jobs',1)
    value = dict(version=1,checks=result,keep_going=value.get('keep_going',True))
    # Preserve the legacy encoding when parallelism is not requested.
    if parallel != 1:
        value['max_parallel'] = parallel
    if len(json.dumps(value).encode())>32768:
        raise ValueError('Batch definition exceeds 32 KiB')
    return value


def command(value):
    # Pin imports even when cwd contains another agent_builds package. A wheel
    # has no repository-root buildr script, so bootstrap from its package root.
    bootstrap = 'import sys; sys.path.insert(0,sys.argv.pop(1)); from agent_builds.__main__ import main; raise SystemExit(main())'
    return [sys.executable,'-I','-c',bootstrap,str(Path(__file__).resolve().parent.parent),
            'batch-worker',json.dumps(validate(value),separators=(',',':'))]


def worker_slots(definition, slots):
    parallel = definition.get('max_parallel', 1)
    if type(slots) is not int or slots < parallel:
        raise ValueError('Batch max_parallel must fit the CPU reservation; increase --jobs or lower max_parallel')
    if any('jobs' in check for check in definition['checks']):
        if max(check.get('jobs',1) for check in definition['checks'])>slots:
            raise ValueError('Each check CPU budget must fit the batch reservation')
        return None  # Explicit checks have individual hints, never a uniform one.
    return slots // min(parallel, len(definition['checks']))


def allocations(definition, slots):
    uniform=worker_slots(definition,slots)
    return [check.get('jobs',1) if uniform is None else uniform for check in definition['checks']]


def minimum_slots(definition):
    return max([definition.get('max_parallel',1)]+[check.get('jobs',1) for check in definition['checks']])


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
        budgets = allocations(definition,row['slots'])
        parallel = min(definition.get('max_parallel', 1), len(definition['checks']))
        from .budgets import worker_budget
        report = dict(schema='buildr-batch-v1',keep_going=definition['keep_going'],complete=False,
            max_parallel=parallel, slots_per_check=slots, memory_scope='one aggregate reservation; no per-check memory limit',
            cpu_allocation='explicit' if slots is None else 'uniform',
            checks=[dict(name=check['name'],state='pending',exit_code=None,cpu_slots=budgets[index],
                         worker_budget=worker_budget(check['command'],budgets[index])) for index,check in enumerate(definition['checks'])])
        def save():
            store.db.execute('UPDATE jobs SET batch=? WHERE id=?', (json.dumps(report),handle))
        def stop(signum, frame):
            stopped[0] = True
        def event(index, kind, **fields):
            print(json.dumps(dict(batch_check=definition['checks'][index]['name'],event=kind,**fields)),flush=True)
        for sig in (signal.SIGTERM,signal.SIGINT):
            previous[sig] = signal.signal(sig,stop)
        failed = False
        pending = list(range(len(definition['checks'])))
        save()
        while pending or active:
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
            while pending and len(active) < parallel:
                if stopped[0] or (failed and not definition['keep_going']):
                    for index in pending:
                        report['checks'][index].update(state='skipped',reason='batch stopped' if stopped[0] else 'earlier check failed')
                    pending.clear()
                    save()
                    break
                available=row['slots']-sum(budgets[index] for index in active)
                index=next((index for index in pending if budgets[index]<=available),None)
                if index is None:
                    break
                pending.remove(index)
                result = report['checks'][index]
                result.update(state='running',started=time.time())
                save()
                event(index, 'started',cpu_slots=budgets[index])
                try:
                    if stopped[0]:
                        result.update(state='skipped',reason='batch stopped before command launch')
                        save()
                        continue
                    # All siblings stay in the outer native worker's group:
                    # timeout/cancel/crash cleanup retains its existing boundary.
                    env=dict(os.environ,CARGO_BUILD_JOBS=str(budgets[index]),RUST_TEST_THREADS=str(budgets[index]))
                    child = subprocess.Popen(definition['checks'][index]['command'],env=env,
                        stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
                    active[index] = dict(child=child,eof=False)
                    selector.register(child.stdout,selectors.EVENT_READ,index)
                    # A group stop can arrive during Popen, before the new
                    # process exists. That child missed the group signal; stop
                    # it explicitly rather than waiting for the outer grace.
                    if stopped[0]:
                        child.terminate()
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
