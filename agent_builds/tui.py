import curses
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from .metrics import ProcessSampler, snapshot
from . import __version__
from .runner import cancel_job
from .store import Store, TERMINAL


def tail(path, lines):
    try:
        with path.open('rb') as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - 65536))
            return handle.read().decode('utf-8', errors='replace').splitlines()[-lines:]
    except OSError:
        return []


def paint(screen, y, x, text, attr=0):
    height, width = screen.getmaxyx()
    if y < 0 or y >= height or x >= width - 1:
        return
    # Strip terminal controls from command output before drawing it.
    clean = ''.join(ch if ch.isprintable() else ' ' for ch in str(text))
    try:
        screen.addnstr(y, x, clean, max(0, width - x - 1), attr)
    except curses.error:
        pass


def prompt(screen, title, initial=''):
    value = initial
    screen.timeout(-1)
    try:
        curses.curs_set(1)
        while True:
            height, width = screen.getmaxyx()
            screen.move(height - 2, 0)
            screen.clrtoeol()
            label = title + ': '
            paint(screen, height - 2, 0, label + value[-max(1, width - len(label) - 2):])
            screen.refresh()
            key = screen.get_wch()
            if key in ('\n', '\r'):
                return value
            if key == '\x1b':
                return None
            if key in ('\b', '\x7f', curses.KEY_BACKSPACE):
                value = value[:-1]
            elif key == '\x15':
                value = ''
            elif isinstance(key, str) and key.isprintable():
                value += key
    finally:
        curses.curs_set(0)
        screen.timeout(200)


def spawn(cwd, command, lane='heavy', backend='local', image=''):
    env = os.environ.copy()
    package_root = str(Path(__file__).resolve().parent.parent)
    env['PYTHONPATH'] = package_root + os.pathsep + env.get('PYTHONPATH', '')
    args = [sys.executable, '-m', 'agent_builds', 'run', '--cwd', str(cwd),
            '--lane', lane, '--backend', backend, '--notify']
    if image:
        args += ['--image', image]
    args += ['--'] + command
    # Keep jobs alive when the dashboard closes; log output belongs to each job.
    return subprocess.Popen(args, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)


def main(screen, cwd):
    curses.curs_set(0)
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        for index, color in enumerate((curses.COLOR_CYAN, curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_RED), 1):
            curses.init_pair(index, color, -1)
    screen.timeout(200)
    store = Store()
    selected = 0
    message = 'n new job | v views | a announce queue policy | q quit (jobs continue)'
    children = []
    last_recovery = 0
    sampler = ProcessSampler()
    processes = []
    view = 0
    selected_id = None
    details_offset = 0
    details_job = None
    last_sample = 0
    disk_pool = ThreadPoolExecutor(max_workers=1)
    disk_future = None
    disk_result = None
    plan_rows = []
    plan_index = 0
    plan_id = None
    plan_offset = 0
    last_plans = 0
    def scan_disk(repository):
        from .disk import inventory
        reader = Store(store.root, read_only=True)
        try:
            return inventory(reader, repository)
        finally:
            reader.close()
    try:
        while True:
            children = [child for child in children if child.poll() is None]
            if time.monotonic() - last_recovery > 5:
                store.recover()
                last_recovery = time.monotonic()
            rows = store.rows()
            if selected_id:
                selected = next((index for index, row in enumerate(rows) if row['id'] == selected_id), selected)
            selected = min(selected, max(0, len(rows) - 1))
            metrics = snapshot(cwd)
            if time.monotonic() - last_sample >= 1:
                processes = sampler.sample()
                last_sample = time.monotonic()
            config = store.config()
            if view == 7 and time.monotonic()-last_plans >= 1:
                from .plans import listing
                plan_rows = listing(store, limit=20)
                plan_index = next((i for i, p in enumerate(plan_rows) if p['id'] == plan_id), min(plan_index, max(0, len(plan_rows)-1)))
                plan_id = plan_rows[plan_index]['id'] if plan_rows else None
                last_plans = time.monotonic()
            active = [r for r in rows if r['state'] in ('running', 'cancelling')]
            screen.erase()
            height, width = screen.getmaxyx()
            if height < 15 or width < 65:
                paint(screen, 0, 0, 'Agent Builds needs a terminal at least 65 columns x 15 rows.')
                paint(screen, 2, 0, 'Resize, or press q to exit. Jobs keep running.')
            else:
                context = ' | Herdr ' + os.environ.get('HERDR_PANE_ID', '') if os.environ.get('HERDR_ENV') == '1' else ''
                title = 'AGENT BUILDS v' + __version__ + (' [PAUSED]' if config['paused'] else '')
                paint(screen, 0, 0, title + ' | shared queue' + context, curses.A_BOLD | curses.color_pair(1))
                paint(screen, 1, 0, 'CPU {} logical | load {:.1f} | RAM available {:.1f}/{:.1f} GiB | disk free {:.1f} GiB'.format(
                    metrics['cpus'], metrics['load'], metrics['available_gb'], metrics['total_gb'], metrics['free_gb']))
                paint(screen, 2, 0, 'Managed reservations: {}/{} CPU slots | {:.1f}/{} GiB | {}/{} jobs | local: nice, Docker: hard limits'.format(
                    sum(r['slots'] for r in active), config['cpu_slots'], sum(r['memory_gb'] for r in active),
                    config['memory_gb'], len(active), config['max_running']))
                fmt = lambda value: 'n/a' if value is None else '{:.0f}%'.format(value)
                paint(screen, 3, 0, 'Pressure avg10: CPU {}  memory {}  I/O {} | External builds are not controlled.'.format(
                    fmt(metrics['cpu_pressure']), fmt(metrics['memory_pressure']), fmt(metrics['io_pressure'])))
                heading = 'DETAILS / TOOLS | arrows select jobs | v changes view' if view >= 3 else 'JOB           STATE       CPU RAM  BACKEND  REPOSITORY / COMMAND'
                paint(screen, 5, 0, heading, curses.A_BOLD)
                count = 0 if view >= 3 else min(8, max(3, (height - 12) // 2))
                start = max(0, selected - count + 1)
                for index, row in enumerate(rows[start:start + count], start):
                    text = '{} {:11} {:>2} {:>3g}  {:7}  {} / {}'.format(
                        row['id'], row['state'], row['slots'], row['memory_gb'], row['backend'],
                        Path(row['cwd']).name, shlex.join(json.loads(row['command'])))
                    color = 2 if row['state'] == 'succeeded' else (4 if row['state'] in ('failed', 'lost') else 3)
                    attr = curses.A_REVERSE if index == selected else curses.color_pair(color)
                    paint(screen, 6 + index - start, 0, text, attr)
                if not rows:
                    paint(screen, 6, 0, 'No jobs yet. Press n, or run: agent-builds run -- cargo check')
                log_top = 7 + count
                if view == 1:
                    paint(screen, log_top - 1, 0, 'SYSTEM PROCESSES (your user; read only; 100% CPU = one logical core)', curses.A_BOLD)
                    paint(screen, log_top, 0, 'PID     CPU%   RSS MiB  PROCESS / DIRECTORY')
                    for index, process in enumerate(processes[:max(1, height - log_top - 4)]):
                        cpu = 'warmup' if process['cpu'] is None else '{:.0f}'.format(process['cpu'])
                        paint(screen, log_top + 1 + index, 0, '{:<7} {:>6} {:>8.0f}  {} / {}'.format(
                            process['pid'], cpu, process['rss_mb'], process['name'], process['cwd']))
                elif view == 2:
                    from .announce import deliveries
                    paint(screen, log_top - 1, 0, 'ANNOUNCEMENTS | submitted means input sent; adoption is not yet confirmed', curses.A_BOLD)
                    for index, delivery in enumerate(deliveries(store)[:max(1, height - log_top - 3)]):
                        paint(screen, log_top + index, 0, '{} {:10} {} | {}'.format(
                            delivery['pane'], delivery['state'], Path(delivery['repository']).name, delivery['detail']))
                elif view == 3 and rows:
                    row = rows[selected]
                    if details_job != row['id']:
                        details_offset = 0
                        details_job = row['id']
                    origin = row['origin']
                    context = row['wait_context']
                    workers = row['worker_budget']
                    lines = [
                        '{} | {} | {}'.format(row['id'], row['state'], row['command_display']),
                        'Submitted from pane {} | tab {} | workspace {}'.format(
                            origin.get('pane_id', 'not captured'), origin.get('tab_id', '?'), origin.get('workspace_id', '?')),
                        'Reserved: {} CPU slots, {} GiB RAM'.format(row['slots'], row['memory_gb']),
                        'Queue worker defaults: CARGO_BUILD_JOBS={}, RUST_TEST_THREADS={}'.format(row['slots'], row['slots']),
                        'Cargo build setting: {} ({})'.format(workers['cargo_build']['configured_value'], workers['cargo_build']['source']),
                        'Rust test setting: {} ({})'.format(workers['rust_tests']['configured_value'], workers['rust_tests']['source']),
                        'Worker settings describe configuration; runtime concurrency is not measured.']
                    usage = row['usage']
                    if usage:
                        lines += ['Measured CPU {:.2f}s | sampled peak RSS {:.3f} GiB | {} samples'.format(
                            usage.get('cpu_seconds', 0), usage.get('sampled_peak_rss_gb', 0), usage.get('samples', 0)),
                            'RSS is sampled, may include shared pages, and may miss short peaks.']
                    else:
                        lines += ['Job resource usage has not been measured.']
                    if row['source']:
                        source = row['source']
                        lines += ['Source: submitted {} | started {}'.format(
                            source.get('submission', {}).get('head', 'unavailable'), source.get('start', {}).get('head', 'not captured')),
                            'Source drift: before start {} | during command {}'.format(
                                source.get('changed_since_submission', 'unknown'), source.get('changed_during_run', 'unknown'))]
                    if row.get('scheduling'):
                        policy = row['scheduling']
                        lines += ['Runner policy: {}'.format(policy['policy_version']),
                            'Age priority: {} | threshold {}s | current quick reserve {} CPU / {} jobs / {} GiB'.format(
                            policy['age_priority'], policy['aging_seconds'], policy['quick_reserve_slots'],
                            policy['quick_reserve_jobs'], policy['quick_reserve_memory_gb']), policy['note']]
                    if context:
                        lines += [
                            'RAM admission: {} GiB job + {} GiB headroom = {} GiB minimum available'.format(
                                context.get('job_memory_gb', row['memory_gb']), context.get('memory_headroom_gb', '?'), context.get('required_memory_gb', '?')),
                            'CPU: {} used + {} requested / {} slots'.format(
                                context.get('current_used_cpu_slots', context.get('used_cpu_slots', '?')),
                                row['slots'], context.get('total_cpu_slots', '?')),
                            'Contributing running reservations (live observation):']
                        lines += ['{}: {} CPU / {} GiB | {}'.format(job['id'], job['slots'], job['memory_gb'], job['command_display'])
                                  for job in context.get('contributing_jobs', [])]
                    paint(screen, log_top - 1, 0, 'JOB DETAILS | i returns to logs', curses.A_BOLD)
                    visible = max(1, height - log_top - 3)
                    details_offset = min(max(0, details_offset), max(0, len(lines) - visible))
                    for index, line in enumerate(lines[details_offset:details_offset + visible]):
                        paint(screen, log_top + index, 0, line)
                elif view == 4:
                    from .telemetry import statistics
                    report = statistics(store, rows[selected]['cwd'] if rows else cwd)
                    paint(screen, log_top - 1, 0, 'HISTORY | selected repository | memory suggestions never apply automatically', curses.A_BOLD)
                    for index, entry in enumerate(report['groups'][:max(1, height - log_top - 3)]):
                        paint(screen, log_top + index, 0, '{} jobs | p95 wait {:.1f}s | runtime {:.1f}s | {}'.format(
                            entry['jobs'], entry['wait_p95_seconds'], entry['average_runtime_seconds'], entry['command_display']))
                elif view == 5:
                    from .remote import dispatches
                    from .profiles import load
                    paint(screen, log_top - 1, 0, 'REMOTE WORKERS | explicit dispatch only; no automatic SSH connections', curses.A_BOLD)
                    records = dispatches(store)
                    lines = ['Configured dedicated workers: ' + ', '.join(load(store).get('workers', {}))]
                    lines += ['{} | {} | {} | {}'.format(r['handle'], r['worker'], r['state'], r.get('job_id') or 'outcome unknown') for r in records]
                    if not records:
                        lines += ['No remote submissions. No host is configured automatically.']
                    for index, line in enumerate(lines[:max(1, height - log_top - 3)]):
                        paint(screen, log_top + index, 0, line)
                elif view == 6:
                    paint(screen, log_top - 1, 0, 'DISK INVENTORY | d scans selected repo | caches are never deleted here', curses.A_BOLD)
                    if disk_future is not None and disk_future.done():
                        try:
                            disk_result = disk_future.result()
                        except (ValueError, OSError) as error:
                            message = 'Disk scan failed: ' + str(error)
                        disk_future = None
                    lines = ['Scanning...' if disk_future else 'Press d to scan the selected repository.']
                    if disk_result:
                        lines += ['{} GiB | {}'.format('{:.2f}'.format(r['bytes']/1024**3) if r['bytes'] is not None else '?', r['path']) for r in disk_result['paths']]
                    lines += ['Old queue logs: agent-builds clean (preview); --apply deletes eligible logs.']
                    for index, line in enumerate(lines[:max(1, height - log_top - 3)]):
                        paint(screen, log_top + index, 0, line)
                elif view == 7:
                    paint(screen, log_top-1, 0, 'VERIFICATION PLANS | arrows select | Enter opens a stage log', curses.A_BOLD)
                    lines = ['No plans yet. Press o to submit a JSON verification plan.']
                    if plan_rows:
                        plan = plan_rows[plan_index]
                        lines = ['{} | {} | {}'.format(plan['id'], plan['state'], plan['name']),
                                 'Stages: {} succeeded / {} total | elapsed {:.1f}s'.format(
                                     sum(s['state']=='succeeded' for s in plan['stages']), len(plan['stages']), plan['elapsed_seconds'])]
                        if plan['state'] not in ('succeeded', 'failed', 'cancelled') and not plan['controller_alive']:
                            lines += ['Controller is not running; r resumes monitoring without retrying submitted stages.']
                        if plan['reason']:
                            lines += [plan['reason']]
                        for stage in plan['stages']:
                            job = stage['job']
                            lines += ['{} {:11} | {} | {}'.format(stage['name'], stage['state'], stage['job_id'] or 'not submitted',
                                stage['reason'] or (job['reason'] if job else 'waiting for prerequisites'))]
                            if job:
                                lines += ['  waited {:.1f}s | ran {:.1f}s | exit {} | {}'.format(job['wait_seconds'], job['elapsed_seconds'], job['exit_code'], job['command_display'])]
                                if job['source'].get('changed_since_submission') or job['source'].get('changed_during_run'):
                                    lines += ['  Source changes observed; inspect plan evidence before relying on the result.']
                                elif job['started'] and job['source'].get('start', {}).get('status') != 'captured':
                                    lines += ['  Source identity unavailable or not yet captured.']
                    visible = max(1, height-log_top-3)
                    plan_offset = min(max(0, plan_offset), max(0, len(lines)-visible))
                    for index, line in enumerate(lines[plan_offset:plan_offset+visible]):
                        paint(screen, log_top+index, 0, line)
                elif rows:
                    row = rows[selected]
                    paint(screen, log_top - 1, 0, '{} | {} | {}'.format(row['id'], row['cwd'], row['reason'] or 'live output'), curses.A_BOLD)
                    offset = 0
                    context = row['wait_context']
                    if context:
                        paint(screen, log_top, 0, 'Wait {:.1f}s | disk {:.1f}/{:.1f} GiB free/needed | measured {}'.format(
                            row['wait_seconds'], context['disk_free_gb'], context['required_free_gb'],
                            time.strftime('%H:%M:%S', time.localtime(context['measured_at']))))
                        offset = 1
                        blocker = context.get('blocking_job')
                        if blocker:
                            paint(screen, log_top + offset, 0, 'Blocker {}: waited {:.1f}s, ran {:.1f}s | {} | g logs'.format(
                                blocker['id'], blocker.get('wait_seconds', 0), blocker['elapsed_seconds'], blocker['command_display']))
                            offset += 1
                    for index, line in enumerate(tail(store.log_path(row['id']), max(1, height - log_top - offset - 3))):
                        paint(screen, log_top + offset + index, 0, line)
                paint(screen, height - 2, 0, message)
                paint(screen, height - 1, 0, 'o plan  t task  i details  d disk  v views  q quit' if view != 7 else 'o plan  c cancel plan  r resume  e export  PgUp/PgDn scroll  v views  q quit')
            screen.refresh()
            key = screen.getch()
            if key in (ord('q'), 27):
                break
            selected_id = rows[selected]['id'] if rows else None
            if key in (ord('j'), curses.KEY_DOWN):
                if view == 7:
                    plan_index = min(plan_index+1, max(0, len(plan_rows)-1))
                    plan_id = plan_rows[plan_index]['id'] if plan_rows else None
                    plan_offset = 0
                else:
                    selected = min(selected + 1, max(0, len(rows) - 1))
                    selected_id = rows[selected]['id'] if rows else None
            elif key in (ord('k'), curses.KEY_UP):
                if view == 7:
                    plan_index = max(0, plan_index-1)
                    plan_id = plan_rows[plan_index]['id'] if plan_rows else None
                    plan_offset = 0
                else:
                    selected = max(0, selected - 1)
                    selected_id = rows[selected]['id'] if rows else None
            elif key == ord('v'):
                view = (view + 1) % 8
            elif key == ord('o') and height >= 15 and width >= 65:
                try:
                    from .plans import create, read_manifest, spawn as spawn_plan
                    location = prompt(screen, 'Plan checkout', rows[selected]['cwd'] if rows else str(cwd))
                    if location is None:
                        continue
                    manifest = prompt(screen, 'Verification plan JSON path', str(Path(location)/'agent-builds-plan.json'))
                    if manifest is None:
                        continue
                    definition = read_manifest(manifest, location, config)
                    plan_id = create(store, definition)
                    children.append(spawn_plan(store, plan_id))
                    view, last_plans, plan_offset = 7, 0, 0
                    message = 'Submitted verification plan '+plan_id
                except (ValueError, OSError) as error:
                    message = 'Plan rejected: '+str(error)
            elif view == 7 and key in (curses.KEY_NPAGE, curses.KEY_PPAGE):
                plan_offset += max(1, height-10)*(1 if key == curses.KEY_NPAGE else -1)
            elif view == 7 and plan_rows and key in (ord('x'), ord('c'), ord('r'), ord('e')):
                try:
                    from .plans import cancel as cancel_plan, export, spawn as spawn_plan, write_export
                    handle = plan_rows[plan_index]['id']
                    if key in (ord('x'), ord('c')):
                        if prompt(screen, 'Cancel plan {}? type yes'.format(handle)) == 'yes':
                            cancel_plan(store, handle)
                            message = 'Plan cancellation requested: '+handle
                    elif key == ord('r'):
                        children.append(spawn_plan(store, handle))
                        message = 'Controller submitted; dispatched stages are not retried.'
                    else:
                        path = prompt(screen, 'New evidence JSON file', '/tmp/agent-builds-evidence-'+handle+'.json')
                        if path:
                            write_export(path, export(store, handle))
                            message = 'Evidence exported to '+path
                    last_plans = 0
                except (ValueError, OSError) as error:
                    message = 'Plan action failed: '+str(error)
            elif view == 7 and plan_rows and key in (10, 13, curses.KEY_ENTER):
                stages = plan_rows[plan_index]['stages']
                targets = [s for s in stages if s['job'] and s['job']['state'] not in TERMINAL]
                targets += [s for s in stages if s['state'] in ('failed', 'lost') and s['job']]
                targets += list(reversed([s for s in stages if s['job']]))
                if targets:
                    target = next((i for i, row in enumerate(rows) if row['id'] == targets[0]['job_id']), None)
                    if target is not None:
                        selected, selected_id, view = target, targets[0]['job_id'], 0
                    else:
                        message = 'Stage log: agent-builds logs '+targets[0]['job_id']
            elif key == ord('d'):
                view = 6
                if disk_future is None:
                    disk_future = disk_pool.submit(scan_disk, rows[selected]['cwd'] if rows else cwd)
            elif key == ord('t') and height >= 15 and width >= 65:
                from .profiles import load, repository
                try:
                    location = prompt(screen, 'Task repository', rows[selected]['cwd'] if rows else str(cwd))
                    if location is None:
                        continue
                    prefix = repository(location) + ':'
                    tasks = [v['name'] for k, v in load(store).get('tasks', {}).items() if k.startswith(prefix)]
                    if not tasks:
                        message = 'No shortcuts. Register one with agent-builds task add NAME --cwd PATH -- COMMAND'
                        continue
                    task = prompt(screen, 'Task: ' + ', '.join(tasks), tasks[0])
                    if task not in tasks:
                        continue
                    env = os.environ.copy()
                    env['PYTHONPATH'] = str(Path(__file__).resolve().parent.parent) + os.pathsep + env.get('PYTHONPATH', '')
                    children.append(subprocess.Popen([sys.executable, '-m', 'agent_builds', 'task', 'run', task,
                        '--cwd', location, '--detach'], env=env, stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True))
                    message = 'Submitted task ' + task
                except (ValueError, OSError) as error:
                    message = 'Task failed: ' + str(error)
            elif key == ord('i'):
                view = 0 if view == 3 else 3
                details_offset = 0
            elif view == 3 and key in (curses.KEY_NPAGE, curses.KEY_PPAGE):
                details_offset += max(1, height - 10) * (1 if key == curses.KEY_NPAGE else -1)
            elif key == ord('g') and rows:
                blocker = rows[selected]['wait_context'].get('blocking_job')
                if blocker:
                    target = next((index for index, row in enumerate(rows) if row['id'] == blocker['id']), None)
                    if target is not None:
                        selected = target
                        selected_id = rows[selected]['id']
                        view = 0
                    else:
                        message = 'Blocker details: agent-builds show ' + blocker['id']
            elif key == ord('a') and height >= 15 and width >= 65:
                try:
                    from .announce import create, preview, spawn_worker
                    recipients = preview()
                    screen.erase()
                    paint(screen, 0, 0, 'Preview: ask these agents to route future builds through the queue', curses.A_BOLD)
                    paint(screen, 2, 0, 'Running Codex agents receive native queued input. Blocked agents wait.')
                    for index, agent in enumerate(recipients[:max(1, height - 7)]):
                        paint(screen, 4 + index, 0, '{} {:8} {}'.format(agent['pane_id'], agent.get('agent_status', ''), agent.get('cwd', '')))
                    screen.refresh()
                    answer = prompt(screen, 'Send queue instructions to {} agents? type yes'.format(len(recipients)))
                    if answer == 'yes':
                        announcement = create(store, recipients)
                        spawn_worker(announcement)
                        message = 'Announcement {} queued to {} agents'.format(announcement, len(recipients))
                        view = 2
                except (ValueError, OSError, subprocess.TimeoutExpired) as error:
                    message = 'Announcement failed: ' + str(error)
            elif key == ord('p'):
                result = store.update_config({'paused': not config['paused']})
                message = 'Queue paused; running jobs continue.' if result['paused'] else 'Queue resumed.'
            elif key == ord('b') and height >= 15 and width >= 65:
                changes = {}
                try:
                    for field, label, kind in [
                        ('cpu_slots', 'Total CPU slots', int),
                        ('max_running', 'Concurrent jobs', int),
                        ('max_heavy', 'Concurrent heavy jobs', int),
                        ('quick_reserve_slots', 'CPU slots reserved for quick checks', int),
                        ('quick_reserve_jobs', 'Job capacity reserved for quick checks', int),
                        ('quick_reserve_memory_gb', 'RAM GiB reserved for quick checks', float),
                        ('aging_seconds', 'Aged-job priority after seconds', float),
                        ('memory_gb', 'Total reserved RAM GiB', float),
                        ('min_free_gb', 'Minimum free disk GiB', float),
                        ('min_available_gb', 'RAM headroom GiB', float)]:
                        value = prompt(screen, label + ' (Esc aborts)', str(config[field]))
                        if value is None:
                            changes = {}
                            break
                        changes[field] = kind(value)
                    if changes:
                        store.update_config(changes)
                        message = 'Budgets saved. Running jobs keep their original reservations.'
                except (ValueError, OSError) as error:
                    message = 'Budget update rejected: ' + str(error)
            elif key == ord('c') and rows and view != 7:
                row = rows[selected]
                if row['state'] not in TERMINAL:
                    answer = prompt(screen, 'Cancel {}? type yes'.format(row['id']))
                    if answer == 'yes':
                        cancel_job(store, row['id'])
                        message = 'Cancellation requested: ' + row['id']
            elif key == ord('n') and height >= 15 and width >= 65:
                location = prompt(screen, 'Working directory', str(cwd))
                if location is None:
                    continue
                command = prompt(screen, 'Command (arguments; no implicit shell)')
                if not command:
                    continue
                lane = prompt(screen, 'Lane: quick or heavy', 'heavy')
                if lane is None:
                    continue
                backend = prompt(screen, 'Backend: local or docker', 'local')
                if backend is None:
                    continue
                image = config['docker_image']
                if backend == 'docker':
                    image = prompt(screen, 'Docker image', image)
                    if image is None:
                        continue
                try:
                    command = shlex.split(command)
                    location = Path(location).expanduser().resolve()
                    if not location.is_dir() or not command or lane not in ('quick', 'heavy') or backend not in ('local', 'docker'):
                        raise ValueError('Check directory, command, lane and backend')
                    if backend == 'docker' and not image:
                        raise ValueError('A Docker image is required')
                    children.append(spawn(location, command, lane, backend, image))
                    message = 'Submitted ' + shlex.join(command)
                    selected = 0
                except (ValueError, OSError) as error:
                    message = str(error)
    finally:
        disk_pool.shutdown(wait=False)
        store.close()


def launch(cwd):
    if not sys.stdin.isatty() or not sys.stdout.isatty() or os.environ.get('TERM') in (None, '', 'dumb'):
        raise ValueError('Open a Ghostty/Herdr terminal to use the TUI; use status --json from an agent tool')
    curses.wrapper(main, Path(cwd).expanduser().resolve())
