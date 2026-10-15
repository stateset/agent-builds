"""Read-only latency targets and reservation evidence; no automatic tuning."""
import json
import math
import time

from . import advice, protection


def report(store, hours=24, target=30, repo=None):
    if type(hours) not in (int,float) or not math.isfinite(hours) or not 0<hours<=8760:
        raise ValueError('hours must be positive and at most 8760')
    if type(target) not in (int,float) or not math.isfinite(target) or not 0<target<=3600:
        raise ValueError('target must be positive and at most 3600 seconds')
    with store.read_snapshot():
        active=store.rows(active=True,repo=repo,limit=2000)
        counts=store.counts(active=True,repo=repo)
        config=store.config()
        policy=protection.status(store,config)
        where,params=store.filters(repo=repo)
        where+=(' AND ' if where else ' WHERE ')+"finished>=? AND state IN ('succeeded','failed','cancelled','lost')"
        params+=(time.time()-hours*3600,)
        total=store.db.execute('SELECT COUNT(*) FROM jobs'+where,params).fetchone()[0] if store.db else 0
        rows=[store.describe(dict(row)) for row in store.db.execute('SELECT * FROM jobs'+where+' ORDER BY finished DESC LIMIT 2000',params)] if store.db else []
    cohorts={}
    for row in rows:
        if row['started'] is not None:
            cohorts.setdefault((row.get('policy_version') or 'unrecorded',row['runner_version'],row['lane'],json.dumps(row['admission'],sort_keys=True)),[]).append(row)
    output=[]
    for (version,runner,lane,admission),jobs in sorted(cohorts.items()):
        waits=sorted(job['wait_seconds'] for job in jobs)
        blockers={}
        for job in jobs:
            for category,seconds in job['wait_breakdown']['seconds'].items():
                blockers[category]=blockers.get(category,0)+seconds
        output.append(dict(policy_version=version,runner_version=runner,lane=lane,admission=json.loads(admission),jobs=len(jobs),
            wait_p50_seconds=waits[math.ceil(len(waits)*.5)-1],wait_p95_seconds=waits[math.ceil(len(waits)*.95)-1],
            within_target=sum(wait<=target for wait in waits),target_met_fraction=sum(wait<=target for wait in waits)/len(waits),
            runtime_seconds=sum(job['elapsed_seconds'] for job in jobs),wait_seconds=sum(waits),blocker_seconds=blockers))
    evidence=advice.report(store,repo,hours,limit=2000)
    return dict(schema='buildr-latency-v1',hours=hours,target_seconds=target,config=config,protection=policy,
        active_counts=counts,active_sampled=len(active),active_truncated=sum(counts.values())>len(active),
        overdue_queued=[dict(job_id=row['id'],lane=row['lane'],wait_seconds=row['wait_seconds'],reason=row['reason'],
                            slots=row['slots'],memory_gb=row['memory_gb']) for row in active if row['state']=='queued' and row['wait_seconds']>target],
        history_matching=total,history_sampled=len(rows),history_truncated=total>len(rows),cohorts=output,
        reservation_trials=[finding for finding in evidence['findings'] if finding.get('suggested_trial')],
        changes_applied=False,note='Target is an observation goal, not an ETA. Completed cohorts exclude still-waiting jobs; overdue queued work is reported separately. Cohorts are not matched causal comparisons. Trials require validation on representative inputs; no reservations are changed automatically.')
