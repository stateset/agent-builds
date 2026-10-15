"""Read-only projections before submission; never reserve capacity or execute argv."""
import json
from itertools import product
import math
import os
from pathlib import Path
import shutil
import time

from . import batch, locks
from .budgets import worker_budget
from .diagnostics import capacity
from .metrics import snapshot
from .protection import validate_job
from .runner import resource_key
from .scheduler import POLICY_VERSION, choose

MAX_ACTIVE = 200
MAX_CASES = 25
NOTE = ('Projection of the loaded scheduler over one bounded queue snapshot and current host observations. '
        'It is not admission, a reservation, an ETA or evidence that a smaller budget is safe. '
        'Each case is an alternative for one job, not simultaneous reservations. '
        'Queue state, policy and host pressure can change before submission. No submitted command is executed; '
        'no jobs, request keys, files, settings or reservations are changed.')


def _number(value, name, zero=False):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or (not zero and value == 0):
        raise ValueError(name+' must be a finite '+('nonnegative' if zero else 'positive')+' number')
    return value


def _active(store):
    if store.db is None:
        return [], 0, dict(jobs=0,cpu_slots=0,memory_gb=0,scratch_gb=0)
    columns = {r[1] for r in store.db.execute('PRAGMA table_info(jobs)')}
    # Read only scheduling fields: large command/source/batch/log payloads are
    # never loaded just to project a reservation.
    fields = ['id','state','created','cwd','lane','slots','memory_gb','resource_key','cancel_requested']
    fields += [name if name in columns else default+' AS '+name for name,default in
               (('scratch_gb','0'),('min_free_gb','0'),('lock_protocol','0'),('resource_keys',"'[]'"))]
    where = "state IN ('queued','held','running','cancelling')"
    total = store.db.execute('SELECT COUNT(*) FROM jobs WHERE '+where).fetchone()[0]
    rows = [dict(r) for r in store.db.execute('SELECT '+','.join(fields)+' FROM jobs WHERE '+where+
                                            ' ORDER BY created,id LIMIT ?', (MAX_ACTIVE,))]
    scratch = 'scratch_gb' if 'scratch_gb' in columns else '0'
    sums = store.db.execute('SELECT COUNT(*),COALESCE(SUM(slots),0),COALESCE(SUM(memory_gb),0),'
                           'COALESCE(SUM('+scratch+"),0) FROM jobs WHERE state IN ('running','cancelling')").fetchone()
    return rows, total, dict(zip(('jobs','cpu_slots','memory_gb','scratch_gb'),sums))


def _existing_parent(path):
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def _protocol_compatible(store):
    if store.db is None:
        return True
    if not store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='admission_policy'").fetchone():
        return False
    columns={r[1] for r in store.db.execute('PRAGMA table_info(admission_policy)')}
    return ('required_protocol' in columns and
            store.db.execute('SELECT required_protocol FROM admission_policy WHERE id=1').fetchone()[0]==2)


def _worker_warnings(argv, slots, label='command'):
    budget=worker_budget(argv,slots)
    result=[]
    for kind in ('cargo_build','rust_tests'):
        setting=budget[kind]
        if setting.get('exceeds_reservation'):
            result.append('{}: {} requests {} workers for {} CPU worker slots'.format(
                label,setting['flag'],setting['configured_value'],slots))
    return result


def report(store, arguments):
    allowed = {'cwd','lane','jobs','memory_gb','timeout','backend','image','resource_key','locks',
               'min_free_gb','scratch_gb','command','manifest','compare_jobs','compare_memory_gb'}
    if not isinstance(arguments,dict) or set(arguments)-allowed:
        raise ValueError('Unsupported preflight fields')
    if not isinstance(arguments.get('cwd'),str) or not arguments['cwd'] or '\0' in arguments['cwd']:
        raise ValueError('An existing working directory is required')
    cwd = Path(arguments['cwd']).expanduser().resolve()
    if not cwd.is_dir():
        raise ValueError('Working directory must already exist')
    manifest = batch.validate(arguments['manifest']) if 'manifest' in arguments else None
    if manifest is not None and 'command' in arguments:
        raise ValueError('Choose a command or a batch manifest, not both')
    argv = arguments.get('command')
    if argv is not None and (not isinstance(argv,list) or not 1<=len(argv)<=128 or not argv[0] or any(
            not isinstance(a,str) or '\0' in a or len(a)>4096 for a in argv) or sum(map(len,argv))>32768):
        raise ValueError('Command must be a bounded nonempty literal argv array')
    lane, backend = arguments.get('lane','quick'), arguments.get('backend','local')
    if lane not in ('quick','heavy') or backend not in ('local','docker'):
        raise ValueError('Unsupported lane or backend')
    if manifest is not None and (lane!='quick' or backend!='local'):
        raise ValueError('Batches require the quick lane and local backend')
    comparisons = arguments.get('compare_jobs',[])
    if not isinstance(comparisons,list) or len(comparisons)>8 or any(type(x) is not int or not 1<=x<=64 for x in comparisons):
        raise ValueError('compare_jobs must contain at most eight CPU counts from 1 to 64')
    memory_comparisons = arguments.get('compare_memory_gb',[])
    if not isinstance(memory_comparisons,list) or len(memory_comparisons)>8:
        raise ValueError('compare_memory_gb must contain at most eight positive finite RAM reservations')
    for value in memory_comparisons:
        _number(value,'compare_memory_gb')
    primary = arguments.get('resource_key')
    if primary is None:
        primary = resource_key(cwd)
    resources = locks.normalize(primary, arguments.get('locks',[]))
    with store.read_snapshot():
        config = store.config()
        active, total, totals = _active(store)
        compatible = _protocol_compatible(store)
    slots = arguments.get('jobs', 1 if manifest is not None else config[lane+'_jobs'])
    if type(slots) is not int or not 1<=slots<=64:
        raise ValueError('jobs must be an integer from 1 to 64')
    requested_memory = _number(arguments.get('memory_gb',1 if manifest is not None else 6 if lane=='heavy' else 2),'memory_gb')
    cpu_counts = list(dict.fromkeys([slots]+comparisons))
    memory_counts = list(dict.fromkeys([requested_memory]+memory_comparisons))
    if len(cpu_counts)*len(memory_counts)>MAX_CASES:
        raise ValueError('CPU/RAM comparisons exceed {} unique combinations; request fewer alternatives'.format(MAX_CASES))
    timeout = _number(arguments.get('timeout',60 if manifest is not None else config['timeout_seconds']),'timeout')
    minimum = _number(arguments.get('min_free_gb',0),'min_free_gb',True)
    scratch = _number(arguments.get('scratch_gb',0),'scratch_gb',True)
    image = arguments.get('image',config['docker_image'])
    if not isinstance(image,str) or '\0' in image or len(image)>4096:
        raise ValueError('image must be text of at most 4096 characters without NUL')
    image=image or config['docker_image']
    try:
        machine = snapshot(cwd)
    except OSError as error:
        machine = dict(available_gb=None,error=type(error).__name__)
    probe = _existing_parent(store.root)
    disk_by_device = {}
    probe_errors = []
    try:
        state_free = shutil.disk_usage(probe).free/1024**3
    except OSError as error:
        state_free = None
        probe_errors.append(dict(scope='state',error=type(error).__name__))
    def disk_free(row):
        try:
            device = os.stat(row['cwd']).st_dev
            if device not in disk_by_device:
                disk_by_device[device] = shutil.disk_usage(row['cwd']).free/1024**3
            value = disk_by_device[device]
            return min(value,state_free) if state_free is not None else None
        except OSError:
            return None
    running = [r for r in active if r['state'] in ('running','cancelling')]
    queued = [r for r in active if r['state'] in ('queued','held') and not r['cancel_requested']]
    pending_policy = bool(config.get('protected_quick_requested') and not config.get('protected_quick'))
    complete = total<=MAX_ACTIVE
    now = time.time()
    cases = []
    for count,memory in product(cpu_counts,memory_counts):
        row = dict(id='~preflight',state='queued',created=now,cwd=str(cwd),lane=lane,slots=count,memory_gb=memory,
                   resource_key=primary,resource_keys=resources,lock_protocol=1,cancel_requested=0,
                   min_free_gb=minimum,scratch_gb=scratch)
        errors = []
        if count>config['cpu_slots'] or memory>config['memory_gb']:
            errors.append('reservation exceeds total machine budget')
        if backend=='docker' and (not image or ',' in str(cwd)):
            errors.append('Docker requires an image and a bind path without commas')
        if manifest is not None and timeout>300:
            errors.append('batch timeout exceeds 300 seconds')
        try:
            validate_job(dict(config,protected_quick=config.get('protected_quick_requested',False)),lane,count,memory)
        except ValueError as error:
            errors.append(str(error))
        per_check = None
        check_budgets = None
        if manifest is not None:
            try:
                per_check = batch.worker_slots(manifest,count)
                check_budgets = batch.allocations(manifest,count)
            except ValueError as error:
                errors.append(str(error))
        arithmetic = capacity(row,running,config,now) if complete else None
        free = disk_free(row)
        required = max(config['min_free_gb'],minimum)+scratch+totals['scratch_gb']
        ram = machine.get('available_gb')
        facts = list(arithmetic['blocking_dimensions']) if arithmetic else []
        if free is not None and free<required:
            facts.append('disk headroom')
        if ram is not None and ram<memory+config['min_available_gb']:
            facts.append('available RAM headroom')
        candidates = locks.eligible(queued+[row])
        legacy = next((r for r in queued if not r['lock_protocol'] and locks.overlaps(row,r)),None) if len(resources)>1 else None
        older = next((r for r in candidates if (r['created'],r['id'])<(now,row['id']) and locks.overlaps(row,r)),None)
        busy = next((r for r in running if locks.overlaps(row,r)),None)
        blockers = [dict(kind=kind,job_id=r['id']) for kind,r in
                    (('legacy lock',legacy),('earlier queued lock',older),('running lock',busy)) if r]
        facts += [r['kind'] for r in blockers]
        selected, fits, reason = None, None, ''
        if errors:
            fits,reason = False,errors[0]
        elif config['paused']:
            fits,reason = False,'queue paused'
        elif not complete or pending_policy or not compatible or free is None or ram is None:
            reason = ('active snapshot truncated' if not complete else 'policy upgrade pending' if not compatible else 'policy activation pending' if pending_policy
                      else 'host headroom unavailable')
        elif blockers:
            fits,reason = False,blockers[0]['kind']
        else:
            choice, skipped = choose(candidates,running,config,ram,
                                     lambda candidate: disk_free(candidate) or 0,now=now)
            selected = 'candidate' if choice is not None and choice['id']==row['id'] else choice['id'] if choice else None
            fits = selected=='candidate' and not skipped.get(row['id'])
            reason = '' if fits else skipped.get(row['id'], 'another queued job selected' if selected else 'waiting for scheduling capacity')
        case = dict(jobs=count,memory_gb=memory,submission_acceptable=not errors,submission_issues=errors,
            fits_now=fits,reason=reason,selected=selected,blocking_facts=facts,lock_blockers=blockers,
            reservation_arithmetic=arithmetic,
            disk=dict(available_gb=free,required_gb=required,deficit_gb=max(0,required-free) if free is not None else None),
            ram=dict(available_gb=ram,required_gb=memory+config['min_available_gb'],
                     deficit_gb=max(0,memory+config['min_available_gb']-ram) if ram is not None else None))
        case['worker_warnings']=[]
        if manifest is not None:
            case['batch'] = dict(requested_max_parallel=manifest.get('max_parallel',1),
                effective_max_parallel=min(manifest.get('max_parallel',1),len(manifest['checks'])),
                slots_per_check=per_check,check_slots=dict(zip((c['name'] for c in manifest['checks']),check_budgets)) if check_budgets else None,
                checks=len(manifest['checks']),memory_scope='one aggregate reservation')
            if check_budgets is not None:
                for check,budget in zip(manifest['checks'],check_budgets):
                    case['worker_warnings'] += _worker_warnings(check['command'],budget,check['name'])
        elif argv is not None:
            case['worker_budget'] = worker_budget(argv,count)
            case['worker_warnings'] = _worker_warnings(argv,count)
        cases.append(case)
    return dict(schema='buildr-preflight-v1',observed_at=now,initialized=store.db is not None,
        current_policy_version=POLICY_VERSION,config=config,machine=machine,
        request=dict(cwd=str(cwd),lane=lane,backend=backend,memory_gb=requested_memory,timeout=timeout,
                     resource_keys=resources,min_free_gb=minimum,scratch_gb=scratch,command_provided=argv is not None),
        comparisons=dict(jobs=cpu_counts,memory_gb=memory_counts,cases=len(cases),max_cases=MAX_CASES),
        active_matching=total,active_sampled=len(active),snapshot_complete=complete,policy_pending=pending_policy,
        protocol_compatible=compatible,
        global_reservations=totals,state_probe_path=str(probe),state_probe_errors=probe_errors,cases=cases,
        changes_applied=False,commands_submitted=False,note=NOTE)


def lines(result):
    yield 'BUILDR preflight | read only | no submission or reservation'
    for case in result['cases']:
        state = 'fits snapshot' if case['fits_now'] is True else 'blocked in snapshot' if case['fits_now'] is False else 'unknown'
        yield '{} CPU slots / {:g} GiB RAM: {} | {}'.format(case['jobs'],case['memory_gb'],state,case['reason'])
        for issue in case['submission_issues']:
            yield '  Submission issue: '+issue
        for fact in case['blocking_facts']:
            yield '  '+fact
        for warning in case['worker_warnings']:
            yield '  Worker warning: '+warning
        disk = case['disk']
        if disk['available_gb'] is not None:
            yield '  Disk {:.3f} GiB free / {:.3f} required; deficit {:.3f}'.format(disk['available_gb'],disk['required_gb'],disk['deficit_gb'])
        ram = case['ram']
        if ram['available_gb'] is not None:
            yield '  RAM {:.3f} GiB available / {:.3f} required; deficit {:.3f}'.format(ram['available_gb'],ram['required_gb'],ram['deficit_gb'])
        if 'batch' in case:
            yield '  Batch: {} checks, parallel limit {}, CPU hints {}'.format(
                case['batch']['checks'],case['batch']['effective_max_parallel'],
                'individual' if case['batch']['check_slots'] and case['batch']['slots_per_check'] is None else case['batch']['slots_per_check'])
            if case['batch']['slots_per_check'] is None and case['batch']['check_slots']:
                yield '  Per-check CPU hints: '+str(case['batch']['check_slots'])
    yield result['note']
