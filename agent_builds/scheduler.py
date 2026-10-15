"""Admission ordering; callers hold the queue's SQLite write transaction."""
import time


def choose(queued, running, config, available_gb, disk_free):
    now = time.time()
    busy = {r['resource_key'] for r in running}
    heads = {}
    for row in sorted(queued, key=lambda r: (r['created'], r['id'])):
        if not row['cancel_requested'] and row['resource_key'] not in busy:
            heads.setdefault(row['resource_key'], row)
    def aged(row):
        return now - row['created'] >= config['aging_seconds']
    def priority(row):
        return (0 if aged(row) else 1 if row['lane'] == 'quick' else 2,
                row['created'], row['id'])
    def fits(row):
        if len(running) >= config['max_running']:
            return 'machine concurrency budget'
        if sum(r['slots'] for r in running) + row['slots'] > config['cpu_slots']:
            return 'CPU slot budget'
        if sum(r['memory_gb'] for r in running) + row['memory_gb'] > config['memory_gb']:
            return 'memory reservation budget'
        if row['lane'] == 'heavy':
            if sum(r['lane'] == 'heavy' for r in running) >= config['max_heavy']:
                return 'heavy job budget'
            heavy_slots = sum(r['slots'] for r in running if r['lane'] == 'heavy')
            if not aged(row) and heavy_slots + row['slots'] > config['cpu_slots'] - config['quick_reserve_slots']:
                return 'capacity reserved for quick checks; heavy jobs gain priority with age'
            if not aged(row) and sum(r['lane'] == 'heavy' for r in running) + 1 > config['max_running'] - config['quick_reserve_jobs']:
                return 'job capacity reserved for quick checks'
            if not aged(row) and sum(r['memory_gb'] for r in running if r['lane'] == 'heavy') + row['memory_gb'] > config['memory_gb'] - config['quick_reserve_memory_gb']:
                return 'memory capacity reserved for quick checks'
        return ''
    skipped = {}
    for row in sorted(heads.values(), key=priority):
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
        reason = fits(row)
        if not reason:
            return row, skipped
        skipped[row['id']] = reason
        # Let existing jobs drain for the oldest aged job. Do not preempt them.
        if aged(row):
            return row, skipped
    return None, skipped
