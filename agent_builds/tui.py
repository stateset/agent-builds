import curses
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import time
import textwrap
from concurrent.futures import ThreadPoolExecutor

from .metrics import ProcessSampler, snapshot
from . import __version__
from .runner import cancel_job
from .store import Store, TERMINAL
from .tui_browser import MODES, ORDERS, BrowserCache, SelectedJobRead, filter_rows, sort_rows, page_size, columns, job_line, label as browser_label, HELP
from .tui_logs import LogView, TailCache, strip_escapes
from .tui_details import DetailsView


def tail(path, lines):
    return TailCache().read(path, lines, time.monotonic())


def paint(screen, y, x, text, attr=0):
    height, width = screen.getmaxyx()
    if y < 0 or y >= height or x >= width - 1:
        return
    # Strip terminal controls from command output before drawing it.
    text = str(text)
    if '\x1b' in text:
        text = strip_escapes(text)
    clean = ''.join(ch if ch.isprintable() else ' ' for ch in text)
    try:
        screen.addnstr(y, x, clean, max(0, width - x - 1), attr)
    except curses.error:
        pass


def compact_lines(rows, selected, plans, plan_index, view, config, height, width, watches=None, watch_index=0, health=None, health_offset=0, workbench=None, workbench_offset=0, queue_health=None, browser=None, tail_reader=None, details_view=None, detail_row=None, detail_error=None):
    """A monitor for narrow split panes; editing actions need the full view."""
    lines = ['BUILDR ' + __version__ + (' PAUSED' if config['paused'] else '')]
    if height < 8 or width < 20:
        return lines + ['Resize: 20x8 min', 'q quit; jobs continue']
    running = [r for r in rows if r['state'] in ('running', 'cancelling')]
    reserved = queue_health['reservations'] if queue_health else dict(cpu_slots=sum(r['slots'] for r in running),memory_gb=sum(r['memory_gb'] for r in running))
    lines += ['CPU {}/{} reserved'.format(reserved['cpu_slots'], config['cpu_slots']),
              'RAM {:.1f}/{}G est.'.format(reserved['memory_gb'], config['memory_gb'])]
    if queue_health:
        from .queue_health import banner
        lines += [banner(queue_health, compact=True)]
    selected_row = None
    if view == 3:
        room=max(1,height-len(lines)-2)
        if details_view is not None and detail_row is not None:
            page=details_view.page(detail_row,max(1,width-1),room)
            lines+=['DETAILS '+details_view.position()]+page
        else:
            lines+=['DETAILS unavailable']
            lines+=textwrap.wrap(detail_error or 'No job selected',max(1,width-1))[:room]
    elif view == 12:
        from .workbench import lines as workbench_lines
        content=list(workbench_lines(workbench)) if workbench and 'error' not in workbench else [workbench.get('error','Loading workbench') if workbench else 'Loading workbench']
        wrapped=[part for line in content for part in textwrap.wrap(line,max(1,width-1))]
        room=max(1,height-len(lines)-1)
        workbench_offset=min(max(0,workbench_offset),max(0,len(wrapped)-room))
        lines+=wrapped[workbench_offset:workbench_offset+room]
    elif view == 11:
        from .diagnostics import lines as health_lines
        lines += ['HEALTH (read only)']
        if health:
            wrapped=[part for line in health_lines(health) for part in textwrap.wrap(line,max(1,width-1))]
            room=max(1,height-len(lines)-1)
            health_offset=min(max(0,health_offset),max(0,len(wrapped)-room))
            lines += wrapped[health_offset:health_offset+room]
        else:
            lines += ['Loading diagnostics']
    elif view == 10:
        watches=watches or []
        lines+=['EXTERNAL | {} shown'.format(len(watches))]
        if watches:
            row=watches[watch_index]
            lines += [row['id'],row['state'],row['definition']['external_ref'],
                      '{} probes | idle {:.1f}s'.format(row['probe_count'],row['external_idle_seconds'])]
            lines += textwrap.wrap(row['reason'],max(1,width-1))
        else:
            lines+=['No external watches']
    elif view == 7:
        lines += ['PLANS | {} shown'.format(len(plans))]
        if plans:
            selected_row = plans[plan_index]
            lines += [selected_row['id'], selected_row['state'], selected_row['name']]
            lines += ['{} {}'.format(s['name'], s['state']) for s in selected_row['stages']]
            if selected_row['reason']:
                lines += textwrap.wrap(selected_row['reason'], width-1)
        else:
            lines += ['No plans yet']
    elif view==0 and browser and browser.get('expanded'):
        lines += [browser_label(browser['mode'],browser['query'],len(rows),browser['loaded'],browser.get('order','queue'),short=True,repository=browser.get('repository'),follow=browser.get('follow',False)),' '+columns(width-1)]
        count=max(1,height-len(lines)-1)
        start=max(0,selected-count+1)
        lines += [('>' if index==selected else ' ')+job_line(row,width-1) for index,row in enumerate(rows[start:start+count],start)]
        if not rows:
            lines += ['No matching jobs']
    else:
        lines += [browser_label(browser['mode'],browser['query'],len(rows),browser['loaded'],browser.get('order','queue'),short=True,repository=browser.get('repository'),follow=browser.get('follow',False)) if browser else 'JOBS | {} shown'.format(len(rows))]
        if rows:
            selected_row = rows[selected]
            if height < 12:
                lines += [selected_row['state']+' '+selected_row['id']]
            else:
                lines += [selected_row['id'],selected_row['state']]
            if selected_row['reason']:
                lines += textwrap.wrap(selected_row['reason'], width-1)
            lines += [Path(selected_row['cwd']).name,
                      'Wait {:.1f}s run {:.1f}s'.format(selected_row['wait_seconds'], selected_row['elapsed_seconds'])]
            lines += (tail_reader or tail)(Path(selected_row['log_path']), max(1, height-len(lines)-2))
        else:
            lines += ['No matching jobs' if browser and (browser['query'] or browser['mode']!='all' or browser.get('repository') is not None) else 'No jobs yet']
    footer = 'PgUp/Dn i/l ? q' if view==3 else 'j/k scroll r back q' if view==12 else 'j/k scroll u back q' if view==11 else 'F R C Tab / f l ? q' if browser and view==0 else 'j/k move v views q'
    return lines[:height-1] + [footer]


def prompt(screen, title, initial=''):
    value = initial
    screen.timeout(-1)
    try:
        curses.curs_set(1)
        while True:
            height, width = screen.getmaxyx()
            screen.move(height - 2, 0)
            screen.clrtoeol()
            usable=max(1,width-1)
            input_room=min(12,max(1,usable//2))
            label=title[:max(0,usable-input_room-2)]+': ' if usable>=4 else ''
            paint(screen, height - 2, 0, label + value[-max(1,usable-len(label)):])
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
    message = '? help | / search | f filter | l logs | q quit (jobs continue)'
    browser_mode, browser_query = 'all',''
    browser_repository = None
    follow_head = False
    browser_order,jobs_expanded='queue',False
    help_active, help_offset = False,0
    loaded_rows, metrics, last_jobs = None,None,0
    log_view, log_job = LogView(),None
    inline_tail, browser_cache = TailCache(), BrowserCache()
    def read_tail(path, lines):
        return inline_tail.read(path, lines, time.monotonic())
    def read_details(handle):
        with store.read_snapshot():
            return store.get(handle)
    detail_read,context_read=SelectedJobRead(read_details),SelectedJobRead(store.get_wait_context)
    children = []
    last_recovery = 0
    sampler = ProcessSampler()
    processes = []
    view = 0
    selected_id = None
    details_view = DetailsView()
    last_sample = 0
    disk_pool = ThreadPoolExecutor(max_workers=1)
    disk_future = None
    disk_result = None
    plan_rows = []
    plan_index = 0
    plan_id = None
    plan_offset = 0
    last_plans = 0
    advice_result = None
    advice_offset = 0
    last_advice = 0
    health_result, health_offset, last_health = None, 0, 0
    queue_health_result, last_queue_health = None, 0
    workbench_result,workbench_future,workbench_job = None,None,None
    workbench_offset,workbench_candidate,last_workbench = 0,0,0
    group_rows = []
    group_index = 0
    group_id = None
    group_offset = 0
    last_groups = 0
    watch_rows, watch_index, watch_id, watch_offset, last_watches = [], 0, None, 0, 0
    def scan_disk(repository):
        from .disk import inventory
        reader = Store(store.root, read_only=True)
        try:
            return inventory(reader, repository)
        finally:
            reader.close()
    def inspect_job(handle):
        from .workbench import report
        reader=Store(store.root,read_only=True)
        try:
            return report(reader,handle)
        except (ValueError,OSError) as error:
            return dict(error=str(error))
        finally:
            reader.close()
    try:
        while True:
            children = [child for child in children if child.poll() is None]
            if time.monotonic() - last_recovery > 5:
                store.recover_if_due()
                last_recovery = time.monotonic()
            if loaded_rows is None or time.monotonic()-last_jobs>=1:
                with store.read_snapshot():
                    loaded_rows=store.browser_rows()
                metrics=snapshot(cwd)
                last_jobs=time.monotonic()
            rows = browser_cache.get(loaded_rows,browser_mode,browser_query,browser_order,browser_repository)
            if follow_head and view==0 and not help_active:
                selected=0
            elif selected_id:
                selected = next((index for index, row in enumerate(rows) if row['id'] == selected_id), selected)
            selected = min(selected, max(0, len(rows) - 1))
            selected_id = rows[selected]['id'] if rows else None
            process_height,process_width=screen.getmaxyx()
            full_view=process_height>=15 and process_width>=65
            detail_row=detail_read.get(selected_id if view==3 and process_height>=8 and process_width>=20 and not help_active else None,time.monotonic())
            if view!=3 or help_active or detail_row is None:
                details_view.clear()
            inline_context=context_read.get(selected_id if view==0 and full_view and not jobs_expanded and not help_active else None,time.monotonic())
            if view == 1 and not help_active and process_height>=15 and process_width>=65 and time.monotonic() - last_sample >= 1:
                processes = sampler.sample()
                last_sample = time.monotonic()
            config = store.config()
            if queue_health_result is None or time.monotonic()-last_queue_health >= 2:
                from .queue_health import report as queue_report
                reader = Store(store.root, read_only=True)
                try:
                    queue_health_result = queue_report(reader)
                finally:
                    reader.close()
                last_queue_health = time.monotonic()
            if view==12 and rows:
                desired=rows[selected]['id']
                if workbench_future is not None and workbench_future.done():
                    observed=workbench_future.result()
                    workbench_result=observed if workbench_job==desired else None
                    workbench_future=None
                if workbench_job!=desired:
                    workbench_result=None
                if workbench_future is None and (workbench_job!=desired or time.monotonic()-last_workbench>=3):
                    workbench_job=desired
                    workbench_future=disk_pool.submit(inspect_job,desired)
                    last_workbench=time.monotonic()
            if view == 11 and time.monotonic()-last_health >= 2:
                from .diagnostics import report
                reader = Store(store.root, read_only=True)
                try:
                    health_result = report(reader)
                finally:
                    reader.close()
                last_health = time.monotonic()
            if view == 7 and time.monotonic()-last_plans >= 1:
                from .plans import listing
                plan_rows = listing(store, limit=20)
                plan_index = next((i for i, p in enumerate(plan_rows) if p['id'] == plan_id), min(plan_index, max(0, len(plan_rows)-1)))
                plan_id = plan_rows[plan_index]['id'] if plan_rows else None
                last_plans = time.monotonic()
            if view == 9 and time.monotonic()-last_groups >= 1:
                from .groups import listing
                with store.read_snapshot():
                    group_rows=listing(store,limit=20)
                group_index=next((i for i,g in enumerate(group_rows) if g['id']==group_id),min(group_index,max(0,len(group_rows)-1)))
                group_id=group_rows[group_index]['id'] if group_rows else None
                last_groups=time.monotonic()
            if view == 10 and time.monotonic()-last_watches >= 1:
                from .external import listing
                with store.read_snapshot():
                    watch_rows=listing(store,limit=20)
                watch_index=next((i for i,row in enumerate(watch_rows) if row['id']==watch_id),min(watch_index,max(0,len(watch_rows)-1)))
                watch_id=watch_rows[watch_index]['id'] if watch_rows else None
                last_watches=time.monotonic()
            active = [r for r in rows if r['state'] in ('running', 'cancelling')]
            screen.erase()
            height, width = screen.getmaxyx()
            compact = height < 15 or width < 65
            if help_active:
                pass  # Only the help overlay below is visible.
            elif view == 13:
                if log_job:
                    log_job=next((row for row in loaded_rows if row['id']==log_job['id']),log_job)
                log_view.poll(time.monotonic())
                log_page=log_view.page(max(1,width-1),max(1,height-4))
                mode='LIVE' if log_view.follow else 'HELD (output only)'
                paint(screen,0,0,'LOG '+mode+' | '+log_view.position(max(1,height-4)),curses.A_BOLD | curses.color_pair(1 if log_view.follow else 3))
                paint(screen,1,0,log_job['id']+' '+log_job['state'] if log_job else 'No job selected')
                note=log_view.error or (log_view.search_label() if log_view.query else 'Space hold/resume | / find | End live')
                if log_view.clipped:
                    note='Recent 256 KiB only | '+note
                paint(screen,2,0,note)
                for index,line in enumerate(log_page):
                    match=log_view.match_kind(index)
                    paint(screen,index+3,0,line,curses.A_REVERSE if match==2 else curses.color_pair(3) if match else 0)
                paint(screen,height-1,0,'/ find  n/N match  End live  l jobs  ? help  q quit')
            elif compact:
                browser=dict(mode=browser_mode,query=browser_query,loaded=len(loaded_rows),order=browser_order,expanded=jobs_expanded,repository=browser_repository,follow=follow_head)
                for index, line in enumerate(compact_lines(rows, selected, plan_rows, plan_index, view, config, height, width, watch_rows, watch_index, health_result, health_offset,workbench_result,workbench_offset,queue_health_result,browser,read_tail,details_view,detail_row,detail_read.error)):
                    paint(screen, index, 0, line)
            else:
                context = ' | Herdr ' + os.environ.get('HERDR_PANE_ID', '') if os.environ.get('HERDR_ENV') == '1' else ''
                title = 'BUILDR v' + __version__ + (' [PAUSED]' if config['paused'] else '')
                title += ' [quick protected]' if config.get('protected_quick') else ' [protection draining]' if config.get('protected_quick_pending') else ''
                paint(screen, 0, 0, title + ' | shared queue' + context, curses.A_BOLD | curses.color_pair(1))
                paint(screen, 1, 0, 'CPU {} logical | load {:.1f} | RAM available {:.1f}/{:.1f} GiB | disk free {:.1f} GiB'.format(
                    metrics['cpus'], metrics['load'], metrics['available_gb'], metrics['total_gb'], metrics['free_gb']))
                paint(screen, 2, 0, 'Managed reservations: {}/{} CPU slots | {:.1f}/{} GiB | {}/{} jobs | local: nice, Docker: hard limits'.format(
                    queue_health_result['reservations']['cpu_slots'], config['cpu_slots'], queue_health_result['reservations']['memory_gb'],
                    config['memory_gb'], queue_health_result['reservations']['jobs'], config['max_running']))
                fmt = lambda value: 'n/a' if value is None else '{:.0f}%'.format(value)
                paint(screen, 3, 0, 'Pressure avg10: CPU {}  memory {}  I/O {} | External builds are not controlled.'.format(
                    fmt(metrics['cpu_pressure']), fmt(metrics['memory_pressure']), fmt(metrics['io_pressure'])))
                if queue_health_result:
                    from .queue_health import banner
                    color = 4 if queue_health_result['status']=='disk_blocked' else 3 if queue_health_result['waiting_jobs'] else 2
                    paint(screen, 4, 0, banner(queue_health_result), curses.A_BOLD | curses.color_pair(color))
                heading = 'DETAILS / TOOLS | arrows select jobs | ? help' if view >= 3 else browser_label(browser_mode,browser_query,len(rows),len(loaded_rows),browser_order,repository=browser_repository,follow=follow_head)+' | Tab table s sort'
                if view==3 and browser_repository is not None:
                    heading='DETAILS | repo: '+browser_repository
                paint(screen, 5, 0, heading, curses.A_BOLD)
                expanded=view==0 and jobs_expanded
                count = 0 if view >= 3 else page_size(height,expanded=expanded)
                start = max(0, selected - count + 1)
                if expanded:
                    paint(screen,6,0,columns(width),curses.A_BOLD)
                for index, row in enumerate(rows[start:start + count], start):
                    text = job_line(row,width) if expanded else '{} {:11} {:>2} {:>3g}  {:7}  {} / {}'.format(
                        row['id'], row['state'], row['slots'], row['memory_gb'], row['backend'],
                        Path(row['cwd']).name, shlex.join(json.loads(row['command'])))
                    color = 2 if row['state'] == 'succeeded' else (4 if row['state'] in ('failed', 'lost') else 3)
                    attr = curses.A_REVERSE if index == selected else curses.color_pair(color)
                    paint(screen, (7 if expanded else 6) + index - start, 0, text, attr)
                if not rows:
                    paint(screen, 7 if expanded else 6, 0, 'No matching jobs; C clears filters, R removes repository focus.' if browser_query or browser_mode!='all' or browser_repository is not None else 'No jobs yet. Press n, or run: agent-builds run -- cargo check')
                log_top = 7 + count
                if expanded:
                    if rows:
                        row=rows[selected]
                        paint(screen,height-3,0,'Selected {}/{} {} | {}'.format(selected+1,len(rows),row['id'],row['reason'] or row['cwd']))
                elif view == 12:
                    from .workbench import lines as workbench_lines
                    content=list(workbench_lines(workbench_result,workbench_candidate)) if workbench_result and 'error' not in workbench_result else [workbench_result.get('error','Loading workbench') if workbench_result else 'Loading workbench']
                    wrapped=[part for line in content for part in textwrap.wrap(line,max(1,width-1))]
                    visible=max(1,height-log_top-3)
                    workbench_offset=min(max(0,workbench_offset),max(0,len(wrapped)-visible))
                    paint(screen,log_top-1,0,'WORKBENCH | future runs, measurements and reviewed profiles',curses.A_BOLD)
                    for index,line in enumerate(wrapped[workbench_offset:workbench_offset+visible]):
                        paint(screen,log_top+index,0,line)
                elif view == 11:
                    from .diagnostics import lines as health_lines
                    lines = [part for line in health_lines(health_result) for part in (textwrap.wrap(line,max(1,width-2)) or [''])] if health_result else ['Loading diagnostics']
                    room = max(1,height-log_top-3)
                    health_offset = min(max(0,health_offset),max(0,len(lines)-room))
                    paint(screen,log_top-1,0,'HEALTH | read only | u returns to jobs | j/k or PgUp/PgDn scroll',curses.A_BOLD)
                    for index,line in enumerate(lines[health_offset:health_offset+room]):
                        paint(screen,log_top+index,0,line)
                elif view == 1:
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
                elif view == 3 and rows and detail_row is not None:
                    row = detail_row
                    page=details_view.page(row,max(1,width-1),max(1,height-log_top-3))
                    paint(screen,log_top-1,0,'JOB DETAILS | '+details_view.position()+' | i jobs',curses.A_BOLD)
                    for index,line in enumerate(page):
                        paint(screen,log_top+index,0,line)
                elif view == 3:
                    paint(screen,log_top-1,0,'JOB DETAILS | i returns to jobs',curses.A_BOLD)
                    paint(screen,log_top,0,'Details unavailable: '+(detail_read.error or 'No job selected'))
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
                elif view == 9:
                    paint(screen,log_top-1,0,'EXISTING JOB GROUPS | arrows select | Enter opens a job log | e exports evidence',curses.A_BOLD)
                    lines=['Create with: agent-builds group create JOB... --name NAME']
                    if group_rows:
                        group=group_rows[group_index]
                        lines=['{} {} | {}'.format(group['id'],group['state'],group['name']),
                               'Native job outcomes only; grouping never resubmits work or imposes dependencies.']
                        lines += ['{} {} | exit {} | waited {:.1f}s | ran {:.1f}s'.format(j['id'],j['state'],j['exit_code'],j['wait_seconds'],j['elapsed_seconds']) for j in group['jobs']]
                    visible=max(1,height-log_top-3)
                    group_offset=min(max(0,group_offset),max(0,len(lines)-visible))
                    for index,line in enumerate(lines[group_offset:group_offset+visible]):
                        paint(screen,log_top+index,0,line)
                elif view == 10:
                    from .external import lines as watch_lines
                    paint(screen,log_top-1,0,'EXTERNAL COMPLETION | idle watches hold no build reservations',curses.A_BOLD)
                    lines=['No external watches. Use buildr watch start or MCP buildr_watch_submit.']
                    if watch_rows:
                        watch=watch_rows[watch_index]
                        lines=watch_lines(watch)
                        if not watch['complete'] and not watch['controller_alive']:
                            lines+=['Controller stopped; r resumes observation of existing probe handles.']
                    lines=[part for line in lines for part in (textwrap.wrap(line,max(1,width-1)) or [''])]
                    visible=max(1,height-log_top-3)
                    watch_offset=min(max(0,watch_offset),max(0,len(lines)-visible))
                    for index,line in enumerate(lines[watch_offset:watch_offset+visible]):
                        paint(screen,log_top+index,0,line)
                elif view == 8:
                    from .advice import report, lines as advice_lines
                    if advice_result is None or time.monotonic()-last_advice >= 2:
                        advice_result = report(store)
                        last_advice = time.monotonic()
                    lines = [part for line in advice_lines(advice_result)
                             for part in (textwrap.wrap(line, max(1, width-1)) or [''])]
                    paint(screen, log_top-1, 0, 'QUEUE ADVISOR | global | scroll with arrows / PgUp / PgDn | h returns to logs', curses.A_BOLD)
                    visible = max(1, height-log_top-3)
                    advice_offset = min(max(0, advice_offset), max(0, len(lines)-visible))
                    for index, line in enumerate(lines[advice_offset:advice_offset+visible]):
                        paint(screen, log_top+index, 0, line)
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
                    context = inline_context or {}
                    if context_read.error:
                        paint(screen,log_top,0,'Wait details unavailable: '+context_read.error)
                        offset=1
                    elif context and all(key in context for key in ('disk_free_gb','required_free_gb','measured_at')):
                        paint(screen, log_top, 0, 'Wait {:.1f}s | disk {:.1f}/{:.1f} GiB free/needed | measured {}'.format(
                            row['wait_seconds'], context['disk_free_gb'], context['required_free_gb'],
                            time.strftime('%H:%M:%S', time.localtime(context['measured_at']))))
                        offset = 1
                        blocker = context.get('blocking_job')
                        if blocker:
                            paint(screen, log_top + offset, 0, 'Blocker {}: waited {:.1f}s, ran {:.1f}s | {} | g logs'.format(
                                blocker['id'], blocker.get('wait_seconds', 0), blocker['elapsed_seconds'], blocker['command_display']))
                            offset += 1
                    for index, line in enumerate(read_tail(store.log_path(row['id']), max(1, height - log_top - offset - 3))):
                        paint(screen, log_top + offset + index, 0, line)
                paint(screen, height - 2, 0, message)
                paint(screen, height - 1, 0, 'F follow  R repo  C clear  Tab table  / find  f filter  l log  ? q' if view==0 else 'PgUp/Dn Home/End  j/k job  i jobs  l logs  ? help  q quit' if view==3 else '? help  / search  f filter  s sort  l logs  i details  v views  q quit' if view!=7 else 'o plan  c cancel plan  r resume  e export  PgUp/PgDn scroll  v views  q quit')
                if view==10:
                    paint(screen,height-1,0,'c stop observation  r resume controller  e export  Enter probe log  v views  q quit')
                elif view==11:
                    paint(screen,height-1,0,'j/k scroll  PgUp/PgDn page  u jobs  v views  q quit | read only')
                elif view==12:
                    paint(screen,height-1,0,'j/k scroll  f next trial  e draft profile  t trial reviewed file  r jobs  q quit')
            if help_active:
                screen.erase()
                content=[part for line in HELP for part in textwrap.wrap(line,max(1,width-2))]
                room=max(1,height-3)
                help_offset=min(max(0,help_offset),max(0,len(content)-room))
                paint(screen,0,0,'BUILDR HELP | shared queue',curses.A_BOLD)
                for index,line in enumerate(content[help_offset:help_offset+room]):
                    paint(screen,index+1,0,line)
                paint(screen,height-1,0,'Esc/?/q back | j/k scroll')
            screen.refresh()
            key = screen.getch()
            if help_active:
                if key in (ord('?'),ord('q'),27):
                    help_active=False
                elif key in (ord('j'),curses.KEY_DOWN,curses.KEY_NPAGE):
                    help_offset+=max(1,height-3) if key==curses.KEY_NPAGE else 1
                elif key in (ord('k'),curses.KEY_UP,curses.KEY_PPAGE):
                    help_offset=max(0,help_offset-(max(1,height-3) if key==curses.KEY_PPAGE else 1))
                continue
            if key==ord('?'):
                help_active,help_offset=True,0
                continue
            if key in (ord('q'), 27):
                break
            if view==0:
                if key==ord('F'):
                    follow_head=not follow_head
                    message='Following first matching row; other input pins selection.' if follow_head else 'Selection pinned; F follows the first matching row.'
                    continue
                if follow_head and key not in (-1,curses.KEY_RESIZE,9):
                    follow_head=False
                    message='Selection pinned; F follows the first matching row.'
            if view==13:
                if key in (ord('l'),ord('v')):
                    view=0
                elif key==ord(' '):
                    log_view.toggle()
                elif key==ord('/') and height>=8 and width>=20:
                    query=prompt(screen,'Find in loaded log (blank clears)',log_view.query)
                    if query is not None:
                        log_view.search(query,max(1,width-1),max(1,height-4))
                elif key in (ord('n'),ord('N')):
                    log_view.next_match(1 if key==ord('n') else -1,max(1,width-1),max(1,height-4))
                elif key==curses.KEY_HOME:
                    log_view.home()
                elif key==curses.KEY_END:
                    log_view.end()
                elif key in (ord('j'),curses.KEY_DOWN,ord('k'),curses.KEY_UP,curses.KEY_NPAGE,curses.KEY_PPAGE):
                    amount=max(1,height-4) if key in (curses.KEY_NPAGE,curses.KEY_PPAGE) else 1
                    direction=1 if key in (ord('j'),curses.KEY_DOWN,curses.KEY_NPAGE) else -1
                    log_view.scroll(direction*amount,max(1,width-1),max(1,height-4))
                continue
            if view in (0,3) and key==ord('l'):
                log_job=rows[selected] if rows else None
                log_view.select(log_job['id'] if log_job else None,log_job['log_path'] if log_job else None)
                view=13
                continue
            if view in (0,3) and key==ord('R'):
                if browser_repository is not None:
                    browser_repository=None
                    message='Repository focus cleared; other filters stay active. C clears all filters.'
                elif rows:
                    browser_repository=rows[selected]['cwd']
                    message='Repository focus: '+browser_repository+' | R removes; C clears all filters'
                else:
                    message='No job selected. C clears filters before choosing a repository.'
                continue
            if view in (0,3) and key==ord('C'):
                browser_mode,browser_query,browser_repository='all','',None
                message='Browsing filters cleared; display order and selected job retained.'
                continue
            if view in (0,3) and key==ord('f'):
                browser_mode=MODES[(MODES.index(browser_mode)+1)%len(MODES)]
                selected,selected_id=0,None
                continue
            if view in (0,3) and key==ord('s'):
                browser_order=ORDERS[(ORDERS.index(browser_order)+1)%len(ORDERS)]
                continue
            if view==0 and key==9:
                jobs_expanded=not jobs_expanded
                continue
            if view in (0,3) and key==ord('/') and height>=8 and width>=20:
                query=prompt(screen,'Search (blank clears)',browser_query)
                if query is not None:
                    browser_query=query[:256]
                    selected,selected_id=0,None
                continue
            if view==0 and key in (curses.KEY_HOME,curses.KEY_END,curses.KEY_NPAGE,curses.KEY_PPAGE):
                page=page_size(height,compact,jobs_expanded)
                if key==curses.KEY_HOME:
                    selected=0
                elif key==curses.KEY_END:
                    selected=max(0,len(rows)-1)
                else:
                    delta=page if key==curses.KEY_NPAGE else -page
                    selected=min(max(0,len(rows)-1),max(0,selected+delta))
                selected_id=rows[selected]['id'] if rows else None
                continue
            if view in (0,3) and key in (ord('i'),10,13,curses.KEY_ENTER) and height>=8 and width>=20:
                view=0 if view==3 else 3
                if view==3:
                    details_view.home()
                continue
            if view==3 and key in (curses.KEY_HOME,curses.KEY_END,curses.KEY_NPAGE,curses.KEY_PPAGE):
                if key==curses.KEY_HOME:
                    details_view.home()
                elif key==curses.KEY_END:
                    details_view.end()
                else:
                    details_view.scroll(1 if key==curses.KEY_NPAGE else -1)
                continue
            if compact and key not in (ord('j'), ord('k'), curses.KEY_DOWN, curses.KEY_UP, ord('v'), ord('u'),ord('r')):
                continue
            if key==ord('r') and view in (0,3,12):
                view=0 if view==12 else 12
                workbench_offset,workbench_candidate,last_workbench=0,0,0
                continue
            if view==12:
                if key in (ord('j'),curses.KEY_DOWN,curses.KEY_NPAGE):
                    workbench_offset+=max(1,height-10) if key==curses.KEY_NPAGE else 1
                elif key in (ord('k'),curses.KEY_UP,curses.KEY_PPAGE):
                    workbench_offset=max(0,workbench_offset-(max(1,height-10) if key==curses.KEY_PPAGE else 1))
                elif key==ord('v'):
                    view=0
                elif not compact and key==ord('f') and workbench_result and workbench_result.get('candidates'):
                    workbench_candidate=(workbench_candidate+1)%len(workbench_result['candidates'])
                    workbench_offset=0
                elif not compact and key==ord('e') and workbench_result and workbench_result.get('candidates'):
                    candidates=workbench_result['candidates']
                    candidate=candidates[workbench_candidate%len(candidates)]
                    name=prompt(screen,'Saved task name','reviewed-task')
                    output=prompt(screen,'New proposal file','/tmp/buildr-'+candidate['anchor_job']+'-'+candidate['kind']+'.json') if name else None
                    if name and output:
                        reader=Store(store.root,read_only=True)
                        try:
                            from .task_review import propose
                            from .plans import write_export
                            proposal=propose(reader,candidate['anchor_job'],candidate['kind'],name)
                            target=Path(output).expanduser().absolute()
                            if Path(proposal['source']['repository']) in target.parents:
                                raise ValueError('Save proposals outside the source checkout')
                            write_export(target,proposal)
                            message='Draft saved: '+str(target)+'; review before using t to trial it'
                        except (ValueError,OSError) as error:
                            message=str(error)
                        finally:
                            reader.close()
                elif not compact and key==ord('t'):
                    filename=prompt(screen,'Reviewed proposal file to trial')
                    if filename:
                        try:
                            from .task_review import trial
                            handle=trial(store,filename,emit=False,processes=children)
                            message='Profile trial plan '+handle+' submitted; stages use the shared queue'
                        except (ValueError,OSError) as error:
                            message=str(error)
                continue
            if key == ord('u'):
                view = 0 if view == 11 else 11
                health_offset,last_health = 0,0
                continue
            if view == 11:
                if key in (ord('j'),curses.KEY_DOWN,curses.KEY_NPAGE):
                    health_offset += max(1,height-10) if key==curses.KEY_NPAGE else 1
                elif key in (ord('k'),curses.KEY_UP,curses.KEY_PPAGE):
                    health_offset = max(0,health_offset-(max(1,height-10) if key==curses.KEY_PPAGE else 1))
                elif key == ord('v'):
                    view = 0
                continue
            if view == 8 and not compact and key not in (ord('j'), ord('k'), curses.KEY_DOWN, curses.KEY_UP,
                                                       curses.KEY_NPAGE, curses.KEY_PPAGE, ord('v'), ord('h')):
                continue
            if view == 9 and not compact and key not in (ord('j'),ord('k'),curses.KEY_DOWN,curses.KEY_UP,
                curses.KEY_NPAGE,curses.KEY_PPAGE,ord('v'),ord('h'),ord('e'),10,13,curses.KEY_ENTER):
                continue
            if view == 10 and not compact and key not in (ord('j'),ord('k'),curses.KEY_DOWN,curses.KEY_UP,
                curses.KEY_NPAGE,curses.KEY_PPAGE,ord('v'),ord('h'),ord('e'),ord('r'),ord('c'),10,13,curses.KEY_ENTER):
                continue
            if view==10 and not watch_rows and key not in (ord('v'),ord('h')):
                continue
            selected_id = rows[selected]['id'] if rows else None
            if key in (ord('j'), curses.KEY_DOWN):
                if view == 10:
                    watch_index=min(watch_index+1,max(0,len(watch_rows)-1))
                    watch_id=watch_rows[watch_index]['id'] if watch_rows else None
                    watch_offset=0
                elif view == 9 and not compact:
                    group_index=min(group_index+1,max(0,len(group_rows)-1))
                    group_id=group_rows[group_index]['id'] if group_rows else None
                    group_offset=0
                elif view == 8 and not compact:
                    advice_offset += 1
                elif view == 7:
                    plan_index = min(plan_index+1, max(0, len(plan_rows)-1))
                    plan_id = plan_rows[plan_index]['id'] if plan_rows else None
                    plan_offset = 0
                else:
                    selected = min(selected + 1, max(0, len(rows) - 1))
                    selected_id = rows[selected]['id'] if rows else None
            elif key in (ord('k'), curses.KEY_UP):
                if view == 10:
                    watch_index=max(0,watch_index-1)
                    watch_id=watch_rows[watch_index]['id'] if watch_rows else None
                    watch_offset=0
                elif view == 9 and not compact:
                    group_index=max(0,group_index-1)
                    group_id=group_rows[group_index]['id'] if group_rows else None
                    group_offset=0
                elif view == 8 and not compact:
                    advice_offset = max(0, advice_offset-1)
                elif view == 7:
                    plan_index = max(0, plan_index-1)
                    plan_id = plan_rows[plan_index]['id'] if plan_rows else None
                    plan_offset = 0
                else:
                    selected = max(0, selected - 1)
                    selected_id = rows[selected]['id'] if rows else None
            elif key == ord('v'):
                view = ({0:7,7:10,10:11,11:12,12:0}.get(view,0)) if compact else (view + 1) % 13
            elif key == ord('h'):
                view = 0 if view == 8 else 8
                advice_offset, last_advice = 0, 0
            elif view == 8 and key in (curses.KEY_NPAGE, curses.KEY_PPAGE):
                advice_offset += max(1, height-10)*(1 if key == curses.KEY_NPAGE else -1)
            elif view == 9 and key in (curses.KEY_NPAGE,curses.KEY_PPAGE):
                group_offset+=max(1,height-10)*(1 if key==curses.KEY_NPAGE else -1)
            elif view == 10 and key in (curses.KEY_NPAGE,curses.KEY_PPAGE):
                watch_offset+=max(1,height-10)*(1 if key==curses.KEY_NPAGE else -1)
            elif view == 10 and watch_rows and key in (ord('c'),ord('r'),ord('e')):
                from . import external
                from .plans import write_export
                handle=watch_rows[watch_index]['id']
                try:
                    if key==ord('c'):
                        if prompt(screen,'Stop observing {}? type yes'.format(handle))=='yes':
                            child=external.cancel(store,handle)
                            if child is not None:
                                children.append(child)
                            message='Watch observation cancelled; external task continues'
                    elif key==ord('r'):
                        children.append(external.spawn(store,handle))
                        message='Watch controller resumed; existing probe handles retained'
                    else:
                        path=prompt(screen,'New watch evidence JSON file','/tmp/buildr-watch-'+handle+'.json')
                        if path:
                            write_export(path,external.get(store,handle,limit=1000))
                            message='Watch evidence exported to '+path
                    last_watches=0
                except (ValueError,OSError) as error:
                    message='Watch action failed: '+str(error)
            elif view == 10 and watch_rows and key in (10,13,curses.KEY_ENTER):
                probes=watch_rows[watch_index]['probes']
                if probes and probes[-1]['job_id']:
                    handle=probes[-1]['job_id']
                    target=next((i for i,row in enumerate(rows) if row['id']==handle),None)
                    if target is None:
                        message='Probe log: buildr logs '+handle
                    else:
                        selected,selected_id,view=target,handle,0
            elif view == 9 and group_rows and key==ord('e'):
                from .groups import export
                from .plans import write_export
                try:
                    path=prompt(screen,'New group evidence JSON file','/tmp/agent-builds-group-'+group_rows[group_index]['id']+'.json')
                    if path:
                        write_export(path,export(store,group_rows[group_index]['id']))
                        message='Group evidence exported to '+path
                except (ValueError,OSError) as error:
                    message='Group export failed: '+str(error)
            elif view == 9 and group_rows and key in (10,13,curses.KEY_ENTER):
                jobs=group_rows[group_index]['jobs']
                targets=[j for j in jobs if j['state'] not in TERMINAL]+[j for j in jobs if j['state'] in ('failed','lost')]+list(reversed(jobs))
                if targets:
                    target=next((i for i,row in enumerate(rows) if row['id']==targets[0]['id']),None)
                    if target is not None:
                        selected,selected_id,view=target,targets[0]['id'],0
                    else:
                        message='Job log: agent-builds logs '+targets[0]['id']
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
                from .profiles import tasks as saved_tasks, repository
                try:
                    location = prompt(screen, 'Task repository', rows[selected]['cwd'] if rows else str(cwd))
                    if location is None:
                        continue
                    prefix = repository(location) + ':'
                    tasks = [v['name'] for k, v in saved_tasks(store).items() if k.startswith(prefix)]
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
                details_view.home()
            elif key == ord('g') and rows:
                try:
                    blocker = store.get_wait_context(rows[selected]['id']).get('blocking_job')
                except (ValueError,OSError,sqlite3.Error) as error:
                    message='Blocker unavailable: '+str(error)
                    continue
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
                    from .announce import create, preview, select, spawn_worker
                    recipients = preview()
                    screen.erase()
                    paint(screen, 0, 0, 'Preview: ask these agents to route future builds through the queue', curses.A_BOLD)
                    paint(screen, 2, 0, 'Only explicitly selected idle recipients receive native agent input.')
                    for index, agent in enumerate(recipients[:max(1, height - 7)]):
                        paint(screen, 4 + index, 0, '{} {:8} {}'.format(agent['pane_id'], agent.get('agent_status', ''), agent.get('cwd', '')))
                    screen.refresh()
                    panes = prompt(screen, 'Select exact idle pane IDs, separated by spaces (blank cancels)')
                    if not panes:
                        continue
                    recipients = select(recipients, panes.split())
                    answer = prompt(screen, 'Send to '+', '.join(agent['pane_id'] for agent in recipients)+'? type yes')
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
