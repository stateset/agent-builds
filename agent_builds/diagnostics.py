"""Bounded, read-only operational explanations; never predict admission or reclaim work."""
import os
import shlex
import shutil
import time

from . import protection, waits
from .metrics import snapshot

NOTE = ('Reasons and blocker links are the latest runner observations, not an ETA or a new '
        'admission decision. Host processes outside Buildr are not controlled. No settings, '
        'reservations, jobs or files are changed. Cache ownership and last use require owner review.')


def brief(row):
    fields = ('id', 'state', 'cwd', 'backend', 'lane', 'slots', 'memory_gb', 'exit_code',
              'wait_seconds', 'elapsed_seconds', 'runner_version', 'policy_version', 'log_path')
    result = {key: row[key] for key in fields}
    result.update(command_display=row['command_display'][:240],
                  command_truncated=len(row['command_display'])>240, reason=(row['reason'] or '')[:400])
    context = row['wait_context']
    result['observation_at'] = context.get('measured_at') if row['state']=='queued' else None
    result['blocking_job_id'] = context.get('blocking_job', {}).get('id') if row['state']=='queued' else None
    return result


def history(store, limit, repo=None, before=None):
    """Terminal history with stable keyset pagination, including equal timestamps."""
    where, params = store.filters(repo=repo)
    where += (' AND ' if where else ' WHERE ') + "state IN ('succeeded','failed','cancelled','lost')"
    if before:
        cursor = store.get(before)
        if cursor['state'] not in ('succeeded','failed','cancelled','lost'):
            raise ValueError('History cursor must identify a completed job')
        where += ' AND (created<? OR (created=? AND id<?))'
        params += (cursor['created'], cursor['created'], cursor['id'])
    if store.db is None:
        return []
    return [store.describe(dict(row)) for row in store.db.execute(
        'SELECT * FROM jobs'+where+' ORDER BY created DESC,id DESC LIMIT ?', params+(limit,))]


def policy_summary(store, limit=20):
    state = protection.status(store)
    result = dict(state)
    result['blocking_job_count'] = len(state['blocking_jobs'])
    result['blocking_controller_count'] = len(state['blocking_controllers'])
    for key in ('blocking_jobs', 'blocking_controllers', 'blocking_job_details'):
        if key in result:
            result[key] = result[key][:limit]
    result['truncated'] = (result['blocking_job_count']>limit or result['blocking_controller_count']>limit)
    return result


def observation(row, now):
    context = row['wait_context']
    stamp = context.get('measured_at') if row['state']=='queued' else None
    age = max(0, now-stamp) if stamp is not None else None
    return dict(at=stamp, age_seconds=age, stale=age is None or age>waits.MAX_GAP_SECONDS,
                category=waits.category(row['reason'] or '') if stamp is not None else 'unobserved')


def headroom(store, row, running, config, now):
    required = max(config['min_free_gb'], row.get('min_free_gb', 0))+row.get('scratch_gb', 0)+sum(r['scratch_gb'] for r in running)
    disk = dict(measured_at=now, required_gb=required, available_gb=None, deficit_gb=None,
                job_filesystem=row['cwd'], state_filesystem=str(store.root),
                components=dict(machine_floor_gb=config['min_free_gb'],job_floor_gb=row.get('min_free_gb',0),
                    job_scratch_gb=row.get('scratch_gb',0),running_scratch_gb=sum(r['scratch_gb'] for r in running)),
                contributing_jobs=[dict(job_id=r['id'],scratch_gb=r['scratch_gb']) for r in running if r['scratch_gb']][:20],
                contributors_truncated=sum(bool(r['scratch_gb']) for r in running)>20,
                formula='max(machine floor, job floor) + job scratch + all running scratch; scratch is reserved, not measured growth')
    try:
        # A missing/inaccessible path is unknown; never create it during inspection.
        free = min(shutil.disk_usage(row['cwd']).free, shutil.disk_usage(store.root).free)/1024**3
        disk.update(available_gb=free, deficit_gb=max(0, required-free))
    except OSError as error:
        disk['error'] = type(error).__name__
    return disk


def chain(store, row, now, depth=8):
    result, seen = [], set()
    for unused in range(depth):
        if row['id'] in seen:
            return dict(jobs=result, end='cycle', complete=False)
        seen.add(row['id'])
        item = brief(row)
        item['observation'] = observation(row, now)
        result.append(item)
        if row['state']!='queued':
            return dict(jobs=result, end='running' if row['state'] in ('running','cancelling') else 'settled_since_observation', complete=True)
        blocker = item['blocking_job_id']
        if not blocker:
            return dict(jobs=result, end='observed_head' if not item['observation']['stale'] else 'unobserved_or_stale', complete=True)
        try:
            row = store.get(blocker)
        except ValueError:
            return dict(jobs=result, end='missing_blocker', complete=False)
    return dict(jobs=result, end='depth_limit', complete=False)


def report(store, job=None, repo=None, limit=10):
    if type(limit) is not int or not 1<=limit<=50:
        raise ValueError('limit must be between 1 and 50')
    now = time.time()
    with store.read_snapshot():
        config = store.config()
        counts = store.counts(active=True, repo=repo)
        rows = [store.get(job)] if job else store.rows(active=True, repo=repo, limit=limit)
        # All global reservations contribute, even when the job list is filtered.
        running = [dict(row) for row in store.db.execute("SELECT id,slots,memory_gb,scratch_gb,lane FROM jobs WHERE state IN ('running','cancelling')")] if store.db else []
        policy = policy_summary(store)
        output = []
        for row in rows:
            item = brief(row)
            item['observation'] = observation(row, now)
            item['blocker_chain'] = chain(store, row, now)
            if row['state']=='queued':
                item['disk'] = headroom(store, row, running, config, now)
                item['inspect_cache_command'] = shlex.join(['buildr','disk','--repo',row['cwd'],'--json'])
                item['cleanup_status'] = 'owner_review_required; no automatic deletion'
            output.append(item)
    machine = snapshot(os.getcwd())
    return dict(schema='buildr-diagnostics-v1', initialized=store.db is not None, measured_at=now,
        machine=machine, config=config, protection=policy, active_counts=counts,
        global_reservations=dict(cpu_slots=sum(r['slots'] for r in running), memory_gb=sum(r['memory_gb'] for r in running),
                                 jobs=len(running), scratch_gb=sum(r['scratch_gb'] for r in running)),
        jobs=output, jobs_truncated=not job and sum(counts.values())>len(rows),
        repository=repo, changes_applied=False, note=NOTE)


def lines(result):
    reserve = result['global_reservations']
    yield 'BUILDR diagnostics | {} queued | {} running | {} GiB scratch reserved'.format(
        result['active_counts']['queued'], reserve['jobs'], reserve['scratch_gb'])
    policy = result['protection']
    yield 'Quick protection: '+('active' if policy['effective'] else 'draining' if policy['pending'] else 'off')
    for item in policy.get('blocking_job_details', []):
        yield '  Job {} {} | runner {} | {}'.format(item['id'],item['state'],item['runner_version'],', '.join(item['conditions']))
    for item in policy['blocking_controllers']:
        yield '  Controller {} {} | {} | runner {} | {}'.format(item['kind'],item['id'],item.get('state','unknown'),item.get('runner_version','unrecorded'),item.get('condition','legacy controller must finish'))
    if policy['pending']:
        yield '  '+policy['activation_condition']
    for row in result['jobs']:
        yield '{} {} | wait {:.1f}s | {}'.format(row['id'],row['state'],row['wait_seconds'],row['cwd'])
        if row['reason']:
            yield '  '+row['reason']+(' (observation stale)' if row['observation']['stale'] else '')
        disk = row.get('disk')
        if disk:
            if disk['available_gb'] is None:
                yield '  Disk: unknown ('+disk['error']+')'
            else:
                yield '  Disk: {:.3f} GiB free / {:.3f} required | deficit {:.3f} GiB'.format(disk['available_gb'],disk['required_gb'],disk['deficit_gb'])
            parts=disk['components']
            yield '  Requirement: max({:.3f}, {:.3f}) floor + {:.3f} job scratch + {:.3f} running scratch GiB'.format(
                parts['machine_floor_gb'],parts['job_floor_gb'],parts['job_scratch_gb'],parts['running_scratch_gb'])
            for item in disk['contributing_jobs']:
                yield '    {} reserves {:.3f} GiB scratch'.format(item['job_id'],item['scratch_gb'])
            yield '  Inspect: '+row['inspect_cache_command']
        links = row['blocker_chain']
        if len(links['jobs'])>1:
            yield '  Observed chain: '+' -> '.join(j['id']+' '+j['state'] for j in links['jobs'])+' ('+links['end']+')'
    if result['jobs_truncated']:
        yield 'More active jobs exist; narrow with --repo or inspect a specific job.'
    yield result['note']
