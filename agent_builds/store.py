import contextlib
import fcntl
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
from . import waits, locks
from . import __version__

DEFAULTS = dict(cpu_slots=6, max_running=3, max_heavy=2, memory_gb=16,
                min_free_gb=10, quick_jobs=2, heavy_jobs=3, nice=10,
                timeout_seconds=7200, docker_image='', min_available_gb=4, paused=False,
                quick_reserve_slots=2, quick_reserve_jobs=1, quick_reserve_memory_gb=2, aging_seconds=120, protected_quick=False)
SCHEMA_VERSION = 2

TERMINAL = ('succeeded', 'failed', 'cancelled', 'lost')
STATES = ('queued', 'running', 'cancelling', 'succeeded', 'failed', 'cancelled', 'lost')

# Scheduling must not copy every job's source evidence, telemetry or batch
# results while holding SQLite's sole writer. Keep presentation reads separate.
ADMISSION_COLUMNS = ('id,state,created,cwd,lane,slots,memory_gb,resource_key,'
                     'resource_keys,lock_protocol,cancel_requested,min_free_gb,scratch_gb')
RESERVATION_COLUMNS = ADMISSION_COLUMNS + ',command,herdr,started'
BROWSER_COLUMNS = 'id,state,created,started,finished,cwd,command,backend,slots,memory_gb,exit_code,reason,herdr'
JOB_ORDER = " ORDER BY CASE WHEN state IN ('running','cancelling') THEN 0 WHEN state IN ('queued','held') THEN 1 ELSE 2 END, CASE WHEN state IN ('running','cancelling','queued','held') THEN created END ASC, created DESC"


class AdmissionBusy(sqlite3.OperationalError):
    """BEGIN IMMEDIATE could not acquire the writer lock; nothing ran."""


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
        self._config_cache = None
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
        self.db.execute('PRAGMA busy_timeout=15000')
        try:
            self._ensure_schema()
        except BaseException:
            self.db.close()
            raise

    def _ensure_schema(self):
        # WAL activation needs the same bootstrap lock as migrations. Concurrent
        # journal-mode changes can return SQLITE_BUSY without waiting for the
        # connection timeout. Current opens only read mode/version under this
        # short file lock; they do not change mode or start a write transaction.
        descriptor = os.open(str(self.root/'schema.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'a+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            version = self.db.execute('PRAGMA user_version').fetchone()[0]
            if version > SCHEMA_VERSION:
                raise ValueError('Queue schema is newer than this Buildr; use a current runner')
            if self.db.execute('PRAGMA journal_mode').fetchone()[0].lower() != 'wal':
                self.db.execute('PRAGMA journal_mode=WAL')
            if version < 1:
                self._migrate_1()
                with self.transaction():
                    self.db.execute('PRAGMA user_version=1')
                version = 1
            if version < 2:
                with self.transaction():
                    self._migrate_2()
                    self.db.execute('PRAGMA user_version=2')

    def _migrate_1(self):
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
                                     ('runner_version', "TEXT NOT NULL DEFAULT ''"),
                                     ('lock_protocol', 'INTEGER NOT NULL DEFAULT 0'),
                                     ('resource_keys', "TEXT NOT NULL DEFAULT '[]'"),
                                     ('batch', "TEXT NOT NULL DEFAULT '{}'"),
                                     ('admission', "TEXT NOT NULL DEFAULT '{}'"),
                                     ('reserve_protocol', 'INTEGER NOT NULL DEFAULT 0')]:
                if name not in columns:
                    self.db.execute('ALTER TABLE jobs ADD COLUMN ' + name + ' ' + definition)
            self.db.execute('CREATE TABLE IF NOT EXISTS admission_policy (id INTEGER PRIMARY KEY CHECK(id=1), requested INTEGER NOT NULL DEFAULT 0, effective INTEGER NOT NULL DEFAULT 0)')
            self.db.execute('INSERT OR IGNORE INTO admission_policy (id) VALUES (1)')
            policy_columns = {row[1] for row in self.db.execute('PRAGMA table_info(admission_policy)')}
            for name in ('requested_at', 'effective_at'):
                if name not in policy_columns:
                    self.db.execute('ALTER TABLE admission_policy ADD COLUMN '+name+' REAL')
            if 'required_protocol' not in policy_columns:
                self.db.execute('ALTER TABLE admission_policy ADD COLUMN required_protocol INTEGER NOT NULL DEFAULT 1')
            self.db.execute("""CREATE TRIGGER IF NOT EXISTS protected_policy_guard BEFORE INSERT ON jobs
                WHEN NEW.reserve_protocol!=1 AND (SELECT effective FROM admission_policy WHERE id=1)=1
                BEGIN SELECT RAISE(ABORT, 'Protected quick capacity active; use the installed Buildr runner'); END""")
            self.db.execute('CREATE TABLE IF NOT EXISTS job_locks (job_id TEXT NOT NULL, resource_key TEXT NOT NULL, PRIMARY KEY(job_id,resource_key))')
            # Old workers ignore additional locks. A legacy insertion on an
            # active extra key must fail BEFORE it creates any work or handle.
            self.db.execute("""CREATE TRIGGER IF NOT EXISTS legacy_lock_guard
                BEFORE INSERT ON jobs WHEN NEW.lock_protocol=0 AND EXISTS (
                    SELECT 1 FROM job_locks l JOIN jobs j ON j.id=l.job_id
                    WHERE j.state IN ('running','cancelling') AND j.lock_protocol=1
                      AND l.resource_key=NEW.resource_key AND l.resource_key!=j.resource_key)
                BEGIN SELECT RAISE(ABORT, 'Extra resource lock active; use the installed Buildr runner'); END""")
        self.db.execute('''CREATE TABLE IF NOT EXISTS announcements (
            id TEXT PRIMARY KEY, created REAL NOT NULL, expires REAL NOT NULL,
            message TEXT NOT NULL, worker_pid INTEGER, worker_start TEXT)''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS deliveries (
            announcement_id TEXT NOT NULL, pane TEXT NOT NULL, session TEXT NOT NULL,
            repository TEXT NOT NULL, agent TEXT NOT NULL, state TEXT NOT NULL,
            detail TEXT, updated REAL NOT NULL,
            PRIMARY KEY (announcement_id,pane))''')
        with self.transaction():
            self.db.execute('''CREATE TABLE IF NOT EXISTS external_watches (
                id TEXT PRIMARY KEY, created REAL NOT NULL, finished REAL,
                definition TEXT NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
                owner_pid INTEGER, owner_start TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
                stop_kind TEXT NOT NULL DEFAULT '', exit_code INTEGER,
                next_probe_at REAL, idle_since REAL, idle_seconds REAL NOT NULL DEFAULT 0)''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS external_probes (
                watch_id TEXT NOT NULL, ordinal INTEGER NOT NULL, request_key TEXT NOT NULL UNIQUE,
                job_id TEXT UNIQUE, created REAL NOT NULL, settled REAL, exit_code INTEGER,
                launch_token TEXT, launch_attempted INTEGER NOT NULL DEFAULT 0,
                attached INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (watch_id,ordinal))''')
            self.db.execute('''CREATE TABLE IF NOT EXISTS mcp_requests (
                request_key TEXT PRIMARY KEY, kind TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
                created REAL NOT NULL, state TEXT NOT NULL, result TEXT NOT NULL)''')
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

        with self.transaction():
            upgrade = self.db.execute('SELECT required_protocol FROM admission_policy WHERE id=1').fetchone()[0] < 2
            if upgrade:
                # Switch the required protocol, guards and compatibility mode
                # together, after every referenced controller table exists.
                self.db.execute('UPDATE admission_policy SET required_protocol=2,effective=0,effective_at=NULL WHERE id=1')
                self.db.execute('DROP TRIGGER IF EXISTS protected_policy_guard')
            self.db.execute("""CREATE TRIGGER IF NOT EXISTS protected_policy_guard BEFORE INSERT ON jobs
                WHEN NEW.reserve_protocol!=2 AND (SELECT effective FROM admission_policy WHERE id=1)=1
                BEGIN SELECT RAISE(ABORT, 'Protected ordering active; use the installed Buildr runner'); END""")
            for table in ('plans','external_watches'):
                columns={row[1] for row in self.db.execute('PRAGMA table_info('+table+')')}
                if 'reserve_protocol' not in columns:
                    self.db.execute('ALTER TABLE '+table+' ADD COLUMN reserve_protocol INTEGER NOT NULL DEFAULT 0')
                if 'runner_version' not in columns:
                    self.db.execute("ALTER TABLE "+table+" ADD COLUMN runner_version TEXT NOT NULL DEFAULT ''")
                self.db.execute("""CREATE TRIGGER IF NOT EXISTS protected_"""+table+"""_guard BEFORE INSERT ON """+table+"""
                    WHEN NEW.reserve_protocol!=2 AND (SELECT effective FROM admission_policy WHERE id=1)=1
                    BEGIN SELECT RAISE(ABORT, 'Protected ordering active; use the installed Buildr runner'); END""")
            # Already-loaded old code may try to activate using only its older
            # protocol check. Revert that flag within the same SQLite statement
            # rather than throwing and terminating an otherwise healthy runner.
            self.db.execute("""CREATE TRIGGER IF NOT EXISTS protected_activation_barrier
                AFTER UPDATE OF effective ON admission_policy WHEN NEW.effective=1 AND (
                  EXISTS (SELECT 1 FROM jobs WHERE state IN ('queued','held','running','cancelling') AND reserve_protocol!=2) OR
                  EXISTS (SELECT 1 FROM plans WHERE state NOT IN ('succeeded','failed','cancelled') AND reserve_protocol!=2) OR
                  EXISTS (SELECT 1 FROM external_watches WHERE state NOT IN ('succeeded','failed','cancelled','uncertain','timed_out','exhausted') AND reserve_protocol!=2))
                BEGIN UPDATE admission_policy SET effective=0,effective_at=NULL WHERE id=NEW.id; END""")
            # A per-takeover proof prevents already-loaded older controllers or
            # stage workers from silently dropping declared input semantics.
            # This capability is independent of scheduler reserve protocols.
            for table in ('jobs', 'plans'):
                columns = {row[1] for row in self.db.execute('PRAGMA table_info('+table+')')}
                for field in ('input_protocol', 'input_owner_epoch'):
                    if field not in columns:
                        self.db.execute('ALTER TABLE '+table+' ADD COLUMN '+field+' INTEGER NOT NULL DEFAULT 0')
                self.db.execute('''CREATE TRIGGER IF NOT EXISTS declared_inputs_'''+table+'''_guard
                    BEFORE UPDATE OF owner_pid ON '''+table+''' WHEN OLD.input_protocol=1
                      AND NEW.input_owner_epoch!=OLD.input_owner_epoch+1
                    BEGIN SELECT RAISE(ABORT, 'Declared input files require the installed Buildr controller/worker'); END''')

    def _migrate_2(self):
        self.db.execute('CREATE TABLE queue_events (id INTEGER PRIMARY KEY CHECK(id=1), generation INTEGER NOT NULL)')
        self.db.execute('INSERT INTO queue_events VALUES (1,1)')
        for event, name in (('INSERT','insert'), ('DELETE','delete'),
                ('UPDATE OF state,cancel_requested,slots,memory_gb,lane,created,resource_key,resource_keys,reserve_protocol,min_free_gb,scratch_gb','update')):
            self.db.execute('CREATE TRIGGER queue_event_'+name+' AFTER '+event+' ON jobs '
                'BEGIN UPDATE queue_events SET generation=generation+1 WHERE id=1; END')
        self.db.execute('CREATE TRIGGER queue_event_policy AFTER UPDATE OF requested,effective,required_protocol ON admission_policy '
            'WHEN OLD.requested!=NEW.requested OR OLD.effective!=NEW.effective OR OLD.required_protocol!=NEW.required_protocol '
            'BEGIN UPDATE queue_events SET generation=generation+1 WHERE id=1; END')

    def close(self):
        if self.db is not None:
            self.db.close()

    def config_signature(self):
        try:
            stat = self.config_path.stat()
            return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        except FileNotFoundError:
            return None

    def config(self):
        signature = self.config_signature()
        cached = self._config_cache
        if cached is None or cached[0] != signature:
            settings = dict(DEFAULTS)
            if signature is not None:
                settings.update(json.loads(self.config_path.read_text()))
            self.validate_config(settings)
            self._config_cache = (signature, settings) if self.config_signature() == signature else None
        else:
            settings = cached[1]
        result = dict(settings)
        # Policy activation belongs to SQLite and remains fresh even when the
        # configuration file has not changed. Never cache these live flags.
        from .protection import exists
        row = self.db.execute('SELECT requested,effective FROM admission_policy WHERE id=1').fetchone() if exists(self) else (0, 0)
        result.update(protected_quick=bool(row[1]), protected_quick_requested=bool(row[0]),
                      protected_quick_pending=bool(row[0] and not row[1]))
        return result

    def recover_if_due(self, interval=2):
        """One crash-released file lease elects a periodic recovery sweep.

        Explicit recover() remains synchronous. This fast path is for polling
        loops only, and never runs from a read-only connection.
        """
        if self.read_only:
            raise ValueError('Recovery requires a writable queue')
        now = time.monotonic()
        if now < getattr(self, '_next_recovery_check', 0):
            return False
        self._next_recovery_check = now+interval
        descriptor = os.open(str(self.root/'recovery.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'r+b', buffering=0) as lease:
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            try:
                previous = float(lease.read(64) or b'0')
            except ValueError:
                previous = 0
            now = time.monotonic()
            if 0 <= now-previous < interval:
                return False
            acquired, _ = self._try_write(self.recover)
            if not acquired:
                return False
            lease.seek(0)
            lease.write(str(time.monotonic()).encode())
            lease.truncate()
            return True

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
        if not isinstance(result['protected_quick'], bool):
            raise ValueError('protected_quick must be true or false')
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
            from .protection import activate, status, validate_job
            control = status(self, result)
            requested = changes.get('protected_quick', control['requested'])
            if control['effective']:
                # Reject reserve edits that would invalidate active reservations.
                if any(key in changes for key in ('cpu_slots','max_running','memory_gb','quick_reserve_slots','quick_reserve_jobs','quick_reserve_memory_gb')):
                    for row in self.db.execute("SELECT * FROM jobs WHERE state IN ('queued','held','running','cancelling')"):
                        validate_job(dict(result, protected_quick=True), row['lane'], row['slots'], row['memory_gb'])
                    heavy = list(self.db.execute("SELECT * FROM jobs WHERE state IN ('running','cancelling') AND lane='heavy'"))
                    if (sum(row['slots'] for row in heavy)>result['cpu_slots']-result['quick_reserve_slots'] or
                            sum(row['memory_gb'] for row in heavy)>result['memory_gb']-result['quick_reserve_memory_gb'] or
                            len(heavy)>result['max_running']-result['quick_reserve_jobs']):
                        raise ValueError('Wait for active heavy reservations to drain before changing protected reserves')
            self.db.execute('''UPDATE admission_policy SET
                requested_at=CASE WHEN ?=1 AND requested=0 THEN ? ELSE requested_at END,
                effective_at=CASE WHEN ?=0 THEN NULL ELSE effective_at END,
                requested=?,effective=CASE WHEN ?=0 THEN 0 ELSE effective END WHERE id=1''',
                (int(requested),time.time(),int(requested),int(requested),int(requested)))
            activate(self, result)
            result = {key:value for key,value in result.items() if key not in ('protected_quick_requested','protected_quick_pending')}
            result['protected_quick'] = requested
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
        try:
            self.db.execute('BEGIN IMMEDIATE')
        except sqlite3.OperationalError as error:
            # Python 3.8 does not expose sqlite_errorcode. Convert only the
            # canonical SQLITE_BUSY result from BEGIN, never body/commit errors.
            if str(error) == 'database is locked':
                raise AdmissionBusy(str(error)) from error
            raise
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def _try_write(self, operation, *args):
        """Defer a busy BEGIN only. Errors after BEGIN always propagate."""
        timeout = self.db.execute('PRAGMA busy_timeout').fetchone()[0]
        self.db.execute('PRAGMA busy_timeout=0')
        try:
            try:
                return True, operation(*args)
            except AdmissionBusy:
                return False, None
        finally:
            self.db.execute('PRAGMA busy_timeout=' + str(timeout))

    def poll_claim(self, job):
        """None means writer busy, False means denied, True means admitted."""
        acquired, admitted = self._try_write(self.claim, job)
        return admitted if acquired else None

    def log_path(self, job):
        return self.root / 'logs' / (job + '.log')

    def get(self, job):
        row = self.db.execute('SELECT * FROM jobs WHERE id=?', (job,)).fetchone() if self.db else None
        if row is None:
            raise ValueError('Unknown job: ' + job)
        return self.describe(dict(row))

    def get_control(self, job):
        """Small cancellation read; no diagnostic expansion or state normalization."""
        row = self.db.execute('SELECT state,cancel_requested FROM jobs WHERE id=?', (job,)).fetchone() if self.db else None
        if row is None:
            raise ValueError('Unknown job: ' + job)
        return row

    def get_execution_control(self, job):
        """Fresh process identity and cancellation state, without bulky evidence."""
        row = self.db.execute('SELECT id,state,cancel_requested,backend,child_pid,child_start FROM jobs WHERE id=?', (job,)).fetchone() if self.db else None
        if row is None:
            raise ValueError('Unknown job: ' + job)
        return row

    def get_wait_context(self, job):
        """Read one selected job's recorded wait observations, without evidence."""
        row = self.db.execute('SELECT wait_context FROM jobs WHERE id=?', (job,)).fetchone() if self.db else None
        if row is None:
            raise ValueError('Unknown job: ' + job)
        context = json.loads(row[0] or '{}')
        if not isinstance(context, dict):
            raise ValueError('Invalid recorded wait context for ' + job)
        return context

    def browser_rows(self, limit=200):
        """Bounded browser projection; detailed inspection remains explicit."""
        if type(limit) is not int or not 1<=limit<=200:
            raise ValueError('Browser limit must be an integer from 1 to 200')
        if self.db is None:
            return []
        now = time.time()
        rows = []
        for stored in self.db.execute('SELECT '+BROWSER_COLUMNS+' FROM jobs'+JOB_ORDER+' LIMIT ?', (limit,)):
            row = dict(stored)
            row['storage_state'] = row['state']
            if row['state']=='held':
                row['state']='queued'
            row['command_display'] = shlex.join(json.loads(row['command']))
            row['origin'] = json.loads(row.pop('herdr') or '{}')
            row['log_path'] = str(self.log_path(row['id']))
            row['wait_seconds'] = max(0,(row['started'] or row['finished'] or now)-row['created'])
            row['elapsed_seconds'] = max(0,(row['finished'] or now)-row['started']) if row['started'] else 0
            rows.append(row)
        return rows

    def describe(self, row):
        row['storage_state'] = row['state']
        if row['state'] == 'held':
            row['state'] = 'queued'
        row['resource_keys'] = locks.keys(row)
        row['locks'] = [key for key in row['resource_keys'] if key != row.get('resource_key')]
        row['lock_protocol'] = row.get('lock_protocol', 0)
        row['argv'] = json.loads(row['command'])
        row['command_display'] = shlex.join(row['argv'])
        row['log_path'] = str(self.log_path(row['id']))
        row['wait_context'] = json.loads(row.get('wait_context') or '{}')
        row['origin'] = json.loads(row.get('herdr') or '{}')
        row['usage'] = json.loads(row.get('usage') or '{}')
        row['source'] = json.loads(row.get('source') or '{}')
        row['admission'] = json.loads(row.get('admission') or '{}')
        row['batch'] = json.loads(row.get('batch') or '{}')
        from .batch import summary
        row['batch'] = summary(row)
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
                preemption=False, note=('Age grants admission priority, not a guaranteed start time. Protected quick capacity cannot be borrowed.' if config['protected_quick'] else 'Age grants admission priority, not a guaranteed start time. Only aged jobs may borrow quick capacity.') if known_policy else
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
            "SELECT * FROM jobs" + where + JOB_ORDER+" LIMIT ?", parameters + (limit,))]

    @staticmethod
    def filters(active=False, repo=None):
        clauses = []
        parameters = ()
        if active:
            clauses.append("state IN ('queued','held','running','cancelling')")
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
        for state, count in self.db.execute('SELECT state,COUNT(*) FROM jobs' + where + ' GROUP BY state', parameters):
            key = 'queued' if state == 'held' else state
            counts[key] = counts.get(key, 0) + count
        return counts

    def submit(self, cwd, command, backend, lane, slots, memory, resource_key,
               image, timeout, herdr=None, min_free_gb=0, scratch_gb=0, locks=None, source=None):
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
        from .locks import normalize
        resources = normalize(resource_key, locks)
        job = uuid.uuid4().hex[:12]
        origin = {key: os.environ[name] for key, name in
                  [('pane_id', 'HERDR_PANE_ID'), ('tab_id', 'HERDR_TAB_ID'),
                   ('workspace_id', 'HERDR_WORKSPACE_ID'),
                   ('remote_request_id', 'AGENT_BUILDS_REMOTE_REQUEST_ID')]
                  if os.environ.get(name)}
        origin.update(herdr or {})
        created = time.time()
        # The job and all its locks become visible atomically. Prepared plan
        # and watch jobs already hold an enclosing write transaction.
        with contextlib.nullcontext() if self.db.in_transaction else self.transaction():
            from .protection import activate, validate_job
            activate(self)
            current = self.config()
            # Revalidate against policy activation or configuration changes that
            # committed while submission was preparing its argv/source snapshot.
            if slots>current['cpu_slots'] or memory>current['memory_gb']:
                raise ValueError('Job reservation exceeds current machine budget')
            validate_job(dict(current, protected_quick=current['protected_quick_requested']), lane, slots, memory)
            self.db.execute('''INSERT INTO jobs
                (id,state,created,owner_pid,owner_start,cwd,command,backend,lane,
                 slots,memory_gb,resource_key,image,timeout,herdr,min_free_gb,scratch_gb,policy_version,wait_history,runner_version,lock_protocol,resource_keys,reserve_protocol,source,input_protocol)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (job, 'held' if len(resources)>1 else 'queued', created, os.getpid(), identity(os.getpid()), str(cwd),
                 json.dumps(command), backend, lane, slots, memory, resource_key, image,
                 timeout, json.dumps(origin), min_free_gb, scratch_gb, POLICY_VERSION, json.dumps(waits.begin(created)), __version__, 1, json.dumps(resources), 2,
                 json.dumps(source or {}), int(bool((source or {}).get('submission', {}).get('inputs')))))
            self.db.executemany('INSERT INTO job_locks VALUES (?,?)', [(job, key) for key in resources])
        return job

    def recover(self):
        """Never rerun work whose outcome became unknown."""
        with self.transaction():
            rows = self.db.execute("SELECT id,state,owner_pid,owner_start,child_pid,child_start,backend,cancel_requested "
                                   "FROM jobs WHERE state IN ('queued','held','running','cancelling')").fetchall()
            for row in rows:
                if alive(row['owner_pid'], row['owner_start']):
                    continue
                if row['backend'] == 'docker' and row['state'] not in ('queued', 'held'):
                    self.db.execute("UPDATE jobs SET reason='owner exited; cancel to reconcile Docker container' WHERE id=?", (row['id'],))
                    continue
                # Preserve reservations while an orphaned child is still alive.
                if alive(row['child_pid'], row['child_start']):
                    self.db.execute("UPDATE jobs SET reason='owner exited; child still running' WHERE id=?", (row['id'],))
                    continue
                state = 'cancelled' if row['cancel_requested'] else 'lost'
                self.finish(row['id'], state, None, 'owner exited; inspect logs before retrying')

    def _claim_row(self, job):
        row = self.db.execute('SELECT '+ADMISSION_COLUMNS+',reason,wait_context,wait_history FROM jobs WHERE id=?', (job,)).fetchone()
        if row is None:
            raise ValueError('Unknown job: ' + job)
        row = dict(row)
        if row['state'] == 'held':
            row['state'] = 'queued'
        row['resource_keys'] = locks.keys(row)
        row['wait_context'] = json.loads(row['wait_context'] or '{}')
        row['wait_history'] = json.loads(row['wait_history'] or '{}')
        return row

    def _blocking_job(self, job):
        """Read display fields for the one selected blocker, not the backlog."""
        row = dict(self.db.execute('SELECT id,state,created,started,finished,cwd,command FROM jobs WHERE id=?', (job,)).fetchone())
        now = time.time()
        return dict(id=row['id'],state='queued' if row['state']=='held' else row['state'],
                    cwd=row['cwd'],command_display=shlex.join(json.loads(row['command'])),
                    elapsed_seconds=max(0,(row['finished'] or now)-row['started']) if row['started'] else 0,
                    wait_seconds=max(0,(row['started'] or row['finished'] or now)-row['created']),
                    age_seconds=max(0,(row['finished'] or now)-row['created']),
                    log_path=str(self.log_path(job)))

    def claim(self, job):
        with self.transaction():
            from .protection import activate
            activate(self)
            config = self.config()
            row = self._claim_row(job)
            if row['cancel_requested']:
                return False
            if row['state'] != 'queued':
                return False
            running = [dict(r) for r in self.db.execute(
                "SELECT "+RESERVATION_COLUMNS+" FROM jobs WHERE state IN ('running','cancelling')")]
            queued = [dict(r) for r in self.db.execute("SELECT "+ADMISSION_COLUMNS+" FROM jobs WHERE state IN ('queued','held') AND cancel_requested=0 ORDER BY created,id")]
            # Scheduler/lock comparisons reuse each row many times. Decode once
            # in this transaction; never cache across claims or change storage.
            for item in running+queued:
                item['resource_keys'] = locks.keys(item)
            candidates = locks.eligible(queued)
            legacy = next((r for r in queued if not r['lock_protocol'] and locks.overlaps(row, r)), None) if len(row['resource_keys'])>1 else None
            older = next((r for r in candidates if (r['created'], r['id']) < (row['created'], job) and locks.overlaps(row, r)), None)
            measured = snapshot(row['cwd'])
            free_gb = min(shutil.disk_usage(row['cwd']).free, shutil.disk_usage(self.root).free) / 1024**3
            required_disk = max(config['min_free_gb'], row['min_free_gb']) + row['scratch_gb'] + sum(r['scratch_gb'] for r in running)
            same_repo = next((r for r in running if locks.overlaps(row, r)), None)
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
            elif legacy:
                reason = 'legacy worker must drain before multiple locks'
                blocker = legacy
            elif older:
                reason = 'earlier job in this repository'
                blocker = dict(older)
            elif same_repo:
                reason = 'repository already building'
                blocker = same_repo
            else:
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
                selected, skipped = choose(candidates, running, config, measured['available_gb'], disk_free)
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
                context['conflicting_resource_keys'] = sorted(set(row['resource_keys']).intersection(locks.keys(blocker)))
                context['blocking_job'] = self._blocking_job(blocker['id'])
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
            admission={key:config[key] for key in ('cpu_slots','max_running','max_heavy','memory_gb',
                'quick_reserve_slots','quick_reserve_jobs','quick_reserve_memory_gb','protected_quick','aging_seconds')}
            self.db.execute("UPDATE jobs SET state='running',started=?,reason='',wait_context='{}',wait_history=?,admission=? WHERE id=?",
                            (now, json.dumps(history), json.dumps(admission), job))
            self._reservation_cache = None
            return True

    def reservation(self, row):
        result = {key: row[key] for key in ('id', 'state', 'cwd', 'slots', 'memory_gb', 'scratch_gb')}
        command = shlex.join(json.loads(row['command']))
        result.update(resource_keys=locks.keys(dict(row)), command_display=command[:180], command_truncated=len(command) > 180,
                      log_path=str(self.log_path(row['id'])), origin=json.loads(row['herdr'] or '{}'),
                      elapsed_seconds=max(0, time.time() - row['started']) if row['started'] else 0)
        return result

    def reservation_snapshot(self):
        cached = self._reservation_cache
        if cached is None or time.monotonic() - cached[0] > .2:
            jobs = [] if self.db is None else [self.reservation(row) for row in self.db.execute(
                "SELECT "+RESERVATION_COLUMNS+" FROM jobs WHERE state IN ('running','cancelling') ORDER BY started")]
            cached = (time.monotonic(), dict(observed_at=time.time(), jobs=jobs))
            self._reservation_cache = cached
        return cached[1]

    def cancel(self, job):
        with self.transaction():
            row = self.get_control(job)
            if row['state'] in TERMINAL:
                return False
            state = row['state'] if row['state'] in ('queued', 'held') else 'cancelling'
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
