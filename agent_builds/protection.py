"""Transactional activation of non-borrowable quick reserves."""


def exists(store):
    return store.db is not None and store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='admission_policy'").fetchone()


def status(store, config=None):
    state = store.db.execute('SELECT requested,effective FROM admission_policy WHERE id=1').fetchone() if exists(store) else (0, 0)
    result = dict(requested=bool(state[0]), effective=bool(state[1]), pending=bool(state[0] and not state[1]),
                  blocking_jobs=[], blocking_controllers=[], note='Protected reserves limit heavy reservations, not host processes. Shared locks, quick work, disk and RAM pressure can still delay checks.')
    if not result['pending']:
        return result
    config = config or store.config()
    rows = [dict(row) for row in store.db.execute("SELECT id,state,reserve_protocol,lane,slots,memory_gb FROM jobs WHERE state IN ('queued','held','running','cancelling')")]
    blockers = [row['id'] for row in rows if row['reserve_protocol'] != 1]
    heavy = [row for row in rows if row['lane']=='heavy' and row['state'] in ('running','cancelling')]
    if (sum(row['slots'] for row in heavy)>config['cpu_slots']-config['quick_reserve_slots'] or
            sum(row['memory_gb'] for row in heavy)>config['memory_gb']-config['quick_reserve_memory_gb'] or
            len(heavy)>config['max_running']-config['quick_reserve_jobs']):
        blockers += [row['id'] for row in heavy]
    # Existing oversized heavy work must drain under the old admission rules.
    blockers += [row['id'] for row in rows if row['lane']=='heavy' and
        (row['slots']>config['cpu_slots']-config['quick_reserve_slots'] or
         row['memory_gb']>config['memory_gb']-config['quick_reserve_memory_gb'])]
    result['blocking_jobs'] = list(dict.fromkeys(blockers))
    for table in ('plans','external_watches'):
        if store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():
            columns={row[1] for row in store.db.execute('PRAGMA table_info('+table+')')}
            unsupported='reserve_protocol!=1' if 'reserve_protocol' in columns else '1=1'
            for row in store.db.execute("SELECT id FROM "+table+" WHERE state NOT IN ('succeeded','failed','cancelled','uncertain','timed_out','exhausted') AND "+unsupported):
                result['blocking_controllers'].append(dict(kind=table, id=row[0]))
    return result


def activate(store, config=None):
    """Caller holds BEGIN IMMEDIATE; never preempt or invalidate existing work."""
    result = status(store, config)
    if result['pending'] and not result['blocking_jobs'] and not result['blocking_controllers']:
        store.db.execute('UPDATE admission_policy SET effective=1 WHERE id=1')


def validate_job(config, lane, slots, memory):
    if config.get('protected_quick') and lane=='heavy' and (
            slots>config['cpu_slots']-config['quick_reserve_slots'] or
            memory>config['memory_gb']-config['quick_reserve_memory_gb']):
        raise ValueError('Heavy reservation exceeds capacity outside protected quick reserves; choose a measured smaller reservation or explicitly disable protected_quick')
