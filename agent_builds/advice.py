"""Read-only, evidence-based suggestions; never tune or execute commands."""
import math
from pathlib import Path
import time

from .waits import category
from .scheduler import POLICY_VERSION

SCHEMA = 'agent-builds-advice-v1'
MIN_SUCCESSES = 3
LONG_QUICK_SECONDS = 60
WAIT_SECONDS = 60
MAX_FINDINGS = 50


def report(store, repo=None, hours=24, limit=200, policy=None):
    if type(hours) not in (int, float) or not math.isfinite(hours) or not 0 < hours <= 8760:
        raise ValueError('History hours must be positive and at most 8760')
    if type(limit) is not int or not 1 <= limit <= 2000:
        raise ValueError('History limit must be between 1 and 2000')
    if policy is not None and (not isinstance(policy,str) or not policy.strip() or len(policy)>100):
        raise ValueError('Policy filter must be nonempty text of at most 100 characters')
    policy = POLICY_VERSION if policy=='current' else policy
    now = time.time()
    cutoff = now-hours*3600
    repository = str(Path(repo).expanduser().resolve()) if repo else None
    with store.read_snapshot():
        config = store.config()
        active = store.rows(limit=limit, active=True, repo=repository)
        active_counts = store.counts(active=True, repo=repository)
        history, matching = [], 0
        if store.db is not None:
            columns={r[1] for r in store.db.execute('PRAGMA table_info(jobs)')}
            expression="COALESCE(NULLIF(policy_version,''),'unrecorded')" if 'policy_version' in columns else "'unrecorded'"
            if policy:
                active_where,active_params=store.filters(active=True,repo=repository)
                active_where+=' AND '+expression+'=?'
                active_params+=(policy,)
                active=[store.describe(dict(r)) for r in store.db.execute('SELECT * FROM jobs'+active_where+' ORDER BY created,id LIMIT ?',active_params+(limit,))]
                active_counts=dict.fromkeys(active_counts,0)
                for state, count in store.db.execute('SELECT state,COUNT(*) FROM jobs'+active_where+' GROUP BY state',active_params):
                    key = 'queued' if state == 'held' else state
                    active_counts[key] = active_counts.get(key, 0) + count
            where, params = store.filters(repo=repository)
            where += (' AND ' if where else ' WHERE ') + "state IN ('succeeded','failed','cancelled','lost') AND finished>=?"
            params += (cutoff,)
            if policy:
                where+=' AND '+expression+'=?'
                params+=(policy,)
            matching = store.db.execute('SELECT COUNT(*) FROM jobs'+where, params).fetchone()[0]
            history = [store.describe(dict(row)) for row in store.db.execute(
                'SELECT * FROM jobs'+where+' ORDER BY finished DESC,id DESC LIMIT ?', params+(limit,))]
    findings = []
    def add(code, title, action, evidence, trial=None):
        findings.append(dict(code=code, title=title, action=action, evidence=evidence, suggested_trial=trial))
    wait_totals = {}
    for row in history:
        for kind, seconds in row['wait_breakdown']['seconds'].items():
            wait_totals[kind] = wait_totals.get(kind, 0)+seconds
    wait_total = sum(row['wait_seconds'] for row in history)
    observed = sum(row['wait_breakdown']['observed_seconds'] for row in history)
    coverage = observed/wait_total if wait_total else 0
    cohorts={}
    for row in history:
        key=(row.get('policy_version') or 'unrecorded',row['runner_version'],
             'recorded' if row['wait_breakdown']['recorded'] else 'unrecorded',row['lane'])
        cohorts.setdefault(key,[]).append(row)
    cohort_reports=[]
    for (runner_policy,version,instrumentation,lane),jobs in sorted(cohorts.items()):
        total=sum(j['wait_seconds'] for j in jobs)
        measured=sum(j['wait_breakdown']['observed_seconds'] for j in jobs)
        waits=sorted(j['wait_seconds'] for j in jobs)
        cohort_reports.append(dict(policy_version=runner_policy,runner_version=version,wait_instrumentation=instrumentation,
            lane=lane,jobs=len(jobs),wait_seconds=total,observed_seconds=measured,coverage=measured/total if total else None,
            wait_p95_seconds=waits[math.ceil(len(waits)*.95)-1]))
    if wait_total >= WAIT_SECONDS and coverage < .5:
        add('limited_wait_coverage', 'Most historical waiting has no recorded blocker',
            'Collect more wait observations before changing global scheduling limits.',
            dict(wait_seconds=wait_total, observed_seconds=observed, coverage=coverage,
                 recorded_jobs=sum(row['wait_breakdown']['recorded'] for row in history), jobs=len(history)))
    current = {}
    stale = []
    for row in active:
        if row['state'] != 'queued':
            continue
        if row['slots'] > config['cpu_slots'] or row['memory_gb'] > config['memory_gb']:
            add('job_exceeds_budget', 'A queued job exceeds the current total budget',
                'Review this job before explicitly cancelling and resubmitting it with a compatible reservation. It is not retried automatically.',
                dict(job_id=row['id'], slots=row['slots'], memory_gb=row['memory_gb'],
                     total_slots=config['cpu_slots'], total_memory_gb=config['memory_gb']))
        measured_at = row['wait_context'].get('measured_at')
        if not measured_at or not 0 <= now-measured_at <= 5:
            if row['wait_seconds'] >= WAIT_SECONDS:
                stale.append(row['id'])
            continue
        if row['wait_seconds'] >= WAIT_SECONDS:
            current.setdefault(category(row['reason']), []).append(row)
    actions = dict(lock_compatibility='Wait for the named legacy job to settle; use the installed runner for new work. Multiple locks are not acquired partially.', repository_lock='Use a verification plan to sequence stages that share a checkout or output. Use independent output keys only when the outputs are actually independent.',
                   priority='Inspect the aged head and its blocker before changing aging or quick reserves; running work is not preempted.',
                   disk='Inspect agent-builds disk for the affected checkout and have the cache owner review inactive data. No caches are deleted automatically.',
                   ram_headroom='Inspect host memory use and the requested reservation before changing headroom.',
                   quick_reserve='Review quick-lane demand and measured resource use before trying a different quick reserve.',
                   paused='Review why the queue was paused before explicitly resuming admission.')
    for kind, jobs in sorted(current.items()):
        add('current_wait_'+kind, '{} queued job(s) have waited at least 60s; current blocker: {}'.format(len(jobs), kind),
            actions.get(kind, 'Review the contributing running reservations and measured job history before changing machine budgets.'),
            dict(category=kind, jobs=len(jobs), job_ids=[r['id'] for r in jobs[:10]],
                 policy_versions=sorted({r.get('policy_version') or 'unrecorded' for r in jobs}),
                 runner_versions=sorted({r['runner_version'] for r in jobs}),
                 longest_wait_seconds=max(r['wait_seconds'] for r in jobs),
                 note='Total wait is not time attributed to this current blocker.'))
    if stale:
        add('stale_wait_observation', 'Some queued jobs lack a recent blocker observation',
            'Inspect their runner logs. A stale sample does not establish that a host process has exited; do not retry an uncertain job.',
            dict(jobs=len(stale), job_ids=stale[:10]))
    groups = {}
    for row in history:
        # Compare only successful executions with identical reservations/argv.
        if row['state'] != 'succeeded' or row['exit_code'] != 0 or row['started'] is None:
            continue
        key = (row['cwd'], tuple(sorted(row['resource_keys'])), row['command'], row['backend'], row['lane'], row['slots'], row['memory_gb'])
        groups.setdefault(key, []).append(row)
    for _, jobs in sorted(groups.items()):
        if len(jobs) < MIN_SUCCESSES:
            continue
        first = jobs[0]
        common = dict(cwd=first['cwd'], resource_key=first['resource_key'], resource_keys=first['resource_keys'], command_display=first['command_display'],
                      backend=first['backend'], lane=first['lane'], slots=first['slots'], memory_gb=first['memory_gb'],
                      successful_jobs=len(jobs), job_ids=[r['id'] for r in jobs[:10]])
        durations = sorted(row['elapsed_seconds'] for row in jobs)
        median = durations[(len(durations)-1)//2]
        if first['lane'] == 'quick' and median >= LONG_QUICK_SECONDS:
            add('long_quick_command', 'A repeated quick-lane command has a median runtime of {:.1f}s'.format(median),
                'Trial this command in the heavy lane or split a short prerequisite check from the long stage in a verification plan.',
                dict(common, median_runtime_seconds=median), dict(lane='heavy'))
        if first['lane']=='quick' and first['slots']>1 and median<=10:
            add('tiny_check_trial','A repeated quick check has a median runtime of {:.1f}s'.format(median),
                'Trial one CPU slot while retaining the memory reservation, and compare runtime. Review explicit tool worker flags too; the queue does not rewrite them.',
                dict(common,median_runtime_seconds=median),dict(jobs=1))
        measured = [r for r in jobs if r['backend']=='local' and r['usage'].get('source')=='local_process_tree'
                    and r['usage'].get('complete') and r['usage'].get('samples', 0) >= 3
                    and r['usage'].get('sampled_peak_rss_gb', 0) > 0 and r['elapsed_seconds'] >= 1]
        if len(measured) < MIN_SUCCESSES:
            continue
        peak = max(r['usage']['sampled_peak_rss_gb'] for r in measured)
        memory_trial = max(.25, math.ceil(peak*1.3*4)/4)
        if memory_trial <= first['memory_gb']*.7:
            add('memory_trial', 'Repeated measured runs used substantially less sampled RSS than reserved',
                'Test a smaller memory reservation on a representative run before saving a task profile. Sampled RSS can miss peaks and local reservations are not hard limits.',
                dict(common, measured_jobs=len(measured), sampled_peak_rss_gb=peak), dict(memory_gb=memory_trial))
        reserved = sum(r['slots']*r['elapsed_seconds'] for r in measured)
        cpu = sum(r['usage'].get('cpu_seconds', 0) for r in measured)
        if first['slots'] > 1 and reserved > 0 and cpu/reserved < .25:
            add('low_average_cpu', 'Repeated measured runs averaged less than 25% of their CPU reservation',
                'Trial one fewer CPU slot and compare runtime before saving a task profile. Average CPU does not measure peak concurrency or guarantee a faster queue.',
                dict(common, measured_jobs=len(measured), observed_cpu_seconds=cpu, reserved_cpu_seconds=reserved,
                     observed_to_reserved_cpu_ratio=cpu/reserved), dict(jobs=first['slots']-1))
    priority = {'job_exceeds_budget': 0, 'stale_wait_observation': 2, 'limited_wait_coverage': 3}
    findings.sort(key=lambda f: (priority.get(f['code'], 1 if f['code'].startswith('current_wait_') else 4),
                                 f['code'], f['evidence'].get('cwd', ''), f['evidence'].get('command_display', '')))
    return dict(schema=SCHEMA, observed_at=now, repository=repository, hours=hours, since=cutoff,
                history_limit=limit, history_matching=matching, history_sampled=len(history),
                history_truncated=matching>len(history), active_counts=active_counts,
                active_sampled=len(active), active_truncated=sum(active_counts.values())>len(active),
                policy_filter=policy,current_policy_version=POLICY_VERSION,cohorts=cohort_reports,
                wait_breakdown=dict(seconds=wait_totals, total_seconds=wait_total, observed_seconds=observed, coverage=coverage),
                findings=findings[:MAX_FINDINGS], findings_total=len(findings), findings_truncated=len(findings)>MAX_FINDINGS,
                changes_applied=False,
                note='Advisory only. At least three matching successful executions are required for command trials. Historical commands are never executed; settings and jobs are never changed.')


def lines(result):
    output = ['QUEUE ADVISOR | {} completed jobs sampled / {} matching | last {}h'.format(
        result['history_sampled'], result['history_matching'], result['hours']),
        'Wait blocker coverage: {:.0%} | {} active jobs shown'.format(result['wait_breakdown']['coverage'], result['active_sampled'])]
    output+=['Policy filter: '+(result['policy_filter'] or 'all (cohorts below)')]
    for cohort in result['cohorts']:
        coverage='n/a' if cohort['coverage'] is None else '{:.0%}'.format(cohort['coverage'])
        output+=['{} | runner {} | {} | {} | {} jobs | blocker coverage {}'.format(cohort['policy_version'],
            cohort['runner_version'],cohort['wait_instrumentation'],cohort['lane'],cohort['jobs'],coverage)]
    if result['history_truncated'] or result['active_truncated']:
        output += ['Sample limit reached; use --limit (up to 2000) to inspect more history/active jobs.']
    for finding in result['findings']:
        output += ['', finding['code']+': '+finding['title']]
        evidence = finding['evidence']
        if evidence.get('command_display'):
            output += [evidence['cwd']+' | '+evidence['command_display']]
        output += ['Evidence jobs: '+', '.join(evidence.get('job_ids', [evidence['job_id']] if 'job_id' in evidence else [])),
                   finding['action']]
        if finding['suggested_trial']:
            output += ['Suggested trial: '+str(finding['suggested_trial'])]
    if not result['findings']:
        output += ['No findings meet the evidence thresholds in this sample.']
    if result['findings_truncated']:
        output += ['Showing {} of {} findings.'.format(len(result['findings']), result['findings_total'])]
    return output+['', result['note']]
