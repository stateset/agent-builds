"""Explicit user-authorized queue announcements through Herdr's native API."""
import json
import os
import shlex
from pathlib import Path
import subprocess
import sys
import time
import uuid

from .store import Store, alive, identity

_script = Path(__file__).resolve().parent.parent / 'buildr'
_launcher = shlex.quote(str(_script)) if _script.is_file() else shlex.join([sys.executable, '-m', 'agent_builds'])

POLICY = '''The user asks all coding agents in this Herdr session to route future expensive builds, checks and tests through our shared workstation queue.

Use this executable from your repository:
  {buildr} run --lane quick -- cargo check
  {buildr} run -- cargo build
  {buildr} run -- cargo test
  {buildr} run -- npm run build

Keep the original command and arguments after --. The wrapper waits for resources, streams output, and preserves the exit code. It sets Cargo/test worker budgets; for other tools set their own worker flags where needed. --cwd /absolute/repo is supported. Inspect the queue with {buildr} status --json. The TUI is running in a separate Herdr pane.

Continue your current task. Let an already-running build finish, then use the queue for subsequent expensive commands. Do not nest one queue wrapper inside another or wrap an entire agent session. Do not cancel existing jobs, change global Cargo settings, or bypass normal sandbox approval requirements. If shared-state access is blocked by your sandbox, request normal approval for the wrapper command. Light reads and edits can run directly. Existing repository instructions and required verification still apply.

Please briefly acknowledge this policy and apply it to your next build/check/test command.'''.format(buildr=_launcher)


def require_herdr():
    if os.environ.get('HERDR_ENV') != '1':
        raise ValueError('Announcements require a Herdr-managed pane (HERDR_ENV=1)')


def call(*args):
    require_herdr()
    return subprocess.run(['herdr'] + list(args), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, timeout=5)


def agents():
    result = call('agent', 'list')
    if result.returncode:
        raise ValueError('Cannot inspect Herdr agents: ' + result.stderr[:200])
    return json.loads(result.stdout)['result']['agents']


def session(agent):
    value = agent.get('agent_session')
    # Without a session identity a replacement agent cannot be distinguished.
    return json.dumps(value, sort_keys=True) if value else ''


def preview(exclude=None):
    exclude = exclude or os.environ.get('HERDR_PANE_ID')
    return [agent for agent in agents() if agent['pane_id'] != exclude and session(agent)]


def create(store, recipients, message=POLICY, ttl=900):
    require_herdr()
    announcement = uuid.uuid4().hex[:12]
    with store.transaction():
        store.db.execute('INSERT INTO announcements(id,created,expires,message) VALUES(?,?,?,?)',
                         (announcement, time.time(), time.time() + ttl, message))
        for agent in recipients:
            store.db.execute('''INSERT INTO deliveries
                (announcement_id,pane,session,repository,agent,state,detail,updated)
                VALUES(?,?,?,?,?,'pending','waiting for safe input',?)''',
                (announcement, agent['pane_id'], session(agent), agent.get('cwd', ''),
                 agent.get('agent', ''), time.time()))
    return announcement


def deliveries(store, announcement=None):
    if store.db is None:
        return []
    sql = 'SELECT * FROM deliveries'
    params = ()
    if announcement:
        sql += ' WHERE announcement_id=?'
        params = (announcement,)
    sql += ' ORDER BY updated DESC'
    if not announcement:
        sql += ' LIMIT 100'
    return [dict(row) for row in store.db.execute(sql, params)]


def mark(store, announcement, pane, state, detail):
    store.db.execute('UPDATE deliveries SET state=?,detail=?,updated=? WHERE announcement_id=? AND pane=?',
                     (state, detail[:300], time.time(), announcement, pane))


def dispatch_once(store, announcement):
    record = store.db.execute('SELECT * FROM announcements WHERE id=?', (announcement,)).fetchone()
    if record is None:
        raise ValueError('Unknown announcement')
    pending = [row for row in deliveries(store, announcement) if row['state'] == 'pending']
    for row in pending:
        if time.time() > record['expires']:
            mark(store, announcement, row['pane'], 'expired', 'not delivered before expiration')
            continue
        try:
            result = call('agent', 'get', row['pane'])
            if result.returncode:
                # A failed read has no side effect; keep it pending until expiry.
                continue
            current = json.loads(result.stdout)['result']['agent']
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
            continue
        if session(current) != row['session']:
            mark(store, announcement, row['pane'], 'skipped', 'agent session changed')
            continue
        status = current.get('agent_status')
        ready = status in ('idle', 'done') or (status == 'working' and current.get('agent') == 'codex')
        if not ready:
            mark(store, announcement, row['pane'], 'pending', 'agent is ' + str(status))
            continue
        # Claim BEFORE the side effect. An uncertain result is never auto-retried.
        with store.transaction():
            changed = store.db.execute("UPDATE deliveries SET state='sending',updated=? WHERE announcement_id=? AND pane=? AND state='pending'",
                (time.time(), announcement, row['pane'])).rowcount
        if not changed:
            continue
        try:
            result = call('agent', 'prompt', row['pane'], record['message'])
            if result.returncode == 0:
                mark(store, announcement, row['pane'], 'submitted', 'submitted through native agent input; adoption not confirmed')
            elif 'agent_blocked' in result.stderr:
                mark(store, announcement, row['pane'], 'pending', 'agent blocked; no input sent')
            else:
                mark(store, announcement, row['pane'], 'unknown', 'submission failed; inspect before retrying: ' + result.stderr[:150])
        except (OSError, subprocess.TimeoutExpired):
            mark(store, announcement, row['pane'], 'unknown', 'submission outcome unknown; not retried')


def spawn_worker(announcement):
    env = os.environ.copy()
    root = str(Path(__file__).resolve().parent.parent)
    env['PYTHONPATH'] = root + os.pathsep + env.get('PYTHONPATH', '')
    return subprocess.Popen([sys.executable, '-m', 'agent_builds', 'announce-worker', announcement],
                            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)


def work(announcement):
    require_herdr()
    store = Store()
    try:
        with store.transaction():
            record = store.db.execute('SELECT * FROM announcements WHERE id=?', (announcement,)).fetchone()
            if record is None:
                raise ValueError('Unknown announcement')
            if alive(record['worker_pid'], record['worker_start']):
                return
            store.db.execute('UPDATE announcements SET worker_pid=?,worker_start=? WHERE id=?',
                             (os.getpid(), identity(os.getpid()), announcement))
            store.db.execute("UPDATE deliveries SET state='unknown',detail='worker exited during submission; not retried' WHERE announcement_id=? AND state='sending'", (announcement,))
        while any(row['state'] == 'pending' for row in deliveries(store, announcement)):
            dispatch_once(store, announcement)
            if any(row['state'] == 'pending' for row in deliveries(store, announcement)):
                time.sleep(2)
    finally:
        store.close()
