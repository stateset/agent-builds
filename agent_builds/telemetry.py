"""Best-effort Linux job measurements. RSS sums can include shared pages."""
import os
import math
from pathlib import Path
import resource
import time


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
        processes = []
        try:
            directories = Path('/proc').iterdir()
            for path in directories:
                if not path.name.isdigit():
                    continue
                try:
                    if path.stat().st_uid != os.getuid():
                        continue
                    text = (path / 'stat').read_text()
                    fields = text[text.rfind(')') + 2:].split()
                    processes.append((int(path.name), fields[19], int(fields[1]), int(fields[2]),
                                      int(fields[11]) + int(fields[12]), max(0, int(fields[21]))))
                except (OSError, ValueError, IndexError):
                    continue
        except OSError:
            pass
        # Only trust the group ID while the original leader still exists.
        leader_present = any(pid == self.pid and start == self.start for pid, start, *_ in processes)
        selected = {(pid, start) for pid, start, _, group, _, _ in processes
                    if (pid, start) in self.known or (leader_present and group == self.pid)}
        while True:
            parents = {pid for pid, _ in selected}
            expanded = selected | {(pid, start) for pid, start, parent, *_ in processes if parent in parents}
            if expanded == selected:
                break
            selected = expanded
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
        key = (row['resource_key'], row['command'], row['lane'], row['slots'], row['memory_gb'])
        groups.setdefault(key, []).append(row)
    result = []
    for (repository, _, lane, slots, memory), jobs in groups.items():
        measured = [j for j in jobs if j['usage'].get('samples', 0) > 0 and j['usage'].get('sampled_peak_rss_gb', 0) > 0 and j['usage'].get('complete')]
        peak = max((j['usage']['sampled_peak_rss_gb'] for j in measured), default=None)
        waits = sorted(j['wait_seconds'] for j in jobs)
        p95 = waits[max(0, math.ceil(len(waits) * .95) - 1)]
        result.append(dict(repository=repository, command_display=jobs[0]['command_display'],
            jobs=len(jobs), measured_jobs=len(measured), lane=lane, slots=slots, memory_gb=memory, wait_p50_seconds=waits[max(0, math.ceil(len(waits)*.5)-1)],
            wait_p95_seconds=p95, average_runtime_seconds=sum(j['elapsed_seconds'] for j in jobs)/len(jobs),
            sampled_peak_rss_gb=peak,
            suggested_memory_gb=max(.25, math.ceil(peak * 1.3 * 4)/4) if peak is not None and sum(j['state'] == 'succeeded' for j in measured) >= 3 else None))
    lanes = []
    for lane in ('quick', 'heavy'):
        matched = [r for r in rows if r['lane'] == lane and r['state'] in ('succeeded', 'failed', 'cancelled', 'lost')]
        waits = sorted(r['wait_seconds'] for r in matched)
        lanes.append(dict(lane=lane, jobs=len(matched), wait_p95_seconds=waits[max(0, math.ceil(len(waits)*.95)-1)] if waits else None))
    return dict(groups=result, lanes=lanes, history_limit=limit, suggestions_applied=False,
                note='Memory suggestions require three measured jobs; allow extra margin for unsampled peaks.')
