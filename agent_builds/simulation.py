"""Deterministic reservation-model replay. No historical command is executed."""
import math
import time

from .locks import keys
from .scheduler import POLICY_VERSION, capacity_reason, choose
from .store import DEFAULTS

NOTE = ('Hypothetical reservation model, not a forecast or measured speedup. Recorded runtimes are fixed; '
        'external load, RAM/disk pressure, I/O contention, hidden locks and runtime changes from concurrency are excluded. '
        'No historical commands are executed and no queue state or settings are changed.')


def replay(workload, config, backfill):
    arrivals = sorted(workload, key=lambda r: (r['created'], r['id']))
    pending, running, completed = [], [], []
    now = arrivals[0]['created'] if arrivals else 0
    admissions = []
    while arrivals or pending or running:
        while arrivals and arrivals[0]['created'] <= now:
            pending.append(arrivals.pop(0))
        remaining = []
        for row in running:
            if row['ends'] <= now:
                completed.append(row)
            else:
                remaining.append(row)
        running = remaining
        while pending:
            selected, skipped = choose(pending, running, config, 1e9, lambda row: 1e9, allow_backfill=backfill, now=now)
            if selected is None or selected['id'] in skipped:
                break
            if capacity_reason(selected, running, config, now):
                raise AssertionError('Simulation exceeded reservation budget')
            pending = [r for r in pending if r['id'] != selected['id']]
            row = dict(selected, starts=now, ends=now+selected['duration'])
            running.append(row)
            admissions.append(dict(id=row['id'], lane=row['lane'], started=now, wait_seconds=now-row['created']))
        events = [row['created'] for row in arrivals]+[row['ends'] for row in running]
        events += [row['created']+config['aging_seconds'] for row in pending if row['created']+config['aging_seconds'] > now]
        if not events:
            break
        now = min(events)
    lanes = []
    for lane in ('quick', 'heavy'):
        waits = sorted(r['starts']-r['created'] for r in completed if r['lane']==lane)
        lanes.append(dict(lane=lane, completed=len(waits),
                          wait_p95_seconds=waits[math.ceil(len(waits)*.95)-1] if waits else None,
                          max_wait_seconds=max(waits) if waits else None))
    return dict(policy_version=POLICY_VERSION if backfill else 'current-locks-no-backfill-model', completed=len(completed),
                unadmitted_job_ids=[r['id'] for r in pending], lanes=lanes, admissions=admissions,
                makespan_seconds=max((r['ends'] for r in completed), default=now)-min((r['created'] for r in workload), default=now))


def compare(workload, config):
    if len(workload)>500:
        raise ValueError('Simulation is limited to 500 jobs')
    workload = sorted(workload, key=lambda r: (r['created'], r['id']))
    return dict(schema='agent-builds-simulation-v1', jobs=len(workload),
                baseline=replay(workload, config, False), candidate=replay(workload, config, True),
                changes_applied=False, note=NOTE)


def history(store, repo=None, hours=24, limit=100):
    if type(hours) not in (int, float) or not math.isfinite(hours) or not 0<hours<=8760:
        raise ValueError('History hours must be positive and at most 8760')
    if type(limit) is not int or not 1<=limit<=500:
        raise ValueError('Simulation history limit must be between 1 and 500')
    cutoff = time.time()-hours*3600
    with store.read_snapshot():
        config = store.config()
        where, params = store.filters(repo=repo)
        where += (' AND ' if where else ' WHERE ')+"state IN ('succeeded','failed') AND started IS NOT NULL AND finished>=?"
        params += (cutoff,)
        count = store.db.execute('SELECT COUNT(*) FROM jobs'+where, params).fetchone()[0] if store.db else 0
        records = store.db.execute('SELECT * FROM jobs'+where+' ORDER BY finished DESC,id DESC LIMIT ?', params+(limit,)).fetchall() if store.db else []
    workload = []
    skipped = []
    for row in records:
        row = dict(row)
        duration = row['finished']-row['started']
        if duration<0 or row['created']>row['started']:
            skipped.append(row['id'])
            continue
        workload.append({key:row[key] for key in ('id','created','lane','slots','memory_gb','resource_key')})
        workload[-1].update(resource_keys=keys(row), lock_protocol=1, cancel_requested=0, duration=max(.001,duration), min_free_gb=0, scratch_gb=0)
    result = compare(workload,config)
    result.update(history_matching=count, history_sampled=len(records), history_truncated=count>len(records),
                  invalid_record_ids=skipped, hours=hours, since=cutoff, repository=str(repo) if repo else None)
    return result


def fixture():
    """Two heavy slots are occupied; an aged heavy head needs one of them."""
    config = dict(DEFAULTS)
    config.update(quick_reserve_slots=0,quick_reserve_jobs=0,quick_reserve_memory_gb=0)
    def row(name, created, lane, duration, slots=1):
        return dict(id=name, created=created, lane=lane, duration=duration, slots=slots,
                    memory_gb=1, resource_key=name, cancel_requested=0, min_free_gb=0, scratch_gb=0)
    return [row('heavy-a',0,'heavy',180),row('heavy-b',.01,'heavy',240),
            row('aged-head',.02,'heavy',20),row('short-check',130,'quick',1,2)], config
