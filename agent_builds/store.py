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
from .scheduler import POLICY_VERSION, choose
from . import waits
from . import __version__

DEFAULTS = dict(cpu_slots=6, max_running=3, max_heavy=2, memory_gb=16,
                min_free_gb=10, quick_jobs=2, heavy_jobs=3, nice=10,
                timeout_seconds=7200, docker_image='', min_available_gb=4, paused=False,
                quick_reserve_slots=2, quick_reserve_jobs=1, quick_reserve_memory_gb=2, aging_seconds=120)
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
                                     ('wait_context', "TEXT NOT NULL DEFAULT '{}'"),
                                     ('usage', "TEXT NOT NULL DEFAULT '{}'"),
                                     ('policy_version', "TEXT NOT NULL DEFAULT ''"),
                                     ('source', "TEXT NOT NULL DEFAULT '{}'"),
                                     ('wait_history', "TEXT NOT NULL DEFAULT '{}'"),
                                     ('runner_version', "TEXT NOT NULL DEFAULT ''")]:
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
        with self.transaction():
            self.db.execute('''CREATE TABLE IF NOT EXISTS job_groups (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, created REAL NOT NULL, jobs TEXT NOT NULL)''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS plans (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, cwd TEXT NOT NULL, definition TEXT NOT NULL,
                created REAL NOT NULL, finished REAL, state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
                owner_pid INTEGER, owner_start TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT '{}')''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS plan_stages (
                plan_id TEXT NOT NULL, name TEXT NOT NULL, ordinal INTEGER NOT NULL, definition TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', reason TEXT NOT NULL DEFAULT '', job_id TEXT UNIQUE,
                launch_token TEXT, attached INTEGER NOT NULL DEFAULT 0, evidence TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (plan_id,name))''')

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
            if type(result[key]) is not int or result[key] < 1:
                raise ValueError('{} must be a positive integer'.format(key))
        for key in ('memory_gb', 'timeout_seconds', 'aging_seconds'):
            if type(result[key]) not in (int, float) or not math.isfinite(result[key]) or result[key] <= 0:
                raise ValueError('{} must be positive'.format(key))
        for key in ('min_free_gb', 'min_available_gb', 'quick_reserve_memory_gb'):
            if type(result[key]) not in (int, float) or not math.isfinite(result[key]) or result[key] < 0:
                raise ValueError('{} must be finite and nonnegative'.format(key))
        if type(result['quick_reserve_slots']) is not int or not 0 <= result['quick_reserve_slots'] < result['cpu_slots']:
            raise ValueError('quick_reserve_slots must be between zero and cpu_slots - 1')
        if type(result['quick_reserve_jobs']) is not int or not 0 <= result['quick_reserve_jobs'] < result['max_running']:
            raise ValueError('quick_reserve_jobs must be between zero and max_running - 1')
        if result['quick_reserve_memory_gb'] >= result['memory_gb']:
            raise ValueError('quick_reserve_memory_gb must be below the total memory budget')
        if type(result['nice']) is not int or not 0 <= result['nice'] <= 19:
            raise ValueError('nice must be an integer between zero and 19')
        if not isinstance(result['paused'], bool):
            raise ValueError('paused must be true or false')
        if not isinstance(result['docker_image'], str):
            raise ValueError('docker_image must be text')
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
        self._reservation_cache = None
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
        row['usage'] = json.loads(row.get('usage') or '{}')
        row['source'] = json.loads(row.get('source') or '{}')
        row['runner_version'] = row.get('runner_version') or 'unrecorded'
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
        row['wait_history'] = json.loads(row.get('wait_history') or '{}')
        row['wait_breakdown'] = waits.describe(row['wait_history'], row['created'],
                                              row['created'] + row['wait_seconds'])
        row['elapsed_seconds'] = max(0, (row['finished'] or time.time()) - row['started']) if row['started'] else 0
        row['age_seconds'] = max(0, (row['finished'] or time.time()) - row['created'])
        if row['state'] == 'queued':
            config = self.config()
            known_policy = row.get('policy_version') == POLICY_VERSION
            row['scheduling'] = dict(policy_version=row.get('policy_version') or 'unrecorded', policy_known=known_policy,
                age_priority=row['wait_seconds'] >= config['aging_seconds'] if known_policy else None,
                aging_seconds=config['aging_seconds'], quick_reserve_slots=config['quick_reserve_slots'],
                quick_reserve_jobs=config['quick_reserve_jobs'], quick_reserve_memory_gb=config['quick_reserve_memory_gb'],
                preemption=False, note='Age grants admission priority, not a guaranteed start time. Only aged jobs may borrow quick capacity.' if known_policy else
                'Runner policy is unrecorded or unknown. Current configuration may not apply to this already-loaded runner.')
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
                   ('workspace_id', 'HERDR_WORKSPACE_ID'),
                   ('remote_request_id', 'AGENT_BUILDS_REMOTE_REQUEST_ID')]
                  if os.environ.get(name)}
        origin.update(herdr or {})
        created = time.time()
        self.db.execute('''INSERT INTO jobs
            (id,state,created,owner_pid,owner_start,cwd,command,backend,lane,
             slots,memory_gb,resource_key,image,timeout,herdr,min_free_gb,scratch_gb,policy_version,wait_history,runner_version)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (job, 'queued', created, os.getpid(), identity(os.getpid()), str(cwd),
             json.dumps(command), backend, lane, slots, memory, resource_key, image,
             timeout, json.dumps(origin), min_free_gb, scratch_gb, POLICY_VERSION, json.dumps(waits.begin(created)), __version__))
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
                self.finish(row['id'], state, None, 'owner exited; inspect logs before retrying')

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
            free_gb = min(shutil.disk_usage(row['cwd']).free, shutil.disk_usage(self.root).free) / 1024**3
            required_disk = max(config['min_free_gb'], row['min_free_gb']) + row['scratch_gb'] + sum(r['scratch_gb'] for r in running)
            same_repo = next((r for r in running if r['resource_key'] == row['resource_key']), None)
            context = dict(measured_at=time.time(), disk_free_gb=free_gb, required_free_gb=required_disk,
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
            else:
                queued = [dict(r) for r in self.db.execute("SELECT * FROM jobs WHERE state='queued' AND cancel_requested=0")]
                disk_by_device = {}
                def disk_free(candidate):
                    path = candidate['cwd']
                    try:
                        device = os.stat(path).st_dev
                        if device not in disk_by_device:
                            disk_by_device[device] = min(shutil.disk_usage(path).free, shutil.disk_usage(self.root).free) / 1024**3
                        return disk_by_device[device]
                    except OSError:
                        return 0
                selected, skipped = choose(queued, running, config, measured['available_gb'], disk_free)
                context.update(quick_reserve_slots=config['quick_reserve_slots'], aging_seconds=config['aging_seconds'],
                               quick_reserve_jobs=config['quick_reserve_jobs'], quick_reserve_memory_gb=config['quick_reserve_memory_gb'])
                if selected is not None and selected['id'] == job:
                    reason = skipped.get(job, '')
                elif job in skipped:
                    reason = skipped[job]
                elif selected is not None:
                    reason = 'aged job waiting for capacity' if time.time() - selected['created'] >= config['aging_seconds'] else 'higher priority queued job'
                    blocker = selected
                else:
                    reason = 'waiting for scheduling capacity'
            if blocker:
                context['blocking_job'] = {key: value for key, value in self.describe(blocker).items()
                    if key in ('id', 'command_display', 'cwd', 'elapsed_seconds', 'wait_seconds', 'age_seconds', 'log_path', 'state')}
                reason += ' [' + blocker['id'] + ']'
            if reason:
                previous = row['wait_context']
                kind = waits.category(reason)
                blocker_id = blocker['id'] if blocker else None
                history = row['wait_history']
                changed = (kind, blocker_id) != (history.get('current_category'), history.get('current_blocker'))
                if changed or reason != row['reason'] or context['measured_at'] - previous.get('measured_at', 0) >= 2:
                    history = waits.advance(history, context['measured_at'], kind, blocker_id) if history else {}
                    self.db.execute('UPDATE jobs SET reason=?,wait_context=?,wait_history=? WHERE id=?',
                                    (reason, json.dumps(context), json.dumps(history), job))
                return False
            now = time.time()
            history = waits.advance(row['wait_history'], now) if row['wait_history'] else {}
            self.db.execute("UPDATE jobs SET state='running',started=?,reason='',wait_context='{}',wait_history=? WHERE id=?",
                            (now, json.dumps(history), job))
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
        row = self.db.execute('SELECT started,wait_history FROM jobs WHERE id=?', (job,)).fetchone()
        now = time.time()
        history = json.loads(row['wait_history'] or '{}')
        if history and not row['started']:
            history = waits.advance(history, now)
        self.db.execute('UPDATE jobs SET state=?,exit_code=?,finished=?,reason=?,wait_history=? WHERE id=?',
                        (state, code, now, reason, json.dumps(history), job))
        self._reservation_cache = None

    def record_usage(self, job, usage):
        self.db.execute('UPDATE jobs SET usage=? WHERE id=?', (json.dumps(usage), job))

    def record_source(self, job, source):
        self.db.execute('UPDATE jobs SET source=? WHERE id=?', (json.dumps(source), job))
