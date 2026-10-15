"""Bounded observations of queue blockers, not an exact scheduler trace."""
import copy

MAX_GAP_SECONDS = 5
MAX_TRANSITIONS = 64
NOTE = ('Blockers are sampled by the runner. Gaps over five seconds and legacy '
        'waits are unobserved; only the primary admission blocker is counted.')


def category(reason):
    if reason == 'queue paused':
        return 'paused'
    if reason.startswith(('earlier job in this repository', 'repository already building')):
        return 'repository_lock'
    if reason.startswith('legacy worker must drain'):
        return 'lock_compatibility'
    if reason.startswith(('aged job waiting', 'higher priority queued')):
        return 'priority'
    if reason.startswith('disk below'):
        return 'disk'
    if reason == 'available RAM headroom':
        return 'ram_headroom'
    if reason == 'job exceeds current machine budget':
        return 'oversized'
    if 'reserved for quick checks' in reason:
        return 'quick_reserve'
    return {'machine concurrency budget': 'job_capacity', 'CPU slot budget': 'cpu_capacity',
            'memory reservation budget': 'memory_capacity', 'heavy job budget': 'heavy_capacity'}.get(reason, 'scheduling')


def begin(created):
    return dict(version=1, last_at=created, current_category='unobserved',
                current_blocker=None, seconds={}, transitions=[], transition_count=0)


def advance(data, now, kind=None, blocker=None):
    result = copy.deepcopy(data)
    # Wall-clock regressions never produce negative durations or duplicate time.
    now = max(now, result['last_at'])
    elapsed = now - result['last_at']
    previous = result['current_category']
    credited = previous if elapsed <= MAX_GAP_SECONDS else 'unobserved'
    totals = result['seconds']
    totals[credited] = totals.get(credited, 0) + elapsed
    if kind is not None and (kind, blocker) != (previous, result['current_blocker']):
        result['transition_count'] += 1
        result['transitions'] = (result['transitions'] + [dict(at=now, category=kind, blocking_job_id=blocker)])[-MAX_TRANSITIONS:]
        result.update(current_category=kind, current_blocker=blocker)
    result['last_at'] = now
    return result


def describe(data, created, end):
    total = max(0, end-created)
    if not data:
        return dict(recorded=False, seconds={'unobserved': total}, total_seconds=total,
                    observed_seconds=0, coverage=0, transitions=[], transition_count=0, note=NOTE)
    result = advance(data, end)
    # A legacy runner may settle a migrated job without finalizing observations.
    # Never project beyond the actual wait endpoint, or invent missing history.
    totals = {key: max(0, value) for key, value in result['seconds'].items()}
    recorded = sum(totals.values())
    if recorded > total and recorded > 0:
        totals = {'unobserved': total}
    else:
        totals['unobserved'] = totals.get('unobserved', 0) + total-recorded
    observed = max(0, total-totals.get('unobserved', 0))
    return dict(recorded=True, seconds=totals, total_seconds=total, observed_seconds=observed,
                coverage=observed/total if total > 0 else 0,
                current_category=result['current_category'], current_blocker=result['current_blocker'],
                last_observed_at=data['last_at'], transitions=result['transitions'],
                transition_count=result['transition_count'], note=NOTE)


def summary(report):
    ordered = sorted(report['seconds'].items(), key=lambda item: (-item[1], item[0]))
    return ', '.join('{} {:.1f}s'.format(key, value) for key, value in ordered if value > 0) or 'no wait recorded'


class WaitUpdates:
    """Emit semantic transitions promptly, with one heartbeat per minute."""
    def __init__(self):
        self.signature = None
        self.last = None

    def due(self, reason, context, now):
        signature = (category(reason), context.get('blocking_job', {}).get('id'))
        if signature != self.signature or self.last is None or now-self.last >= 60:
            self.signature, self.last = signature, now
            return True
        return False
