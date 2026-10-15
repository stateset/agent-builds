import contextlib
import json
import math
import os
import shlex
from pathlib import Path
import shutil
import sqlite3
import time
import uuid

from .metrics import snapshot
from .budgets import worker_budget

DEFAULTS = dict(cpu_slots=6, max_running=3, max_heavy=2, memory_gb=16,
                min_free_gb=10, quick_jobs=2, heavy_jobs=3, nice=10,
                timeout_seconds=7200, docker_image='', min_available_gb=4, paused=False)
TERMINAL = ('succeeded', 'failed', 'cancelled', 'lost')
STATES = ('queued', 'running', 'cancelling', 'succeeded', 'failed', 'cancelled', 'lost')


def identity(pid):
    """Linux start ticks prevent stale records from signalling a reused PID."""
    try:
        text = Path('/proc/{}/stat'.format(pid)).read_text()
        fields = text[text.rfind(')') + 2:].split()
        if fields[0] == 'Z':
            return None
        return fields[19]
    except (OSError, IndexError):
        return None


def alive(pid, start):
    return bool(pid and start and identity(pid) == start)


class Store:
    def __init__(self, root=None, read_only=False):
        self.root = Path(root or os.environ.get('AGENT_BUILDS_STATE') or
                         Path.home() / '.local/state/agent-builds').expanduser().resolve()
        self.path = self.root / 'queue.sqlite3'
        self.config_path = self.root / 'config.json'
        self.read_only = read_only
        self.db = None
        self._reservation_cache = None
        if read_only:
            if self.path.exists():
                self.db = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True,
                                          timeout=15, isolation_level=None)
                self.db.row_factory = sqlite3.Row
                self.db.execute('PRAGMA query_only=ON')
            return
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(str(self.root), 0o700)
        (self.root / 'logs').mkdir(exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(str(self.path), timeout=15, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        os.chmod(str(self.path), 0o600)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA busy_timeout=15000')
        self.db.execute('''CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, state TEXT NOT NULL, created REAL NOT NULL,
            started REAL, finished REAL, owner_pid INTEGER, owner_start TEXT,
            child_pid INTEGER, child_start TEXT, cwd TEXT NOT NULL,
            command TEXT NOT NULL, backend TEXT NOT NULL, lane TEXT NOT NULL,
            slots INTEGER NOT NULL, memory_gb REAL NOT NULL,
            resource_key TEXT NOT NULL, exit_code INTEGER, reason TEXT,
            cancel_requested INTEGER NOT NULL DEFAULT 0, image TEXT,
            timeout REAL NOT NULL, herdr TEXT NOT NULL DEFAULT '{}')''')
        with self.transaction():
            columns = {row[1] for row in self.db.execute('PRAGMA table_info(jobs)')}
            for name, definition in [('min_free_gb', 'REAL NOT NULL DEFAULT 0'),
                                     ('scratch_gb', 'REAL NOT NULL DEFAULT 0'),
                                     ('wait_context', "TEXT NOT NULL DEFAULT '{}'")]:
                if name not in columns:
                    self.db.execute('ALTER TABLE jobs ADD COLUMN ' + name + ' ' + definition)
        self.db.execute('''CREATE TABLE IF NOT EXISTS announcements (
            id TEXT PRIMARY KEY, created REAL NOT NULL, expires REAL NOT NULL,
            message TEXT NOT NULL, worker_pid INTEGER, worker_start TEXT)''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS deliveries (
            announcement_id TEXT NOT NULL, pane TEXT NOT NULL, session TEXT NOT NULL,
            repository TEXT NOT NULL, agent TEXT NOT NULL, state TEXT NOT NULL,
            detail TEXT, updated REAL NOT NULL,
            PRIMARY KEY (announcement_id,pane))''')

    def close(self):
        if self.db is not None:
            self.db.close()

    def config(self):
        result = dict(DEFAULTS)
        if self.config_path.exists():
            result.update(json.loads(self.config_path.read_text()))
        return self.validate_config(result)

    @staticmethod
    def validate_config(result):
        for key in ('cpu_slots', 'max_running', 'max_heavy', 'quick_jobs', 'heavy_jobs'):
            if not isinstance(result[key], int) or result[key] < 1:
                raise ValueError('{} must be a positive integer'.format(key))
        for key in ('memory_gb', 'timeout_seconds'):
            if not isinstance(result[key], (int, float)) or not math.isfinite(result[key]) or result[key] <= 0:
                raise ValueError('{} must be positive'.format(key))
        if (not math.isfinite(result['min_free_gb']) or result['min_free_gb'] < 0 or
                not math.isfinite(result['min_available_gb']) or result['min_available_gb'] < 0 or
                not 0 <= result['nice'] <= 19):
            raise ValueError('Invalid disk watermark or niceness')
        if not isinstance(result['paused'], bool):
            raise ValueError('paused must be true or false')
        return result

    def update_config(self, changes):
        with self.transaction():
            result = self.config()
            result.update(changes)
            self.validate_config(result)
            temporary = self.root / ('config-' + uuid.uuid4().hex + '.tmp')
            try:
                with temporary.open('x') as handle:
                    os.chmod(str(temporary), 0o600)
                    json.dump(result, handle, indent=2)
                    handle.write('\n')
                    handle.flush()
                    os.fsync(handle.fileno())
                temporary.replace(self.config_path)
            finally:
                if temporary.exists():
                    temporary.unlink()
            return result

    @contextlib.contextmanager
    def read_snapshot(self):
        if self.db is None:
            yield
            return
        self.db.execute('BEGIN')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    @contextlib.contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def log_path(self, job):
        return self.root / 'logs' / (job + '.log')

    def get(self, job):
        row = self.db.execute('SELECT * FROM jobs WHERE id=?', (job,)).fetchone() if self.db else None
        if row is None:
            raise ValueError('Unknown job: ' + job)
        return self.describe(dict(row))

    def describe(self, row):
        row['argv'] = json.loads(row['command'])
        row['command_display'] = shlex.join(row['argv'])
        row['log_path'] = str(self.log_path(row['id']))
        row['wait_context'] = json.loads(row.get('wait_context') or '{}')
        row['origin'] = json.loads(row.get('herdr') or '{}')
        row['worker_budget'] = worker_budget(row['argv'], row['slots'])
        if row['state'] == 'queued' and row['wait_context']:
            observed = self.reservation_snapshot()
            context = row['wait_context']
            context['contributing_jobs'] = observed['jobs']
            context['reservations_observed_at'] = observed['observed_at']
            context['current_used_cpu_slots'] = sum(job['slots'] for job in observed['jobs'])
            context['current_reserved_memory_gb'] = sum(job['memory_gb'] for job in observed['jobs'])
            context.setdefault('job_memory_gb', row['memory_gb'])
            if 'required_memory_gb' in context:
                context.setdefault('memory_headroom_gb', max(0, context['required_memory_gb'] - row['memory_gb']))
        row['wait_seconds'] = max(0, (row['started'] or row['finished'] or time.time()) - row['created'])
        row['elapsed_seconds'] = max(0, (row['finished'] or time.time()) - row['started']) if row['started'] else 0
        row['age_seconds'] = max(0, (row['finished'] or time.time()) - row['created'])
        if row['state'] in ('queued', 'running', 'cancelling'):
            # A read-only client may be inside an agent PID namespace. Its
            # /proc view cannot establish that a host runner has exited.
            row['owner_alive'] = None if self.read_only else alive(row['owner_pid'], row['owner_start'])
            row['owner_visibility'] = 'not verified by read-only inspection' if self.read_only else 'local process namespace'
        return row

    def rows(self, limit=200, active=False, repo=None):
        if self.db is None:
            return []
        where, parameters = self.filters(active, repo)
        return [self.describe(dict(row)) for row in self.db.execute(
            "SELECT * FROM jobs" + where + " ORDER BY CASE WHEN state IN ('running','cancelling') THEN 0 WHEN state='queued' THEN 1 ELSE 2 END, CASE WHEN state IN ('running','cancelling','queued') THEN created END ASC, created DESC LIMIT ?", parameters + (limit,))]

    @staticmethod
    def filters(active=False, repo=None):
        clauses = []
        parameters = ()
        if active:
            clauses.append("state IN ('queued','running','cancelling')")
        if repo:
            path = str(Path(repo).expanduser().resolve())
            clauses.append('(cwd=? OR resource_key=?)')
            parameters += (path, path)
        return (' WHERE ' + ' AND '.join(clauses) if clauses else ''), parameters

    def counts(self, active=False, repo=None):
        counts = dict.fromkeys(STATES, 0)
        if self.db is None:
            return counts
        where, parameters = self.filters(active, repo)
        counts.update({row[0]: row[1] for row in self.db.execute(
            'SELECT state,COUNT(*) FROM jobs' + where + ' GROUP BY state', parameters)})
        return counts

    def submit(self, cwd, command, backend, lane, slots, memory, resource_key,
               image, timeout, herdr=None, min_free_gb=0, scratch_gb=0):
        config = self.config()
        if slots < 1 or slots > config['cpu_slots']:
            raise ValueError('Job CPU slots exceed machine budget')
        if not math.isfinite(memory) or memory <= 0 or memory > config['memory_gb']:
            raise ValueError('Job memory reservation exceeds machine budget')
        if backend not in ('local', 'docker'):
            raise ValueError('Backend must be local or docker')
        if not command or not Path(cwd).is_dir():
            raise ValueError('A command and an existing working directory are required')
        if backend == 'docker' and not image:
            raise ValueError('Docker jobs require --image or configured docker_image')
        if any(not math.isfinite(value) or value < 0 for value in (min_free_gb, scratch_gb)):
            raise ValueError('Disk requirements must be finite nonnegative GiB values')
        job = uuid.uuid4().hex[:12]
        origin = {key: os.environ[name] for key, name in
                  [('pane_id', 'HERDR_PANE_ID'), ('tab_id', 'HERDR_TAB_ID'),
                   ('workspace_id', 'HERDR_WORKSPACE_ID')]
                  if os.environ.get(name)}
        origin.update(herdr or {})
        self.db.execute('''INSERT INTO jobs
            (id,state,created,owner_pid,owner_start,cwd,command,backend,lane,
             slots,memory_gb,resource_key,image,timeout,herdr,min_free_gb,scratch_gb)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (job, 'queued', time.time(), os.getpid(), identity(os.getpid()), str(cwd),
             json.dumps(command), backend, lane, slots, memory, resource_key, image,
             timeout, json.dumps(origin), min_free_gb, scratch_gb))
        return job

    def recover(self):
        """Never rerun work whose outcome became unknown."""
        with self.transaction():
            rows = self.db.execute("SELECT * FROM jobs WHERE state IN ('queued','running','cancelling')").fetchall()
            for row in rows:
                if alive(row['owner_pid'], row['owner_start']):
                    continue
                if row['backend'] == 'docker' and row['state'] != 'queued':
                    self.db.execute("UPDATE jobs SET reason='owner exited; cancel to reconcile Docker container' WHERE id=?", (row['id'],))
                    continue
                # Preserve reservations while an orphaned child is still alive.
                if alive(row['child_pid'], row['child_start']):
                    self.db.execute("UPDATE jobs SET reason='owner exited; child still running' WHERE id=?", (row['id'],))
                    continue
                state = 'cancelled' if row['cancel_requested'] else 'lost'
                self.db.execute('UPDATE jobs SET state=?,finished=?,reason=? WHERE id=?',
                    (state, time.time(), 'owner exited; inspect logs before retrying', row['id']))

    def claim(self, job):
        config = self.config()
        with self.transaction():
            row = self.get(job)
            if row['cancel_requested']:
                return False
            if row['state'] != 'queued':
                return False
            running = [dict(r) for r in self.db.execute(
                "SELECT * FROM jobs WHERE state IN ('running','cancelling')")]
            older = self.db.execute("SELECT * FROM jobs WHERE state='queued' AND created<? AND resource_key=? AND cancel_requested=0 ORDER BY created LIMIT 1",
                                    (row['created'], row['resource_key'])).fetchone()
            measured = snapshot(row['cwd'])
            disk_free = min(shutil.disk_usage(row['cwd']).free, shutil.disk_usage(self.root).free) / 1024**3
            required_disk = max(config['min_free_gb'], row['min_free_gb']) + row['scratch_gb'] + sum(r['scratch_gb'] for r in running)
            same_repo = next((r for r in running if r['resource_key'] == row['resource_key']), None)
            context = dict(measured_at=time.time(), disk_free_gb=disk_free, required_free_gb=required_disk,
                           available_memory_gb=measured['available_gb'], required_memory_gb=row['memory_gb'] + config['min_available_gb'],
                           job_memory_gb=row['memory_gb'], memory_headroom_gb=config['min_available_gb'],
                           reserved_memory_gb=sum(r['memory_gb'] for r in running), total_reserved_memory_gb=config['memory_gb'],
                           used_cpu_slots=sum(r['slots'] for r in running), total_cpu_slots=config['cpu_slots'],
                           requested_cpu_slots=row['slots'],
                           contributing_jobs=[self.reservation(r) for r in running])
            blocker = None
            reason = ''
            if config['paused']:
                reason = 'queue paused'
            elif older:
                reason = 'earlier job in this repository'
                blocker = dict(older)
            elif same_repo:
                reason = 'repository already building'
                blocker = same_repo
            elif len(running) >= config['max_running']:
                reason = 'machine concurrency budget'
            elif sum(r['slots'] for r in running) + row['slots'] > config['cpu_slots']:
                reason = 'CPU slot budget'
            elif sum(r['memory_gb'] for r in running) + row['memory_gb'] > config['memory_gb']:
                reason = 'memory reservation budget'
            elif row['lane'] == 'heavy' and sum(r['lane'] == 'heavy' for r in running) >= config['max_heavy']:
                reason = 'heavy job budget'
            elif disk_free < required_disk:
                reason = 'disk below {:.1f} GiB requirement ({:.1f} GiB free)'.format(required_disk, disk_free)
            elif measured['available_gb'] < row['memory_gb'] + config['min_available_gb']:
                reason = 'available RAM headroom'
            else:
                # FIFO among other repositories that are not currently building.
                earlier = self.db.execute("""SELECT * FROM jobs
                    WHERE state='queued' AND created<? AND cancel_requested=0 ORDER BY created""",
                    (row['created'],)).fetchall()
                busy = {r['resource_key'] for r in running}
                waiting = next((r for r in earlier if r['resource_key'] not in busy and r['resource_key'] != row['resource_key']), None)
                if waiting:
                    reason = 'earlier queued job'
                    blocker = dict(waiting)
            if blocker:
                context['blocking_job'] = {key: value for key, value in self.describe(blocker).items()
                    if key in ('id', 'command_display', 'cwd', 'elapsed_seconds', 'wait_seconds', 'age_seconds', 'log_path', 'state')}
                reason += ' [' + blocker['id'] + ']'
            if reason:
                previous = row['wait_context']
                if reason != row['reason'] or context['measured_at'] - previous.get('measured_at', 0) >= 2:
                    self.db.execute('UPDATE jobs SET reason=?,wait_context=? WHERE id=?', (reason, json.dumps(context), job))
                return False
            self.db.execute("UPDATE jobs SET state='running',started=?,reason='',wait_context='{}' WHERE id=?", (time.time(), job))
            self._reservation_cache = None
            return True

    def reservation(self, row):
        result = {key: row[key] for key in ('id', 'state', 'cwd', 'slots', 'memory_gb', 'scratch_gb')}
        command = shlex.join(json.loads(row['command']))
        result.update(command_display=command[:180], command_truncated=len(command) > 180,
                      log_path=str(self.log_path(row['id'])), origin=json.loads(row['herdr'] or '{}'),
                      elapsed_seconds=max(0, time.time() - row['started']) if row['started'] else 0)
        return result

    def reservation_snapshot(self):
        cached = self._reservation_cache
        if cached is None or time.monotonic() - cached[0] > .2:
            jobs = [] if self.db is None else [self.reservation(row) for row in self.db.execute(
                "SELECT * FROM jobs WHERE state IN ('running','cancelling') ORDER BY started")]
            cached = (time.monotonic(), dict(observed_at=time.time(), jobs=jobs))
            self._reservation_cache = cached
        return cached[1]

    def cancel(self, job):
        with self.transaction():
            row = self.get(job)
            if row['state'] in TERMINAL:
                return False
            state = 'cancelling' if row['state'] != 'queued' else 'queued'
            self.db.execute('UPDATE jobs SET cancel_requested=1,state=? WHERE id=?', (state, job))
            self._reservation_cache = None
            return True

    def finish(self, job, state, code, reason=''):
        self.db.execute('UPDATE jobs SET state=?,exit_code=?,finished=?,reason=? WHERE id=?',
                        (state, code, time.time(), reason, job))
        self._reservation_cache = None
