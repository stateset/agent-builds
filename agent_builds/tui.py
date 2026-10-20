import curses
import json
import os
import re
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
import time
import textwrap

from .metrics import ProcessSampler, snapshot
from .host_pressure import pressure_summary
from . import __version__
from .runner import cancel_job
from .store import Store, TERMINAL
from .tui_browser import MODES, ORDERS, BrowserCache, SelectedJobRead, filter_rows, sort_rows, page_size, columns, job_line, label as browser_label, HELP
from .tui_logs import LogView, TailCache, TAIL_RECHECK_SECONDS, strip_escapes
from .tui_details import DetailsView
from .tui_history import HistoryRead, HistoryView, inspect_history
from .tui_prompt import edit as edit_prompt, job_context, recipient_context
from .tui_views import VIEWS, choose as choose_view, footer as view_footer
from .tui_reads import SnapshotRead, submit_read
from .tui_reads import submit_read as submit_log_read
from .tui_reads import submit_read as submit_log_status_read
from .tui_log_reads import LogReads
from .tui_links import LogLinks
from .tui_plan_stages import PlanStages
from .tui_plan_details import PlanDetails
from .tui_plan_filter import PlanFilter
from .tui_help import HelpView
from .tui_reads import submit_read as submit_link_read
from .tui_submissions import SubmissionTracker
from .tui_reads import submit_read as submit_submission_read
from .tui_submission_view import SubmissionView, notice as submission_notice, compact_notice
from .tui_disk import DiskRead
from .tui_data import LazyStore, browser as read_browser, selected as read_selected, summary as read_summary, tool as read_tool
from .tui_inspect import InspectionFocus, inspect_report, HealthView, WorkbenchView, WhyView


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


def compact_lines(rows, selected, plans, plan_index, view, config, height, width, watches=None, watch_index=0, health=None, health_offset=0, workbench=None, workbench_offset=0, queue_health=None, browser=None, tail_reader=None, details_view=None, detail_row=None, detail_error=None, history_view=None, history_repository=None, history_report=None, history_status="", inspection=None, history_error=None, plan_stages=None, plan_details=None):
    """A monitor for narrow split panes; editing actions need the full view."""
    lines = ['BUILDR ' + __version__ + (' PAUSED' if config['paused'] else '')]
    if height < 8 or width < 20:
        return lines + ['Resize: 20x8 min', 'q quit; jobs continue']
    if browser and browser.get('queue_error'):
        lines[0]='BUILDR STALE jobs'
    elif browser and not browser.get('initialized',True):
        lines[0]='BUILDR no queue'
    if queue_health is None or not queue_health.get('initialized',True):
        lines += ['CPU reservations ?','RAM reservations ?']
    else:
        reserved=queue_health['reservations']
        stale='STALE ' if browser and browser.get('summary_error') else ''
        lines += [stale+'CPU {}/{} reserved'.format(reserved['cpu_slots'], config['cpu_slots']),
                  stale+'RAM {:.1f}/{}G est.'.format(reserved['memory_gb'], config['memory_gb'])]
    if queue_health:
        from .queue_health import dashboard_banner
        lines += [dashboard_banner(queue_health,width,stale=bool(browser and browser.get('summary_error'))) if queue_health.get('initialized',True) else 'Queue not initialized']
    elif browser:
        lines += [browser.get('summary_status','Queue summary unavailable')]
    selected_row = None
    if VIEWS[view].full:
        lines += [VIEWS[view].name.upper(), 'Resize to 65x15', 'V chooses a view']
    elif view == 4:
        room=max(1,height-len(lines)-3)
        page=history_view.display_page(history_repository,history_report,max(1,width-1),room,history_error) if history_view else []
        lines += ["HISTORY " + (history_view.position() if history_view else ""),history_view.status_label(history_status) if history_view else history_status]+page
    elif view == 3:
        room=max(1,height-len(lines)-2)
        if details_view is not None and detail_row is not None:
            page=details_view.page(detail_row,max(1,width-1),room)
            lines+=['DETAILS '+details_view.position()]+page
        else:
            lines+=['DETAILS unavailable']
            lines+=textwrap.wrap(detail_error or 'No job selected',max(1,width-1))[:room]
    elif view in (11,12,14):
        legacy_offset=(health_offset if view==11 else workbench_offset) if inspection is None else 0
        if inspection is None:
            widget=HealthView() if view==11 else WorkbenchView() if view==12 else WhyView()
            inspection=dict(widget=widget,scope='global' if view==11 else (None,0),report=health if view==11 else workbench,status='Read only',error=None,job=None)
        widget=inspection['widget']
        room=max(1,height-len(lines)-3)
        page=widget.display_page(inspection['scope'],inspection['report'],max(1,width-1),room,inspection['error'])
        if legacy_offset:
            widget.offset=legacy_offset
            page=widget.display_page(inspection['scope'],inspection['report'],max(1,width-1),room,inspection['error'])
        title=('WHY ' if view==14 else 'WORK ')+inspection['job']['id'] if view in (12,14) and inspection['job'] else VIEWS[view].name.upper()+' '+widget.position()
        lines += [title,widget.status_label(inspection['status'])]+page
    elif view == 10:
        watches=watches or []
        lines+=[browser.get('tool_heading') if browser and browser.get('tool_heading') else 'EXTERNAL | {} shown'.format(len(watches))]
        if watches:
            row=watches[watch_index]
            lines += [row['id'],row['state']]
        if browser and browser.get('tool_status') and (not watches or height>=10):
            lines+=textwrap.wrap(browser['tool_status'],max(1,width-1))[:max(1,height-len(lines)-2)]
        if watches:
            lines += [row['definition']['external_ref'],
                      '{} probes | idle {:.1f}s'.format(row['probe_count'],row['external_idle_seconds'])]
            lines += textwrap.wrap(row['reason'],max(1,width-1))
        else:
            lines+=['No external watches']
    elif view == 7:
        stage_lines = plan_stages.lines(plans[plan_index]) if plan_stages and plans else []
        if stage_lines:
            lines += stage_lines[:2]
        lines += [browser.get('tool_heading') if browser and browser.get('tool_heading') else 'PLANS | {} shown'.format(len(plans))]
        if stage_lines and plan_details:
            if browser and browser.get('tool_status') and height>=10:
                lines += textwrap.wrap(browser['tool_status'],max(1,width-1))[:1]
            room=max(0,height-len(lines)-1)
            if room:
                lines += plan_details.page_plan(plans[plan_index],max(1,width-1),room)
            return lines[:height-1]+[view_footer(view,width,compact=True)]
        if not plans and browser and browser.get('plan_empty'):
            lines += browser['plan_empty']
            return lines[:height-1]+[view_footer(view,width,compact=True)]
        if plans:
            selected_row = plans[plan_index]
            lines += [selected_row['id'], selected_row['state']]
        if browser and browser.get('tool_status') and (not plans or height>=10):
            lines+=textwrap.wrap(browser['tool_status'],max(1,width-1))[:max(1,height-len(lines)-2)]
        if plans:
            lines += [selected_row['name']]
            lines += ['{} {}'.format(s['name'], s['state']) for s in selected_row['stages']]
            if selected_row['reason']:
                lines += textwrap.wrap(selected_row['reason'], width-1)
        else:
            lines += ['No plans yet']
    elif view==0 and browser and browser.get('expanded') and (not browser.get('submission_notice') or height>=9):
        lines += [browser_label(browser['mode'],browser['query'],len(rows),browser['loaded'],browser.get('order','queue'),short=True,repository=browser.get('repository'),follow=browser.get('follow',False)),' '+columns(width-1)]
        count=max(1,height-len(lines)-1-bool(browser.get('submission_notice')))
        start=max(0,selected-count+1)
        lines += [('>' if index==selected else ' ')+job_line(row,width-1) for index,row in enumerate(rows[start:start+count],start)]
        if not rows:
            lines += ['No matching jobs']
    else:
        lines += [browser_label(browser['mode'],browser['query'],len(rows),browser['loaded'],browser.get('order','queue'),short=True,repository=browser.get('repository'),follow=browser.get('follow',False)) if browser else 'JOBS | {} shown'.format(len(rows))]
        if rows:
            selected_row = rows[selected]
            if height < 12:
                lines += [selected_row['id']+' '+selected_row['state']]
            else:
                lines += [selected_row['id'],selected_row['state']]
            if selected_row['reason']:
                lines += textwrap.wrap(selected_row['reason'], width-1)
            lines += [Path(selected_row['cwd']).name,
                      'Wait {:.1f}s run {:.1f}s'.format(selected_row['wait_seconds'], selected_row['elapsed_seconds'])]
            room=height-len(lines)-2
            if room>0:
                lines += (tail_reader or tail)(Path(selected_row['log_path']),room)
        else:
            lines += ['No matching jobs' if browser and (browser['query'] or browser['mode']!='all' or browser.get('repository') is not None) else 'No jobs yet']
    footer = view_footer(view, width, compact=True)
    notification=browser.get('submission_notice') if browser and view==0 else None
    if notification:
        body=lines[:height-2]
        return body+['']*max(0,height-2-len(body))+[notification,footer]
    return lines[:height-1] + [footer]


def prompt(screen, title, initial='', context=''):
    return edit_prompt(screen, title, initial, context, paint)


def poll_read(reader, key, now, retain=True):
    """Adopt an already completed read without ever waiting in the event loop."""
    value=reader.get(key,now)
    if reader.future is not None and reader.future.done():
        value=reader.get(key,now)
    if not retain and reader.error:
        reader.report=None
        return None
    return value


def main(screen, cwd):
    curses.curs_set(0)
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        for index, color in enumerate((curses.COLOR_CYAN, curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_RED), 1):
            curses.init_pair(index, color, -1)
    screen.timeout(200)
    store = LazyStore()
    selected = 0
    focus = InspectionFocus()
    message = 'V views | ? help | / search | f filter | l logs | q quit (jobs continue)'
    browser_mode, browser_query = 'all',''
    browser_repository = None
    follow_head = False
    browser_order,jobs_expanded='queue',False
    help_active, help_view = False,HelpView(HELP)
    loaded_rows, metrics = [],None
    browser_read=SnapshotRead(lambda key: read_browser(store.root),interval=1,submit=submit_read,label='queue snapshot')
    metrics_read=SnapshotRead(lambda key: snapshot(cwd),interval=1,submit=submit_read,label='host metrics')
    summary_read=SnapshotRead(lambda key: read_summary(store.root),interval=2,submit=submit_read,label='queue summary')
    log_view, log_job = LogView(),None
    inline_tail, browser_cache = TailCache(), BrowserCache()
    log_reads=LogReads(submit=submit_log_read)
    log_visible=False
    def refresh_log(mode, job, path, cache, now, force=False):
        key=(mode,job,str(path))
        result=log_reads.get(key,cache.signature,force,now)
        if result is None and log_reads.future is not None and log_reads.future.done():
            result=log_reads.get(key,cache.signature,force,now)
        if result is not None:
            cache.apply(result,now)
    def read_tail(path, lines):
        nonlocal log_visible
        if lines<=0:
            return []
        log_visible=True
        inline_tail.select(selected_id,path)
        now=time.monotonic()
        refresh_log('inline',selected_id,path,inline_tail,now,now-inline_tail.last_content_read>=TAIL_RECHECK_SECONDS)
        status=inline_tail.error
        if status is None and inline_tail.signature is None:
            status='Loading log; navigation stays available'
        elif status is None and now-inline_tail.last_poll>1:
            status='STALE log; refreshing'
        return ([status]+inline_tail.page(lines-1)) if status else inline_tail.page(lines)
    detail_read=SnapshotRead(lambda handle: read_selected(store.root,('details',handle)),interval=1,submit=submit_read,label='job details')
    context_read=SnapshotRead(lambda handle: read_selected(store.root,('context',handle)),interval=1,submit=submit_read,label='wait details')
    log_status_read=SnapshotRead(lambda handle: read_selected(store.root,('control',handle)),interval=1,submit=submit_log_status_read,label='job state')
    log_links=LogLinks(lambda key: read_selected(store.root,key),submit=submit_link_read)
    direct_log_view=None
    log_return=(0,None,None)
    def remember_log_origin():
        nonlocal log_return
        if view!=13:
            log_return=(view,context_row,history_scope)
    children = []
    submissions=SubmissionTracker(store.root,submit=submit_submission_read)
    submission_view,submission_rows=SubmissionView(),()
    sampler = ProcessSampler()
    processes = []
    view = 0
    selected_id = None
    details_view = DetailsView()
    process_read=SnapshotRead(lambda key: sampler.sample(),interval=1,submit=submit_read,label='processes')
    tool_read=SnapshotRead(lambda key: read_tool(store.root,key),interval=1,submit=submit_read,label='tool report')
    history_read = HistoryRead(
        lambda repository: inspect_history(store.root, repository)
    )
    history_view = HistoryView()
    history_scope = None
    inspection_read = SnapshotRead(lambda key: inspect_report(store.root,key))
    inspection_views = {11:HealthView(),12:WorkbenchView(),14:WhyView()}
    disk_future = None
    disk_result = None
    disk_reader = DiskRead(store.root)
    plan_rows = []
    plan_index = 0
    plan_id = None
    plan_stages = PlanStages()
    plan_filter = PlanFilter()
    plan_details = PlanDetails(plan_stages)
    advice_result = None
    advice_offset = 0
    queue_health_result = None
    workbench_result,workbench_candidate = None,0
    group_rows = []
    group_index = 0
    group_id = None
    group_offset = 0
    watch_rows, watch_index, watch_id, watch_offset = [], 0, None, 0
    try:
        while True:
            children = [child for child in children if child.poll() is None]
            now=time.monotonic()
            submission_updates=submissions.poll(now)
            if submission_updates:
                submission_rows=submissions.statuses
                message=submission_notice(submission_updates[0]) if len(submission_updates)==1 else '{} submission updates | U submissions'.format(len(submission_updates))
            browser_report=browser_read.report if help_active else poll_read(browser_read,'queue',now)
            loaded_rows=browser_report['rows'] if browser_report is not None else []
            config=browser_report['config'] if browser_report is not None else None
            metrics=metrics_read.report if help_active else poll_read(metrics_read,'host',now)
            queue_health_result=summary_read.report if help_active else poll_read(summary_read,'summary',now)
            browser_stale=bool(browser_read.error or (browser_read.observed is not None and now-browser_read.observed>2*browser_read.interval))
            summary_stale=bool(summary_read.error or (summary_read.observed is not None and now-summary_read.observed>2*summary_read.interval))
            rows = browser_cache.get(loaded_rows,browser_mode,browser_query,browser_order,browser_repository)
            if follow_head and view==0 and not help_active:
                selected=0
            elif selected_id:
                selected = next((index for index, row in enumerate(rows) if row['id'] == selected_id), selected)
            selected = min(selected, max(0, len(rows) - 1))
            selected_id = rows[selected]['id'] if rows else None
            browser_row=rows[selected] if rows else None
            inspection_row=focus.get(view,browser_row,loaded_rows)
            context_row=log_job if view==13 else inspection_row or browser_row
            process_height,process_width=screen.getmaxyx()
            log_status_key=log_job['id'] if log_job and view==13 and not help_active and process_height>=8 and process_width>=20 else None
            log_status=poll_read(log_status_read,log_status_key,now)
            state_note=''
            if log_status_key:
                valid_state=isinstance(log_status,dict) and log_status.get('id')==log_status_key and isinstance(log_status.get('state'),str)
                if valid_state and log_status['state']!=log_job['state']:
                    log_job=dict(log_job,state=log_status['state'])
                    context_row=log_job
                if log_status_read.error:
                    state_note='STALE state: '+log_status_read.error
                elif log_status is None:
                    state_note='State loading'
                elif not valid_state:
                    state_note='STALE state: invalid reply'
                elif now-log_status_read.observed>2*log_status_read.interval:
                    state_note='STALE state; refreshing'
            full_view=process_height>=15 and process_width>=65
            detail_row=poll_read(detail_read,inspection_row['id'] if inspection_row and view==3 and process_height>=8 and process_width>=20 and not help_active else None,time.monotonic(),retain=False)
            if view!=3 or help_active or detail_row is None:
                details_view.clear()
            inline_context=poll_read(context_read,selected_id if view==0 and full_view and not jobs_expanded and not help_active else None,time.monotonic(),retain=False)
            processes=poll_read(process_read,'processes' if view==1 and full_view and not help_active and browser_report is not None else None,time.monotonic()) or []
            if view!=4:
                history_scope=None
            elif history_scope is None:
                history_scope=context_row['cwd'] if context_row else str(cwd)
            history_repository=history_scope
            history_visible = (view == 4 and not help_active
                               and process_height >= 8 and process_width >= 20)
            history_report = history_read.get(
                history_repository if history_visible else None, time.monotonic()
            )
            history_status = history_read.status(time.monotonic())
            readable=not help_active and process_height>=8 and process_width>=20
            kind={11:'health',12:'workbench',14:'why'}.get(view)
            inspection_read.label={'health':'diagnostics','workbench':'workbench','why':'job diagnosis'}.get(kind,'inspection')
            inspection_read.interval=3 if kind=='workbench' else 2
            inspect_key=(kind,inspection_row['id'] if inspection_row else None) if kind and readable and (kind=='health' or inspection_row) else None
            inspection_data=inspection_read.get(inspect_key,time.monotonic())
            inspection_status=inspection_read.status(time.monotonic()) if kind=='health' or inspection_row else 'No job selected'
            inspection_error=inspection_read.error if kind=='health' or inspection_row else 'No job selected'
            workbench_result=inspection_data if kind=='workbench' else None
            inspection=None
            if kind:
                scope=(inspection_row['id'] if inspection_row else None,workbench_candidate) if kind=='workbench' else 'global' if kind=='health' else inspection_row['id'] if inspection_row else None
                inspection=dict(widget=inspection_views[view],scope=scope,report=inspection_data,status=inspection_status,error=inspection_error,job=inspection_row)
            tool_kind={2:'announcements',5:'workers',7:'plans',8:'advisor',9:'groups',10:'watches'}.get(view)
            tool_read.label=tool_kind or 'tool report'
            tool_read.interval=2 if view==8 else 1
            tool_key=(tool_kind,None) if tool_kind and readable and browser_report is not None and (not VIEWS[view].full or full_view) else None
            tool_data=poll_read(tool_read,tool_key,time.monotonic())
            tool_status=tool_read.status(time.monotonic())
            if view==7:
                plan_rows=plan_filter.get(tool_data)
                plan_index=next((i for i,p in enumerate(plan_rows) if p['id']==plan_id),min(plan_index,max(0,len(plan_rows)-1)))
                if tool_data is not None:
                    plan_id=plan_rows[plan_index]['id'] if plan_rows else None
                    plan_stages.current(plan_rows[plan_index] if plan_rows else None)
            elif view==9:
                group_rows=tool_data if tool_data is not None else []
                group_index=next((i for i,g in enumerate(group_rows) if g['id']==group_id),min(group_index,max(0,len(group_rows)-1)))
                if tool_data is not None:
                    group_id=group_rows[group_index]['id'] if group_rows else None
            elif view==10:
                watch_rows=tool_data if tool_data is not None else []
                watch_index=next((i for i,row in enumerate(watch_rows) if row['id']==watch_id),min(watch_index,max(0,len(watch_rows)-1)))
                if tool_data is not None:
                    watch_id=watch_rows[watch_index]['id'] if watch_rows else None
            elif view==8:
                advice_result=tool_data
            link_origin=(view,watch_id if view==10 else group_id if view==9 else plan_id if view==7 else context_row['id'] if context_row else None)
            if view==7:
                link_origin=(view,plan_id,plan_stages.name,plan_stages.log_target(plan_rows[plan_index]) if plan_rows and plan_stages.name is not None else None)
            if view==15:
                submission_view.current(submission_rows)
                link_origin=(view,submission_view.selected_key)
            direct_lookup=direct_log_view==view
            linked_job=log_links.poll(('jump',view) if direct_lookup else link_origin,readable and (direct_lookup or not VIEWS[view].full or full_view),time.monotonic())
            if linked_job is not None:
                remember_log_origin()
                direct_log_view=None
                log_job=context_row=linked_job
                log_view.select(log_job['id'],log_job['log_path'])
                view=13
                follow_head=False
                state_note='State loading'
            if disk_future is not None and disk_future.done():
                try:
                    disk_result = disk_future.result()
                except Exception as error:
                    message = 'Disk scan failed: ' + str(error)
                finally:
                    disk_reader.close()
                disk_future = None
            active = [r for r in rows if r['state'] in ('running', 'cancelling')]
            screen.erase()
            height, width = screen.getmaxyx()
            compact = height < 15 or width < 65
            log_visible=False
            if help_active:
                pass  # Only the help overlay below is visible.
            elif view==15:
                if height<8 or width<20:
                    paint(screen,1,0,'Resize: 20x8 min')
                else:
                    page=submission_view.page(None,submission_rows,max(1,width-1),max(1,height-3))
                    for index,line in enumerate(page):
                        paint(screen,index+1,0,line)
                receipt_label=submission_view.selection_label(submission_rows)
                title='SUBMISSIONS '+receipt_label if receipt_label else 'SUBMISSIONS'
                paint(screen,0,0,title+(' | '+submission_view.position() if width>=40 or not receipt_label else ''),curses.A_BOLD)
                if height>=8 and width>=20:
                    paint(screen,height-2,0,submission_view.action_note(submission_rows))
                paint(screen,height-1,0,view_footer(15,width))
            elif browser_report is None and view!=13:
                paint(screen,0,0,'BUILDR '+__version__,curses.A_BOLD)
                pending=browser_read.status(time.monotonic())
                for index,line in enumerate(textwrap.wrap(pending,max(1,width-1))[:max(1,height-3)]):
                    paint(screen,index+1,0,line)
                paint(screen,height-1,0,'V views ? help q quit' if width>=24 else 'V ? q')
            elif view == 13:
                if log_view.follow and log_job and height>=8 and width>=20:
                    log_visible=True
                    now=time.monotonic()
                    refresh_log('focused',log_job['id'],log_view.path,log_view,now,log_view.content_due(now))
                log_page=log_view.page(max(1,width-1),max(1,height-4))
                mode='LIVE' if log_view.follow else 'HELD (output only)'
                paint(screen,0,0,'LOG '+mode+' | '+log_view.position(max(1,height-4)),curses.A_BOLD | curses.color_pair(1 if log_view.follow else 3))
                paint(screen,1,0,log_job['id']+' '+log_job['state'] if log_job else 'No job selected')
                note='No job selected' if log_job is None else log_view.error or (log_view.search_label() if log_view.query else 'Space hold/resume | / find | End live')
                if log_view.follow and log_job is not None and log_view.error is None:
                    if log_view.signature is None:
                        note='Loading log; navigation stays available'
                    elif log_view.observed is not None and time.monotonic()-log_view.observed>1:
                        note='STALE log; refreshing | '+note
                if log_view.clipped:
                    note='Recent 256 KiB only | '+note
                if state_note:
                    note=state_note+' | '+note
                paint(screen,2,0,note)
                if not log_page:
                    paint(screen,3,0,log_view.empty_notice(),curses.A_DIM)
                for index,line in enumerate(log_page):
                    match=log_view.match_kind(index)
                    paint(screen,index+3,0,line,curses.A_REVERSE if match==2 else curses.color_pair(3) if match else 0)
                paint(screen,height-1,0,view_footer(13,width))
            elif compact:
                browser=dict(mode=browser_mode,query=browser_query,loaded=len(loaded_rows),order=browser_order,expanded=jobs_expanded,repository=browser_repository,follow=follow_head,initialized=browser_report['initialized'],queue_error=browser_stale,queue_age=browser_read.status(time.monotonic()),summary_status=summary_read.status(time.monotonic()),summary_error=summary_stale,tool_status=tool_status if view in (7,10) and (tool_data is None or tool_read.error) else '',tool_heading=tool_read.status(time.monotonic(),short_label='PLANS' if view==7 else 'EXTERNAL') if view in (7,10) else '')
                if view==7 and plan_filter.query:
                    browser['tool_heading']=plan_filter.label(browser['tool_heading'],tool_data,compact=True)
                    if not plan_rows:
                        browser['plan_empty']=plan_filter.empty(tool_data,tool_read.error)
                if submission_rows:
                    browser['submission_notice']=compact_notice(submission_rows[-1])
                for index, line in enumerate(compact_lines(rows, selected, plan_rows, plan_index, view, config, height, width, watch_rows, watch_index, None,0,workbench_result,0,queue_health_result,browser,read_tail,details_view,detail_row,detail_read.error or (detail_read.status(time.monotonic()) if inspection_row else None),history_view,history_repository,history_report,history_status,inspection,history_error=history_read.error,plan_stages=plan_stages,plan_details=plan_details)):
                    paint(screen, index, 0, line)
            else:
                context = ' | Herdr ' + os.environ.get('HERDR_PANE_ID', '') if os.environ.get('HERDR_ENV') == '1' else ''
                title = 'BUILDR v' + __version__ + (' [PAUSED]' if config['paused'] else '')
                title += ' [STALE queue]' if browser_stale else ' [not initialized]' if not browser_report['initialized'] else ''
                title += ' [quick protected]' if config.get('protected_quick') else ' [protection draining]' if config.get('protected_quick_pending') else ''
                paint(screen, 0, 0, title+' | '+browser_read.status(time.monotonic())+' | shared queue'+context, curses.A_BOLD | curses.color_pair(1))
                if metrics is None:
                    paint(screen,1,0,metrics_read.status(time.monotonic()))
                    paint(screen,3,0,'Pressure: unknown | External builds are not controlled.')
                else:
                    host_line='{} CPU | load {:.1f} | RAM {:.1f}/{:.1f} GiB | disk {:.1f} GiB'.format(
                        metrics['cpus'],metrics['load'],metrics['available_gb'],metrics['total_gb'],metrics['free_gb'])
                    paint(screen,1,0,metrics_read.status(time.monotonic())+' | '+host_line)
                    paint(screen,3,0,pressure_summary(metrics)+' | External builds are not controlled.')
                if not browser_report['initialized']:
                    paint(screen,2,0,'Managed reservations: unknown | Queue not initialized')
                    paint(screen,4,0,'Queue not initialized; explicit queue actions create state.')
                elif queue_health_result is None or not queue_health_result.get('initialized',True):
                    summary_note=summary_read.status(time.monotonic()) if queue_health_result is None else 'Queue not initialized in summary snapshot; refreshing'
                    paint(screen,2,0,'Managed reservations: unknown | '+summary_note)
                    paint(screen,4,0,summary_note)
                else:
                    paint(screen,2,0,('STALE summary | ' if summary_stale else '')+'Managed reservations: {}/{} CPU slots | {:.1f}/{} GiB | {}/{} jobs | local: nice, Docker: hard limits'.format(
                        queue_health_result['reservations']['cpu_slots'],config['cpu_slots'],queue_health_result['reservations']['memory_gb'],
                        config['memory_gb'],queue_health_result['reservations']['jobs'],config['max_running']))
                    from .queue_health import dashboard_banner
                    color=4 if queue_health_result['status']=='disk_blocked' else 3 if queue_health_result['waiting_jobs'] else 2
                    paint(screen,4,0,dashboard_banner(queue_health_result,width,stale=summary_stale,status=summary_read.status(time.monotonic())),curses.A_BOLD|curses.color_pair(color))
                heading = 'DETAILS / TOOLS | arrows select jobs | ? help' if view >= 3 else browser_label(browser_mode,browser_query,len(rows),len(loaded_rows),browser_order,repository=browser_repository,follow=follow_head)+' | Tab table s sort'
                if view in (11,12,14):
                    heading=VIEWS[view].name.upper()+' | j/k scroll | ? help'
                if inspection_row and view in (3,12,14):
                    heading+=' | '+inspection_row['id']
                if view==4:
                    heading='HISTORY | selected checkout | j/k scroll | ? help'
                if view==3 and browser_repository is not None:
                    heading='DETAILS | repo: '+browser_repository
                if browser_read.error:
                    heading=browser_read.status(time.monotonic())+' | '+heading
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
                elif view in (11,12,14):
                    widget=inspection['widget']
                    page=widget.display_page(inspection['scope'],inspection_data,max(1,width-1),max(1,height-log_top-4),inspection_error)
                    paint(screen,log_top-1,0,VIEWS[view].name.upper()+' | '+widget.position(),curses.A_BOLD)
                    paint(screen,log_top,0,widget.status_label(inspection_status))
                    for index,line in enumerate(page):
                        paint(screen,log_top+1+index,0,line)
                elif view == 1:
                    paint(screen, log_top - 1, 0, 'SYSTEM PROCESSES (your user; read only; 100% CPU = one logical core)', curses.A_BOLD)
                    paint(screen, log_top, 0, process_read.status(time.monotonic()))
                    paint(screen, log_top + 1, 0, 'PID     CPU%   RSS MiB  PROCESS / DIRECTORY')
                    for index, process in enumerate(processes[:max(1, height - log_top - 5)]):
                        cpu = 'warmup' if process['cpu'] is None else '{:.0f}'.format(process['cpu'])
                        paint(screen, log_top + 2 + index, 0, '{:<7} {:>6} {:>8.0f}  {} / {}'.format(
                            process['pid'], cpu, process['rss_mb'], process['name'], process['cwd']))
                elif view == 2:
                    paint(screen, log_top - 1, 0, 'ANNOUNCEMENTS | submitted means input sent; adoption is not yet confirmed', curses.A_BOLD)
                    paint(screen,log_top,0,tool_status)
                    for index, delivery in enumerate((tool_data or [])[:max(1, height - log_top - 4)]):
                        paint(screen, log_top + 1 + index, 0, '{} {:10} {} | {}'.format(
                            delivery['pane'], delivery['state'], Path(delivery['repository']).name, delivery['detail']))
                elif view == 3 and detail_row is not None:
                    row = detail_row
                    page=details_view.page(row,max(1,width-1),max(1,height-log_top-3))
                    paint(screen,log_top-1,0,'JOB DETAILS | '+details_view.position()+' | '+detail_read.status(time.monotonic())+' | i jobs',curses.A_BOLD)
                    for index,line in enumerate(page):
                        paint(screen,log_top+index,0,line)
                elif view == 3:
                    paint(screen,log_top-1,0,'JOB DETAILS | i returns to jobs',curses.A_BOLD)
                    paint(screen,log_top,0,'Details unavailable: '+(detail_read.error or detail_read.status(time.monotonic()) if inspection_row else 'No job selected'))
                elif view == 4:
                    page=history_view.display_page(history_repository,history_report,max(1,width-1),max(1,height-log_top-4),history_read.error)
                    paint(screen,log_top-1,0,'HISTORY | read only | '+history_view.position(),curses.A_BOLD)
                    paint(screen,log_top,0,history_view.status_label(history_status))
                    for index,line in enumerate(page):
                        paint(screen,log_top+1+index,0,line)
                elif view == 5:
                    paint(screen, log_top - 1, 0, 'REMOTE WORKERS | explicit dispatch only; no automatic SSH connections', curses.A_BOLD)
                    records=tool_data['records'] if tool_data else []
                    lines=[tool_status]
                    if tool_data:
                        lines+=['Configured dedicated workers: '+', '.join(tool_data['workers'])]
                        lines+=['{} | {} | {} | {}'.format(r['handle'],r['worker'],r['state'],r.get('job_id') or 'outcome unknown') for r in records]
                    if tool_data is not None and not records:
                        lines += ['No remote submissions. No host is configured automatically.']
                    for index, line in enumerate(lines[:max(1, height - log_top - 3)]):
                        paint(screen, log_top + index, 0, line)
                elif view == 6:
                    paint(screen, log_top - 1, 0, 'DISK INVENTORY | d scans selected repo | caches are never deleted here', curses.A_BOLD)
                    lines = ['Scanning...' if disk_future else 'Press d to scan the selected repository.']
                    if disk_result:
                        lines += ['{} GiB | {}'.format('{:.2f}'.format(r['bytes']/1024**3) if r['bytes'] is not None else '?', r['path']) for r in disk_result['paths']]
                    lines += ['Old queue logs: agent-builds clean (preview); --apply deletes eligible logs.']
                    for index, line in enumerate(lines[:max(1, height - log_top - 3)]):
                        paint(screen, log_top + index, 0, line)
                elif view == 9:
                    paint(screen,log_top-1,0,'EXISTING JOB GROUPS | arrows select | Enter opens a job log | e exports evidence',curses.A_BOLD)
                    paint(screen,log_top,0,tool_status)
                    lines=['Create with: agent-builds group create JOB... --name NAME'] if tool_data is not None else []
                    if group_rows:
                        group=group_rows[group_index]
                        lines=['{} {} | {}'.format(group['id'],group['state'],group['name']),
                               'Native job outcomes only; grouping never resubmits work or imposes dependencies.']
                        lines += ['{} {} | exit {} | waited {:.1f}s | ran {:.1f}s'.format(j['id'],j['state'],j['exit_code'],j['wait_seconds'],j['elapsed_seconds']) for j in group['jobs']]
                    visible=max(1,height-log_top-4)
                    group_offset=min(max(0,group_offset),max(0,len(lines)-visible))
                    for index,line in enumerate(lines[group_offset:group_offset+visible]):
                        paint(screen,log_top+1+index,0,line)
                elif view == 10:
                    from .external import lines as watch_lines
                    paint(screen,log_top-1,0,'EXTERNAL COMPLETION | idle watches hold no build reservations',curses.A_BOLD)
                    paint(screen,log_top,0,tool_status)
                    lines=['No external watches. Use buildr watch start or MCP buildr_watch_submit.'] if tool_data is not None else []
                    if watch_rows:
                        watch=watch_rows[watch_index]
                        lines=watch_lines(watch)
                        if not watch['complete'] and watch['controller_alive'] is False:
                            lines+=['Controller stopped; r resumes observation of existing probe handles.']
                        elif not watch['complete'] and watch['controller_alive'] is None:
                            lines+=['Controller liveness is not verified by read-only inspection.']
                    lines=[part for line in lines for part in (textwrap.wrap(line,max(1,width-1)) or [''])]
                    visible=max(1,height-log_top-4)
                    watch_offset=min(max(0,watch_offset),max(0,len(lines)-visible))
                    for index,line in enumerate(lines[watch_offset:watch_offset+visible]):
                        paint(screen,log_top+1+index,0,line)
                elif view == 8:
                    from .advice import lines as advice_lines
                    content=list(advice_lines(advice_result)) if advice_result is not None else []
                    lines=[part for line in content for part in (textwrap.wrap(line,max(1,width-1)) or [''])]
                    paint(screen, log_top-1, 0, 'QUEUE ADVISOR | global | scroll with arrows / PgUp / PgDn | h returns to logs', curses.A_BOLD)
                    paint(screen,log_top,0,tool_status)
                    visible = max(1, height-log_top-4)
                    advice_offset = min(max(0, advice_offset), max(0, len(lines)-visible))
                    for index, line in enumerate(lines[advice_offset:advice_offset+visible]):
                        paint(screen, log_top+1+index, 0, line)
                elif view == 7:
                    paint(screen, log_top-1, 0, 'VERIFICATION PLANS | / find | arrows plan | Tab stage | Enter log', curses.A_BOLD)
                    paint(screen,log_top,0,plan_filter.label(tool_status,tool_data))
                    prefix=plan_stages.lines(plan_rows[plan_index])[:2] if plan_rows else []
                    for index,line in enumerate(prefix):
                        paint(screen,log_top+1+index,0,line,curses.A_BOLD)
                    visible=max(1,height-log_top-4-len(prefix))
                    if plan_rows:
                        lines=plan_details.page_plan(plan_rows[plan_index],max(1,width-1),visible)
                    else:
                        lines=plan_filter.empty(tool_data,tool_read.error) if plan_filter.query else ['No plans yet. Press o to submit a JSON verification plan.'] if tool_data is not None else []
                    for index,line in enumerate(lines):
                        paint(screen,log_top+1+len(prefix)+index,0,line)
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
                    for index, line in enumerate(read_tail(Path(row['log_path']), max(1, height - log_top - offset - 3))):
                        paint(screen, log_top + offset + index, 0, line)
                paint(screen, height - 2, 0, 'Read only | runner observations are not an ETA or a new admission decision.' if view==14 else message)
                paint(screen, height - 1, 0, view_footer(view, width))
            if help_active:
                screen.erase()
                query_rows=1 if help_view.query else 0
                content=help_view.page(max(1,width-2),max(1,height-3-query_rows))
                paint(screen,0,0,'BUILDR HELP | '+help_view.position(),curses.A_BOLD)
                if help_view.query:
                    paint(screen,1,0,'Find: '+help_view.query)
                for index,line in enumerate(content):
                    paint(screen,index+1+query_rows,0,line)
                if not content:
                    paint(screen,1+query_rows,0,'No matching help')
                paint(screen,height-2,0,'/ search | blank clears' if width>=25 else '/ find blank clears')
                paint(screen,height-1,0,'Esc/?/q back | j/k PgUp/PgDn Home/End' if width>=40 else 'Esc back j/k Pg ? q')
            elif log_links.status and height>=8 and width>=20:
                paint(screen,height-2,0,log_links.status)
            if not log_visible:
                log_reads.get(None,None,False,time.monotonic())
            screen.refresh()
            key = screen.getch()
            if help_active:
                if key in (ord('?'),ord('q'),27):
                    help_active=False
                elif key in (ord('j'),curses.KEY_DOWN,curses.KEY_NPAGE):
                    help_view.scroll(1,key==curses.KEY_NPAGE)
                elif key in (ord('k'),curses.KEY_UP,curses.KEY_PPAGE):
                    help_view.scroll(-1,key==curses.KEY_PPAGE)
                elif key==curses.KEY_HOME:
                    help_view.home()
                elif key==curses.KEY_END:
                    help_view.end()
                elif key==ord('/') and height>=8 and width>=20:
                    query=prompt(screen,'Find help (blank clears)',help_view.query)
                    if query is not None:
                        help_view.search(query)
                continue
            if key==ord('?'):
                help_active=True
                continue
            if key not in (-1,curses.KEY_RESIZE):
                log_links.cancel()
                direct_log_view=None
            if key in (ord('q'), 27):
                break
            if key==ord('J'):
                if height>=8 and width>=20:
                    handle=prompt(screen,'Open log: full job ID')
                    if handle is not None and handle.strip():
                        handle=handle.strip().lower()
                        if re.fullmatch('[0-9a-f]{12}',handle) is None:
                            log_links.error='Use the full 12-character job ID'
                        else:
                            direct_log_view=view
                            log_links.open('browser',handle,('jump',view),time.monotonic())
                            follow_head=False
                continue
            if key==ord('V'):
                if height>=8 and width>=20:
                    target=choose_view(screen,view,context_row is not None,paint,has_log=view==13 and log_job is not None)
                    if target is not None:
                        if target==13 and view!=13:
                            remember_log_origin()
                            log_job=context_row
                            log_view.select(log_job['id'] if log_job else None,log_job['log_path'] if log_job else None)
                        if target in (3,12,14):
                            focus.select(context_row)
                        if target==4 and view!=4:
                            history_scope=context_row['cwd'] if context_row else str(cwd)
                        if target==3 and view!=3:
                            details_view.home()
                        if target!=0 and follow_head:
                            follow_head=False
                            message='Selection pinned; F follows the first matching row.'
                        view=target
                continue
            if key==ord('U') and height>=8 and width>=20:
                if view!=15:
                    follow_head=False
                view=0 if view==15 else 15
                continue
            if view==7 and key==ord('/'):
                if height>=8 and width>=20:
                    query=prompt(screen,'Find loaded plans (blank clears)',plan_filter.query)
                    if query is not None:
                        plan_filter.search(query)
                continue
            if view in (4,11,12,14) and key==ord('/'):
                if height>=8 and width>=20:
                    widget=history_view if view==4 else inspection_views[view]
                    query=prompt(screen,'Find loaded report (blank clears)',widget.query)
                    if query is not None:
                        widget.search(query)
                continue
            if view==15:
                if key in (9,curses.KEY_BTAB) and height>=8 and width>=20:
                    submission_view.choose(submission_rows,1 if key==9 else -1)
                elif key in (10,13,curses.KEY_ENTER) and height>=8 and width>=20:
                    target=submission_view.log_target(submission_rows)
                    if target:
                        log_links.open('browser',target,(view,submission_view.selected_key),time.monotonic())
                elif key in (ord('j'),curses.KEY_DOWN,ord('k'),curses.KEY_UP,curses.KEY_NPAGE,curses.KEY_PPAGE):
                    submission_view.scroll(1 if key in (ord('j'),curses.KEY_DOWN,curses.KEY_NPAGE) else -1,key in (curses.KEY_NPAGE,curses.KEY_PPAGE))
                elif key==curses.KEY_HOME:
                    submission_view.home()
                elif key==curses.KEY_END:
                    submission_view.end()
                elif key==ord('v'):
                    view=0
                continue
            if browser_report is None and view!=13:
                continue
            if view==7 and key in (ord('['),ord(']')) and height>=8 and width>=20:
                if plan_stages.issue(plan_rows[plan_index] if plan_rows else None,1 if key==ord(']') else -1):
                    plan_details.home()
                continue
            if view==7 and key in (9,curses.KEY_BTAB) and height>=8 and width>=20:
                plan_stages.choose(plan_rows[plan_index] if plan_rows else None,1 if key==9 else -1)
                plan_details.home()
                continue
            if view==0:
                if key==ord('F'):
                    follow_head=not follow_head
                    message='Following first matching row; other input pins selection.' if follow_head else 'Selection pinned; F follows the first matching row.'
                    continue
                if follow_head and key not in (-1,curses.KEY_RESIZE,9):
                    follow_head=False
                    message='Selection pinned; F follows the first matching row.'
            if key==ord('H') and height>=8 and width>=20:
                if view!=4:
                    history_scope=context_row['cwd'] if context_row else str(cwd)
                view=0 if view==4 else 4
                continue
            if compact and VIEWS[view].full:
                if key==ord('v'):
                    view=0
                continue
            if view==4:
                if key in (ord('j'),curses.KEY_DOWN,ord('k'),curses.KEY_UP,curses.KEY_NPAGE,curses.KEY_PPAGE):
                    history_view.scroll(1 if key in (ord('j'),curses.KEY_DOWN,curses.KEY_NPAGE) else -1,key in (curses.KEY_NPAGE,curses.KEY_PPAGE))
                elif key==curses.KEY_HOME:
                    history_view.home()
                elif key==curses.KEY_END:
                    history_view.end()
                elif key==ord('v'):
                    view=0 if compact else 5
                continue
            if key==ord('w') and view in (0,3,14) and height>=8 and width>=20:
                if view==14:
                    view=0
                else:
                    focus.select(context_row)
                    view=14
                continue
            if view==14:
                widget=inspection_views[14]
                if key in (ord('j'),curses.KEY_DOWN,ord('k'),curses.KEY_UP,curses.KEY_NPAGE,curses.KEY_PPAGE):
                    widget.scroll(1 if key in (ord('j'),curses.KEY_DOWN,curses.KEY_NPAGE) else -1,key in (curses.KEY_NPAGE,curses.KEY_PPAGE))
                elif key==curses.KEY_HOME:
                    widget.home()
                elif key==curses.KEY_END:
                    widget.end()
                elif key in (ord('v'),):
                    view=0
                elif key==ord('i'):
                    view=3
                    details_view.home()
                elif key==ord('l'):
                    remember_log_origin()
                    log_job=inspection_row
                    log_view.select(log_job['id'] if log_job else None,log_job['log_path'] if log_job else None)
                    view=13
                elif key==ord('g') and inspection_row:
                    links=inspection_data['jobs'][0]['blocker_chain']['jobs'] if inspection_data and inspection_data['jobs'] else []
                    log_links.open('browser' if len(links)>1 else 'blocker',links[1]['id'] if len(links)>1 else inspection_row['id'],link_origin,time.monotonic())
                    follow_head=False
                continue
            if view==13:
                if key==ord('b') and height>=8 and width>=20:
                    view,return_job,history_scope=log_return
                    if view in (3,12,14):
                        focus.select(return_job)
                elif key in (ord('l'),ord('v')):
                    view=0
                elif key==ord(' '):
                    log_reads.invalidate()
                    log_view.toggle()
                elif key==ord('/') and height>=8 and width>=20:
                    log_reads.invalidate()
                    query=prompt(screen,'Find in loaded log (blank clears)',log_view.query)
                    if query is not None:
                        log_view.search(query,max(1,width-1),max(1,height-4))
                elif key in (ord('n'),ord('N')):
                    log_reads.invalidate()
                    log_view.next_match(1 if key==ord('n') else -1,max(1,width-1),max(1,height-4))
                elif key==curses.KEY_HOME:
                    log_reads.invalidate()
                    log_view.home()
                elif key==curses.KEY_END:
                    log_reads.invalidate()
                    log_view.end()
                elif key in (ord('j'),curses.KEY_DOWN,ord('k'),curses.KEY_UP,curses.KEY_NPAGE,curses.KEY_PPAGE):
                    log_reads.invalidate()
                    amount=max(1,height-4) if key in (curses.KEY_NPAGE,curses.KEY_PPAGE) else 1
                    direction=1 if key in (ord('j'),curses.KEY_DOWN,curses.KEY_NPAGE) else -1
                    log_view.scroll(direction*amount,max(1,width-1),max(1,height-4))
                continue
            if view in (0,3) and key==ord('l'):
                remember_log_origin()
                log_job=context_row
                log_view.select(log_job['id'] if log_job else None,log_job['log_path'] if log_job else None)
                view=13
                continue
            if height>=8 and width>=20:
                link_handle=None
                if view in (0,3) and key==ord('g') and context_row:
                    log_links.open('blocker',context_row['id'],link_origin,time.monotonic())
                    follow_head=False
                    continue
                if key in (10,13,curses.KEY_ENTER):
                    if view==10 and watch_rows:
                        probes=watch_rows[watch_index]['probes']
                        link_handle=probes[-1]['job_id'] if probes else None
                    elif view==9 and group_rows and not compact:
                        jobs=group_rows[group_index]['jobs']
                        targets=[job for job in jobs if job['state'] not in TERMINAL]+[job for job in jobs if job['state'] in ('failed','lost')]+list(reversed(jobs))
                        link_handle=targets[0]['id'] if targets else None
                    elif view==7 and plan_rows:
                        stages=plan_rows[plan_index]['stages']
                        targets=[stage for stage in stages if stage['job'] and stage['job']['state'] not in TERMINAL]
                        targets += [stage for stage in stages if stage['state'] in ('failed','lost') and stage['job']]
                        targets += list(reversed([stage for stage in stages if stage['job']]))
                        link_handle=plan_stages.log_target(plan_rows[plan_index]) if plan_stages.name is not None else targets[0]['job_id'] if targets else None
                    if link_handle:
                        log_links.open('browser',link_handle,link_origin,time.monotonic())
                        follow_head=False
                        continue
            if view in (0,3) and key==ord('R'):
                focus.select(None)
                if browser_repository is not None:
                    browser_repository=None
                    message='Repository focus cleared; other filters stay active. C clears all filters.'
                elif context_row:
                    browser_repository=context_row['cwd']
                    message='Repository focus: '+browser_repository+' | R removes; C clears all filters'
                else:
                    message='No job selected. C clears filters before choosing a repository.'
                continue
            if view in (0,3) and key==ord('C'):
                focus.select(None)
                browser_mode,browser_query,browser_repository='all','',None
                message='Browsing filters cleared; display order and selected job retained.'
                continue
            if view in (0,3) and key==ord('f'):
                focus.select(None)
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
                    focus.select(None)
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
                if view==0:
                    focus.select(context_row)
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
            if compact and key not in (ord('j'), ord('k'), curses.KEY_DOWN, curses.KEY_UP, ord('v'), ord('u'),ord('r'),curses.KEY_HOME,curses.KEY_END,curses.KEY_NPAGE,curses.KEY_PPAGE):
                continue
            if key==ord('r') and view in (0,3,12):
                if view!=12:
                    focus.select(context_row)
                view=0 if view==12 else 12
                workbench_candidate=0
                inspection_views[12].home()
                continue
            if view==12:
                if key in (ord('j'),curses.KEY_DOWN,curses.KEY_NPAGE):
                    inspection_views[12].scroll(1,key==curses.KEY_NPAGE)
                elif key in (ord('k'),curses.KEY_UP,curses.KEY_PPAGE):
                    inspection_views[12].scroll(-1,key==curses.KEY_PPAGE)
                elif key==curses.KEY_HOME:
                    inspection_views[12].home()
                elif key==curses.KEY_END:
                    inspection_views[12].end()
                elif key==ord('v'):
                    view=0
                elif not compact and key==ord('f') and workbench_result and workbench_result.get('candidates'):
                    workbench_candidate=(workbench_candidate+1)%len(workbench_result['candidates'])
                    inspection_views[12].home()
                elif not compact and key==ord('e') and workbench_result and workbench_result.get('candidates'):
                    candidates=workbench_result['candidates']
                    candidate=candidates[workbench_candidate%len(candidates)]
                    name=prompt(screen,'Saved task name','reviewed-task')
                    output=prompt(screen,'New proposal file','/tmp/buildr-'+candidate['anchor_job']+'-'+candidate['kind']+'.json') if name else None
                    if name and output:
                        reader=None
                        try:
                            reader=Store(store.root,read_only=True)
                            from .task_review import propose
                            from .plans import write_export
                            proposal=propose(reader,candidate['anchor_job'],candidate['kind'],name)
                            target=Path(output).expanduser().absolute()
                            if Path(proposal['source']['repository']) in target.parents:
                                raise ValueError('Save proposals outside the source checkout')
                            write_export(target,proposal)
                            message='Draft saved: '+str(target)+'; review before using t to trial it'
                        except (ValueError,OSError,sqlite3.Error) as error:
                            message=str(error)
                        finally:
                            if reader is not None:
                                reader.close()
                elif not compact and key==ord('t'):
                    filename=prompt(screen,'Reviewed proposal file to trial')
                    if filename:
                        try:
                            from .task_review import trial
                            handle=trial(store.writable(),filename,emit=False,processes=children)
                            message='Profile trial plan '+handle+' submitted; stages use the shared queue'
                        except (ValueError,OSError,sqlite3.Error) as error:
                            message=str(error)
                continue
            if key == ord('u'):
                view = 0 if view == 11 else 11
                inspection_views[11].home()
                continue
            if view == 11:
                if key in (ord('j'),curses.KEY_DOWN,curses.KEY_NPAGE):
                    inspection_views[11].scroll(1,key==curses.KEY_NPAGE)
                elif key in (ord('k'),curses.KEY_UP,curses.KEY_PPAGE):
                    inspection_views[11].scroll(-1,key==curses.KEY_PPAGE)
                elif key==curses.KEY_HOME:
                    inspection_views[11].home()
                elif key==curses.KEY_END:
                    inspection_views[11].end()
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
                    plan_details.home()
                else:
                    selected = focus.move(rows,1) if view==3 else min(selected + 1, max(0, len(rows) - 1))
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
                    plan_details.home()
                else:
                    selected = focus.move(rows,-1) if view==3 else max(0, selected - 1)
                    selected_id = rows[selected]['id'] if rows else None
            elif key == ord('v'):
                if view==3:
                    history_scope=context_row['cwd'] if context_row else str(cwd)
                if view==11:
                    focus.select(context_row)
                view = ({0:7,7:10,10:11,11:12,12:0}.get(view,0)) if compact else (view + 1) % 13
            elif key == ord('h'):
                view = 0 if view == 8 else 8
                advice_offset=0
                tool_read.next_read=0
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
                            child=external.cancel(store.writable(),handle)
                            if child is not None:
                                children.append(child)
                            message='Watch observation cancelled; external task continues'
                    elif key==ord('r'):
                        children.append(external.spawn(store.writable(),handle))
                        message='Watch controller resumed; existing probe handles retained'
                    else:
                        path=prompt(screen,'New watch evidence JSON file','/tmp/buildr-watch-'+handle+'.json')
                        if path:
                            write_export(path,external.get(store,handle,limit=1000))
                            message='Watch evidence exported to '+path
                    tool_read.next_read=0
                except (ValueError,OSError,sqlite3.Error) as error:
                    message='Watch action failed: '+str(error)
            elif view == 9 and group_rows and key==ord('e'):
                from .groups import export
                from .plans import write_export
                try:
                    path=prompt(screen,'New group evidence JSON file','/tmp/agent-builds-group-'+group_rows[group_index]['id']+'.json')
                    if path:
                        write_export(path,export(store,group_rows[group_index]['id']))
                        message='Group evidence exported to '+path
                except (ValueError,OSError,sqlite3.Error) as error:
                    message='Group export failed: '+str(error)
            elif key == ord('o') and height >= 15 and width >= 65:
                try:
                    from .plans import create, read_manifest, spawn as spawn_plan
                    location = prompt(screen, 'Plan checkout', context_row['cwd'] if context_row else str(cwd))
                    if location is None:
                        continue
                    manifest = prompt(screen, 'Verification plan JSON path', str(Path(location)/'agent-builds-plan.json'))
                    if manifest is None:
                        continue
                    definition = read_manifest(manifest, location, store.writable().config())
                    plan_id = create(store.writable(), definition)
                    children.append(spawn_plan(store.writable(), plan_id))
                    view = 7
                    plan_filter.search('')
                    plan_details.home()
                    tool_read.next_read=0
                    message = 'Submitted verification plan '+plan_id
                except (ValueError, OSError, sqlite3.Error) as error:
                    message = 'Plan rejected: '+str(error)
            elif view == 7 and key in (curses.KEY_NPAGE, curses.KEY_PPAGE,curses.KEY_HOME,curses.KEY_END):
                if not compact or (plan_stages.name is not None and height>8):
                    if key==curses.KEY_HOME:
                        plan_details.home()
                    elif key==curses.KEY_END:
                        plan_details.end()
                    else:
                        plan_details.scroll(1 if key==curses.KEY_NPAGE else -1,pages=True)
            elif view == 7 and plan_rows and key in (ord('x'), ord('c'), ord('r'), ord('e')):
                try:
                    from .plans import cancel as cancel_plan, export, spawn as spawn_plan, write_export
                    handle = plan_rows[plan_index]['id']
                    if key in (ord('x'), ord('c')):
                        if prompt(screen, 'Cancel plan {}? type yes'.format(handle)) == 'yes':
                            cancel_plan(store.writable(), handle)
                            message = 'Plan cancellation requested: '+handle
                    elif key == ord('r'):
                        children.append(spawn_plan(store.writable(), handle))
                        message = 'Controller submitted; dispatched stages are not retried.'
                    else:
                        path = prompt(screen, 'New evidence JSON file', '/tmp/agent-builds-evidence-'+handle+'.json')
                        if path:
                            write_export(path, export(store, handle))
                            message = 'Evidence exported to '+path
                    tool_read.next_read=0
                except (ValueError, OSError, sqlite3.Error) as error:
                    message = 'Plan action failed: '+str(error)
            elif key == ord('d'):
                view = 6
                if disk_future is None:
                    try:
                        disk_future = disk_reader.start(context_row['cwd'] if context_row else cwd)
                    except (RuntimeError,ValueError,OSError) as error:
                        message='Disk scan failed to start: '+str(error)
            elif key == ord('t') and height >= 15 and width >= 65:
                from .profiles import tasks as saved_tasks, repository
                try:
                    location = prompt(screen, 'Task repository', context_row['cwd'] if context_row else str(cwd))
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
                    attempt=submissions.start(['task','run',task,'--cwd',location], 'task '+task,time.monotonic())
                    submission_rows=submissions.statuses
                    message=submission_notice(submissions.get(attempt))
                except (ValueError, OSError, RuntimeError, sqlite3.Error) as error:
                    message = 'Task failed: ' + str(error)
            elif key == ord('i'):
                view = 0 if view == 3 else 3
                details_view.home()
            elif key == ord('a') and height >= 15 and width >= 65:
                try:
                    from .announce import create, preview, select, spawn_worker
                    recipients = preview()
                    panes = prompt(screen, 'Select exact idle pane IDs, separated by spaces (blank cancels)', context=recipient_context(recipients))
                    if not panes:
                        continue
                    recipients = select(recipients, panes.split())
                    answer = prompt(screen, 'Send announcement to {} selected idle agents? type yes'.format(len(recipients)), context=recipient_context(recipients))
                    if answer == 'yes':
                        announcement = create(store.writable(), recipients)
                        spawn_worker(announcement)
                        message = 'Announcement {} queued to {} agents'.format(announcement, len(recipients))
                        view = 2
                except (ValueError,OSError,sqlite3.Error,subprocess.TimeoutExpired) as error:
                    message = 'Announcement failed: ' + str(error)
            elif key == ord('p'):
                try:
                    writer=store.writable()
                    result=writer.update_config({'paused':not writer.config()['paused']})
                    message='Queue paused; running jobs continue.' if result['paused'] else 'Queue resumed.'
                except (ValueError,OSError,sqlite3.Error) as error:
                    message='Pause action failed: '+str(error)
            elif key == ord('b') and height >= 15 and width >= 65:
                changes = {}
                try:
                    action_config=store.writable().config()
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
                        value = prompt(screen, label + ' (Esc aborts)', str(action_config[field]))
                        if value is None:
                            changes = {}
                            break
                        changes[field] = kind(value)
                    if changes:
                        store.writable().update_config(changes)
                        message = 'Budgets saved. Running jobs keep their original reservations.'
                except (ValueError, OSError, sqlite3.Error) as error:
                    message = 'Budget update rejected: ' + str(error)
            elif key == ord('c') and context_row and view != 7:
                row = context_row
                if row['state'] not in TERMINAL:
                    answer = prompt(screen, 'Cancel {}? type yes'.format(row['id']), context=job_context(row))
                    if answer == 'yes':
                        try:
                            cancel_job(store.writable(), row['id'])
                            message = 'Cancellation requested: ' + row['id']
                        except (ValueError,OSError,sqlite3.Error) as error:
                            message='Cancellation outcome unconfirmed for '+row['id']+': '+str(error)+'; inspect before another request.'
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
                try:
                    image=store.writable().config()['docker_image']
                except (ValueError,OSError,sqlite3.Error) as error:
                    message='Job defaults unavailable: '+str(error)
                    continue
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
                    arguments=['run','--cwd',str(location),'--lane',lane,'--backend',backend,'--notify']
                    if image:
                        arguments+=['--image',image]
                    attempt=submissions.start(arguments+['--']+command,shlex.join(command),time.monotonic())
                    submission_rows=submissions.statuses
                    message=submission_notice(submissions.get(attempt))
                    selected = 0
                except (ValueError, OSError, RuntimeError, sqlite3.Error) as error:
                    message = str(error)
    finally:
        try:
            disk_reader.close()
        finally:
            store.close()


def launch(cwd):
    if not sys.stdin.isatty() or not sys.stdout.isatty() or os.environ.get('TERM') in (None, '', 'dumb'):
        raise ValueError('Open a Ghostty/Herdr terminal to use the TUI; use status --json from an agent tool')
    curses.wrapper(main, Path(cwd).expanduser().resolve())
