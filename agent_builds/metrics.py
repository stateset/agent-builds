import os
from pathlib import Path
import shutil
import time

from .host_pressure import PRESSURE_KINDS, pressure_sample


def pressure(kind):
    sample = pressure_sample(kind)['some']
    return sample['avg10'] if sample else None


def snapshot(path):
    memory = {}
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            key, value = line.split(':', 1)
            memory[key] = int(value.strip().split()[0]) / 1024**2
    except (OSError, ValueError):
        pass
    disk = shutil.disk_usage(path)
    psi = {kind: pressure_sample(kind) for kind in PRESSURE_KINDS}
    return dict(cpus=os.cpu_count(), load=os.getloadavg()[0],
                available_gb=memory.get('MemAvailable', 0),
                total_gb=memory.get('MemTotal', 0), free_gb=disk.free / 1024**3,
                cpu_pressure=psi['cpu']['some']['avg10'] if psi['cpu']['some'] else None,
                io_pressure=psi['io']['some']['avg10'] if psi['io']['some'] else None,
                memory_pressure=psi['memory']['some']['avg10'] if psi['memory']['some'] else None,
                psi=psi)


class ProcessSampler:
    """Read only names, ownership and counters; never command-line secrets."""
    def __init__(self):
        self.previous = {}
        self.sampled_at = time.monotonic()
        self.ticks = os.sysconf('SC_CLK_TCK')
        self.pages = os.sysconf('SC_PAGE_SIZE')

    def sample(self):
        now = time.monotonic()
        interval = max(.01, now - self.sampled_at)
        previous = self.previous
        current = {}
        result = []
        for directory in Path('/proc').iterdir():
            if not directory.name.isdigit():
                continue
            try:
                if directory.stat().st_uid != os.getuid():
                    continue
                stat = (directory / 'stat').read_text()
                name = stat[stat.find('(') + 1:stat.rfind(')')]
                fields = stat[stat.rfind(')') + 2:].split()
                if fields[0] == 'Z':
                    continue
                key = (int(directory.name), fields[19])
                cpu_ticks = int(fields[11]) + int(fields[12])
                current[key] = cpu_ticks
                cpu = None if key not in previous else max(0, cpu_ticks - previous[key]) / self.ticks / interval * 100
                rss = int(fields[21]) * self.pages / 1024**2
                try:
                    cwd = str((directory / 'cwd').resolve(strict=True))
                except OSError:
                    cwd = ''
                result.append(dict(pid=key[0], name=name, cpu=cpu, rss_mb=rss, cwd=cwd))
            except (OSError, ValueError, IndexError):
                continue
        self.previous = current
        self.sampled_at = now
        return sorted(result, key=lambda row: (row['cpu'] or 0, row['rss_mb']), reverse=True)
