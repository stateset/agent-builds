"""Transactional activation of non-borrowable quick reserves."""
import time


def exists(store):
    return store.db is not None and store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='admission_policy'").fetchone()


def status(store, config=None):
    state = store.db.execute('SELECT requested,effective FROM admission_policy WHERE id=1').fetchone() if exists(store) else (0, 0)
    result = dict(requested=bool(state[0]), effective=bool(state[1]), pending=bool(state[0] and not state[1]),
                  blocking_jobs=[], blocking_job_details=[], blocking_controllers=[], requested_at=None, effective_at=None,
                  activation_condition='All legacy jobs and controllers must finish; existing heavy reservations must fit outside quick reserves. The next current-runner submit/claim activates protection atomically. No work is preempted.',
                  note='Protected reserves limit heavy reservations, not host processes. Shared locks, quick work, disk and RAM pressure can still delay checks.')
    if exists(store):
        columns={row[1] for row in store.db.execute('PRAGMA table_info(admission_policy)')}
        if {'requested_at','effective_at'}<=columns:
            stamp=store.db.execute('SELECT requested_at,effective_at FROM admission_policy WHERE id=1').fetchone()
            result.update(requested_at=stamp[0], effective_at=stamp[1])
    result['pending_seconds'] = max(0,time.time()-result['requested_at']) if result['pending'] and result['requested_at'] is not None else None
    if not result['pending']:
        return result
    config = config or store.config()
    rows = [dict(row) for row in store.db.execute("SELECT id,state,reserve_protocol,lane,slots,memory_gb,runner_version,created,started FROM jobs WHERE state IN ('queued','held','running','cancelling')")]
    blockers = [row['id'] for row in rows if row['reserve_protocol'] != 1]
    heavy = [row for row in rows if row['lane']=='heavy' and row['state'] in ('running','cancelling')]
    heavy_overflow = (sum(row['slots'] for row in heavy)>config['cpu_slots']-config['quick_reserve_slots'] or
            sum(row['memory_gb'] for row in heavy)>config['memory_gb']-config['quick_reserve_memory_gb'] or
            len(heavy)>config['max_running']-config['quick_reserve_jobs'])
    if heavy_overflow:
        blockers += [row['id'] for row in heavy]
    # Existing oversized heavy work must drain under the old admission rules.
    blockers += [row['id'] for row in rows if row['lane']=='heavy' and
        (row['slots']>config['cpu_slots']-config['quick_reserve_slots'] or
         row['memory_gb']>config['memory_gb']-config['quick_reserve_memory_gb'])]
    result['blocking_jobs'] = list(dict.fromkeys(blockers))
    for row in rows:
        if row['id'] not in result['blocking_jobs']:
            continue
        conditions=[]
        if row['reserve_protocol']!=1:
            conditions.append('legacy runner must finish (reserve_protocol='+str(row['reserve_protocol'])+')')
        if row['lane']=='heavy' and (row['slots']>config['cpu_slots']-config['quick_reserve_slots'] or row['memory_gb']>config['memory_gb']-config['quick_reserve_memory_gb']):
            conditions.append('individual heavy reservation exceeds protected pool')
        if row in heavy and heavy_overflow:
            conditions.append('aggregate heavy reservations must drain into protected pool')
        result['blocking_job_details'].append(dict(id=row['id'],state='queued' if row['state']=='held' else row['state'],
            runner_version=row['runner_version'] or 'unrecorded',conditions=conditions,
            age_seconds=max(0,time.time()-row['created']),elapsed_seconds=max(0,time.time()-row['started']) if row['started'] else 0))
    for table in ('plans','external_watches'):
        if store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():
            columns={row[1] for row in store.db.execute('PRAGMA table_info('+table+')')}
            unsupported='reserve_protocol!=1' if 'reserve_protocol' in columns else '1=1'
            version='runner_version' if 'runner_version' in columns else "'' AS runner_version"
            for row in store.db.execute("SELECT id,state,created,"+version+" FROM "+table+" WHERE state NOT IN ('succeeded','failed','cancelled','uncertain','timed_out','exhausted') AND "+unsupported):
                result['blocking_controllers'].append(dict(kind=table,id=row['id'],state=row['state'],
                    runner_version=row['runner_version'] or 'unrecorded',age_seconds=max(0,time.time()-row['created']),
                    condition='legacy controller must finish or be explicitly resumed by its owner after it stops; idle intervals can still dispatch future jobs'))
    return result


def activate(store, config=None):
    """Caller holds BEGIN IMMEDIATE; never preempt or invalidate existing work."""
    result = status(store, config)
    if result['pending'] and not result['blocking_jobs'] and not result['blocking_controllers']:
        store.db.execute('UPDATE admission_policy SET effective=1,effective_at=? WHERE id=1',(time.time(),))


def validate_job(config, lane, slots, memory):
    if config.get('protected_quick') and lane=='heavy' and (
            slots>config['cpu_slots']-config['quick_reserve_slots'] or
            memory>config['memory_gb']-config['quick_reserve_memory_gb']):
        raise ValueError('Heavy reservation exceeds capacity outside protected quick reserves; choose a measured smaller reservation or explicitly disable protected_quick')
