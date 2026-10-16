"""Admission ordering; callers hold the queue's SQLite write transaction."""
import time

from .locks import keys, overlaps

POLICY_VERSION = 'protected-verification-v5'


def capacity_reason(row, running, config, now):
    if len(running) >= config['max_running']:
        return 'machine concurrency budget'
    if sum(r['slots'] for r in running) + row['slots'] > config['cpu_slots']:
        return 'CPU slot budget'
    if sum(r['memory_gb'] for r in running) + row['memory_gb'] > config['memory_gb']:
        return 'memory reservation budget'
    if row['lane'] == 'heavy':
        if sum(r['lane'] == 'heavy' for r in running) >= config['max_heavy']:
            return 'heavy job budget'
        if config.get('protected_quick') or now-row['created'] < config['aging_seconds']:
            if sum(r['slots'] for r in running if r['lane']=='heavy') + row['slots'] > config['cpu_slots']-config['quick_reserve_slots']:
                return 'protected capacity reserved for quick checks' if config.get('protected_quick') else 'capacity reserved for quick checks; heavy jobs gain priority with age'
            if sum(r['lane']=='heavy' for r in running)+1 > config['max_running']-config['quick_reserve_jobs']:
                return 'job capacity reserved for quick checks'
            if sum(r['memory_gb'] for r in running if r['lane']=='heavy')+row['memory_gb'] > config['memory_gb']-config['quick_reserve_memory_gb']:
                return 'memory capacity reserved for quick checks'
    return ''


def safe_backfill(head, candidate, running, config, now):
    """Adding a quick job must preserve head eligibility after ANY completion subset.

    This proves reservation feasibility only, not runtime or external pressure.
    Large configurations conservatively retain draining instead of exponential work.
    """
    if len(running) > 8 or candidate['lane'] != 'quick' or candidate['scratch_gb']:
        return False
    if overlaps(candidate, head) or capacity_reason(candidate, running, config, now):
        return False
    for mask in range(1 << len(running)):
        remaining = [row for index, row in enumerate(running) if mask & (1 << index)]
        if not capacity_reason(head, remaining, config, now) and capacity_reason(head, remaining+[candidate], config, now):
            return False
    return True


def choose(queued, running, config, available_gb, disk_free, allow_backfill=True, now=None):
    now = time.time() if now is None else now
    busy = {key for row in running for key in keys(row)}
    seen, heads = set(), []
    for row in sorted(queued, key=lambda r: (r['created'], r['id'])):
        if row['cancel_requested']:
            continue
        resources = set(keys(row))
        if resources.isdisjoint(busy) and resources.isdisjoint(seen):
            heads.append(row)
        # Even a busy or externally blocked earlier job retains FIFO on all
        # declared keys; no partial acquisition and no lock-order deadlock.
        seen.update(resources)
    def aged(row):
        return now - row['created'] >= config['aging_seconds']
    def priority(row):
        return (0 if aged(row) else 1 if row['lane'] == 'quick' else 2,
                row['created'], row['id'])
    ordered = sorted(heads, key=priority)
    skipped = {}
    for row in ordered:
        # An impossible or externally blocked job must not freeze other repos.
        required = max(config['min_free_gb'], row['min_free_gb']) + row['scratch_gb'] + sum(r['scratch_gb'] for r in running)
        if disk_free(row) < required:
            skipped[row['id']] = 'disk below {:.1f} GiB requirement'.format(required)
            continue
        if available_gb < row['memory_gb'] + config['min_available_gb']:
            skipped[row['id']] = 'available RAM headroom'
            continue
        if row['slots'] > config['cpu_slots'] or row['memory_gb'] > config['memory_gb']:
            skipped[row['id']] = 'job exceeds current machine budget'
            continue
        reason = capacity_reason(row, running, config, now)
        if not reason:
            return row, skipped
        skipped[row['id']] = reason
        # Let existing jobs drain for the oldest aged job. Do not preempt them.
        # A protected heavy pool may be full at the same time as a global
        # CPU/RAM/job limit. Check heavy-only feasibility instead of relying on
        # which reason happened to be returned first. Quick work may use the
        # reserved capacity (including declared scratch) while heavy work drains.
        # If quick work itself blocks the aged head, retain the normal draining
        # and safe-backfill rules so repeated quick arrivals cannot starve it.
        protected_heavy_drain = (config.get('protected_quick') and row['lane']=='heavy' and
            bool(capacity_reason(row,[r for r in running if r['lane']=='heavy'],config,now)))
        if aged(row) and not protected_heavy_drain:
            if allow_backfill:
                for candidate in ordered:
                    if candidate['id'] == row['id'] or candidate['lane'] != 'quick' or candidate['scratch_gb']:
                        continue
                    required_disk = max(config['min_free_gb'], candidate['min_free_gb'])+candidate['scratch_gb']+sum(r['scratch_gb'] for r in running)
                    # Keep RAM headroom for both jobs, and never borrow scratch.
                    if available_gb < row['memory_gb']+candidate['memory_gb']+config['min_available_gb'] or disk_free(candidate) < required_disk:
                        continue
                    if safe_backfill(row, candidate, running, config, now):
                        skipped.pop(candidate['id'], None)
                        return candidate, skipped
            return row, skipped
    return None, skipped
