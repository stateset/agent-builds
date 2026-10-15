"""Bounded disk inventory and explicit retention of queue-owned logs."""
import os
from pathlib import Path
import stat
import subprocess
import time


def inventory(store, repo=None, extra_paths=()):
    repositories = {str(Path(repo).expanduser().resolve())} if repo else {r['cwd'] for r in store.rows(limit=2000)}
    paths = [(store.root / 'logs', 'queue logs')]
    paths += [(Path(p).expanduser().resolve(), 'explicit cache') for p in extra_paths]
    active = store.rows(limit=2000, active=True)
    for repository in sorted(repositories):
        root = Path(repository)
        for cache in ('target', 'node_modules', '.venv', '.next'):
            paths.append((root / cache, cache))
    rows = []
    deadline = time.monotonic() + 30
    seen = set()
    for path, kind in paths:
        if path in seen or path.is_symlink() or not path.is_dir():
            continue
        seen.add(path)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            rows.append(dict(path=str(path), kind=kind, bytes=None, error='inventory time budget reached'))
            continue
        try:
            result = subprocess.run(['du', '-sx', '-B1', '--', str(path)], stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, timeout=min(10, remaining))
            size = int(result.stdout.split()[0]) if result.stdout.strip() else None
            rows.append(dict(path=str(path), kind=kind, bytes=size,
                             error='some paths could not be read' if result.returncode else None))
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            rows.append(dict(path=str(path), kind=kind, bytes=None, error=type(error).__name__))
    def overlaps(left, right):
        return left == right or left in right.parents or right in left.parents
    for entry in rows:
        path = Path(entry['path'])
        users = [r for r in active if any(overlaps(path, Path(key)) for key in [r['cwd']]+r['resource_keys'] if Path(key).is_absolute())]
        if entry['kind'] == 'queue logs':
            users = active
        entry['running_jobs'] = [r['id'] for r in users if r['state'] in ('running', 'cancelling')]
        entry['queued_jobs'] = [r['id'] for r in users if r['state'] == 'queued']
        entry['cleanup_status'] = 'active_or_queued' if users else 'requires_owner_review'
    return dict(paths=sorted(rows, key=lambda r: r['bytes'] or 0, reverse=True),
                active_scratch_reservations=[dict(job_id=r['id'], cwd=r['cwd'], scratch_gb=r['scratch_gb'])
                    for r in active if r['state'] in ('running', 'cancelling')],
                note='Read-only cache sizes are measured; scratch reservations are estimates, not observed growth. Repository caches are never deleted here; review ownership before cleanup.')


def clean_logs(store, days=7, apply=False):
    import math
    if not math.isfinite(days) or days < 1:
        raise ValueError('Retention must be at least one day')
    cutoff = time.time() - days * 86400
    candidates = []
    def collect():
        if store.db is None:
            return
        for row in store.db.execute("SELECT id,finished FROM jobs WHERE state IN ('succeeded','failed','cancelled','lost') AND finished<?", (cutoff,)):
            path = store.log_path(row['id'])
            try:
                info = path.lstat()
                if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_mtime < cutoff:
                    candidates.append(dict(job_id=row['id'], path=str(path), bytes=info.st_size))
                    if apply:
                        # Never follow a symlink substituted since inspection.
                        current = path.lstat()
                        if (current.st_dev, current.st_ino, current.st_mtime_ns) != (info.st_dev, info.st_ino, info.st_mtime_ns):
                            raise ValueError('Log changed during cleanup; rerun preview')
                        path.unlink()
            except FileNotFoundError:
                continue
    if apply:
        with store.transaction():
            collect()
    else:
        collect()
    return dict(applied=apply, older_than_days=days, files=candidates,
                bytes=sum(c['bytes'] for c in candidates),
                note='Only terminal-job logs are eligible; job history, active logs and repository caches are retained.')
