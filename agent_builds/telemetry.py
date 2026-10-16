"""Best-effort Linux job measurements. RSS sums can include shared pages."""
import os
import math
import resource
import time

from .process_tree import read_processes, select_tree


class JobSampler:
    def __init__(self, pid, start):
        self.pid, self.start = pid, start
        self.ticks = os.sysconf('SC_CLK_TCK')
        self.pages = os.sysconf('SC_PAGE_SIZE')
        self.counters = {}
        self.known = {(pid, start)}
        self.peak = 0
        self.samples = 0
        self.started = time.monotonic()
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        self.baseline = usage.ru_utime + usage.ru_stime

    def sample(self):
        processes = read_processes()
        selected = select_tree(processes, self.pid, self.start, self.known)
        self.known.update(selected)
        rss = 0
        for pid, start, _, _, ticks, pages in processes:
            if (pid, start) in selected:
                self.counters[(pid, start)] = max(ticks, self.counters.get((pid, start), 0))
                rss += pages * self.pages
        self.peak = max(self.peak, rss)
        self.samples += 1
        return self.result()

    def result(self, finished=False):
        cpu = sum(self.counters.values()) / self.ticks
        if finished:
            usage = resource.getrusage(resource.RUSAGE_CHILDREN)
            cpu = max(cpu, usage.ru_utime + usage.ru_stime - self.baseline)
        return dict(cpu_seconds=round(cpu, 4), sampled_peak_rss_gb=self.peak / 1024**3,
                    samples=self.samples, observed_seconds=time.monotonic() - self.started,
                    observed_at=time.time(), source='local_process_tree', complete=finished,
                    limitations='Sampled RSS may miss short peaks and count shared pages more than once; CPU may miss unobserved descendants.')


def statistics(store, repo=None, limit=1000):
    rows = store.rows(limit=limit, repo=repo)
    groups = {}
    for row in rows:
        if row['state'] not in ('succeeded', 'failed', 'cancelled', 'lost'):
            continue
        key = (row['resource_key'], tuple(sorted(row['locks'])), row['command'], row['lane'], row['slots'], row['memory_gb'])
        groups.setdefault(key, []).append(row)
    result = []
    for (repository, extra, _, lane, slots, memory), jobs in groups.items():
        measured = [j for j in jobs if j['usage'].get('samples', 0) > 0 and j['usage'].get('sampled_peak_rss_gb', 0) > 0 and j['usage'].get('complete')]
        peak = max((j['usage']['sampled_peak_rss_gb'] for j in measured), default=None)
        waits = sorted(j['wait_seconds'] for j in jobs)
        p95 = waits[max(0, math.ceil(len(waits) * .95) - 1)]
        reserved_cpu_seconds = sum(j['slots']*j['elapsed_seconds'] for j in measured)
        observed_cpu_seconds = sum(j['usage'].get('cpu_seconds', 0) for j in measured)
        result.append(dict(repository=repository, resource_keys=[repository]+list(extra), command_display=jobs[0]['command_display'],
            jobs=len(jobs), measured_jobs=len(measured), lane=lane, slots=slots, memory_gb=memory, wait_p50_seconds=waits[max(0, math.ceil(len(waits)*.5)-1)],
            wait_p95_seconds=p95, average_runtime_seconds=sum(j['elapsed_seconds'] for j in jobs)/len(jobs),
            sampled_peak_rss_gb=peak,
            reserved_cpu_seconds=reserved_cpu_seconds, observed_cpu_seconds=observed_cpu_seconds,
            observed_to_reserved_cpu_ratio=observed_cpu_seconds/reserved_cpu_seconds if reserved_cpu_seconds > 0 else None,
            suggested_memory_gb=max(.25, math.ceil(peak * 1.3 * 4)/4) if peak is not None and sum(j['state'] == 'succeeded' for j in measured) >= 3 else None))
    lanes = []
    for lane in ('quick', 'heavy'):
        matched = [r for r in rows if r['lane'] == lane and r['state'] in ('succeeded', 'failed', 'cancelled', 'lost')]
        waits = sorted(r['wait_seconds'] for r in matched)
        lanes.append(dict(lane=lane, jobs=len(matched), wait_p95_seconds=waits[max(0, math.ceil(len(waits)*.95)-1)] if waits else None))
    policies = []
    keys = sorted({(r.get('policy_version') or 'unrecorded', r['lane']) for r in rows})
    for policy, lane in keys:
        matched = [r for r in rows if (r.get('policy_version') or 'unrecorded') == policy and r['lane'] == lane and r['state'] in ('succeeded', 'failed', 'cancelled', 'lost')]
        waits = sorted(r['wait_seconds'] for r in matched)
        policies.append(dict(policy_version=policy, lane=lane, jobs=len(matched),
            wait_p95_seconds=waits[max(0, math.ceil(len(waits)*.95)-1)] if waits else None))
    from .waits import NOTE
    completed = [r for r in rows if r['state'] in ('succeeded', 'failed', 'cancelled', 'lost')]
    blockers = {}
    for row in completed:
        for kind, seconds in row['wait_breakdown']['seconds'].items():
            blockers[kind] = blockers.get(kind, 0) + seconds
    total_wait = sum(r['wait_seconds'] for r in completed)
    observed_wait = sum(r['wait_breakdown']['observed_seconds'] for r in completed)
    wait_breakdown = dict(seconds=blockers, total_seconds=total_wait, observed_seconds=observed_wait,
                          coverage=observed_wait/total_wait if total_wait > 0 else 0,
                          recorded_jobs=sum(r['wait_breakdown']['recorded'] for r in completed),
                          jobs=len(completed), note=NOTE)
    return dict(groups=result, lanes=lanes, policies=policies, wait_breakdown=wait_breakdown, history_limit=limit, suggestions_applied=False,
                note='Memory suggestions require three measured jobs; allow extra margin for unsampled peaks.')
