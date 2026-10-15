import argparse
import errno
import json
import os
from pathlib import Path
import shlex
import sys
import sqlite3
import subprocess
import time

from . import __version__
from .metrics import ProcessSampler, snapshot
from .runner import cancel_job, execute
from .store import DEFAULTS, Store


def parser():
    root = argparse.ArgumentParser(description='Shared build queue and TUI for Ghostty and Herdr')
    root.add_argument('--version', action='version', version=__version__)
    sub = root.add_subparsers(dest='action')
    tui = sub.add_parser('tui', help='Open the terminal dashboard (default)')
    tui.add_argument('--cwd', default=os.getcwd())
    run = sub.add_parser('run', help='Queue work, stream output and preserve its exit code')
    run.add_argument('--cwd', default=os.getcwd())
    run.add_argument('--lane', choices=['quick', 'heavy'], default='heavy')
    run.add_argument('--backend', choices=['local', 'docker'], default='local')
    run.add_argument('--jobs', type=int)
    run.add_argument('--memory-gb', type=float)
    run.add_argument('--resource-key', help='Serialize jobs sharing this build output directory')
    run.add_argument('--image', default='')
    run.add_argument('--timeout', type=float)
    run.add_argument('--notify', action='store_true', help='Notify the current Herdr session on completion')
    run.add_argument('--track-source', action='store_true', help='Record bounded source fingerprints at submission/start/finish')
    run.add_argument('--min-free-gb', type=float, default=0, help='Minimum free disk space required before starting')
    run.add_argument('--scratch-gb', type=float, default=0, help='Conservative additional disk reservation while running')
    run.add_argument('--events-json', action='store_true', help='Emit JSON lifecycle events on stderr; command output stays on stdout')
    run.add_argument('--detach', action='store_true', help='Return a durable job handle and keep the runner alive after this terminal exits')
    run.add_argument('--receipt-file', help=argparse.SUPPRESS)
    run.add_argument('command', nargs=argparse.REMAINDER)
    status = sub.add_parser('status', help='Show jobs and machine pressure')
    status.add_argument('--json', action='store_true')
    status.add_argument('--summary', action='store_true', help='Only machine resources, budgets and job counts')
    status.add_argument('--active', action='store_true', help='Only queued/running/cancelling jobs')
    status.add_argument('--repo', help='Filter by absolute repository or worktree path')
    status.add_argument('--limit', type=int, default=200, help='Maximum detailed jobs returned (default 200)')
    logs = sub.add_parser('logs', help='Print the selected job log')
    logs.add_argument('job')
    logs.add_argument('--follow', '-f', action='store_true')
    show = sub.add_parser('show', help='Inspect one stable job handle without modifying shared state')
    show.add_argument('job')
    show.add_argument('--json', action='store_true')
    wait = sub.add_parser('wait', help='Reattach read-only and return the final command exit code')
    wait.add_argument('jobs', nargs='+')
    wait.add_argument('--json', action='store_true')
    wait.add_argument('--timeout', type=float)
    wait.add_argument('--tail', type=int, default=0, help='Include up to 200 final log lines per job')
    cancel = sub.add_parser('cancel', help='Cancel a queued or running job')
    cancel.add_argument('job')
    config = sub.add_parser('config', help='Print defaults or write an initial editable config')
    config.add_argument('--init', action='store_true')
    config.add_argument('--set', action='append', default=[], metavar='KEY=JSON', help='Update a validated budget, e.g. quick_reserve_slots=2')
    sub.add_parser('doctor', help='Check machine headroom and scheduling capabilities')
    sub.add_parser('pause', help='Pause new starts; running jobs continue')
    sub.add_parser('resume', help='Resume new starts')
    processes = sub.add_parser('processes', help='Show resource use of visible processes owned by you')
    processes.add_argument('--json', action='store_true')
    announce = sub.add_parser('announce', help='Preview or send queue instructions to Herdr agents')
    announce.add_argument('--send', action='store_true', help='Send the previewed policy to the current agent snapshot')
    announce.add_argument('--exclude-pane', default=None)
    announce.add_argument('--message-file', help='Use a UTF-8 message file instead of the queue policy')
    announcements = sub.add_parser('announcements', help='Inspect announcement delivery outcomes')
    announcements.add_argument('--json', action='store_true')
    stats = sub.add_parser('stats', help='Summarize wait times and measured local job resources')
    stats.add_argument('--repo')
    stats.add_argument('--json', action='store_true')
    advise = sub.add_parser('advise', help='Read-only queue findings and resource trials supported by job history')
    advise.add_argument('--repo')
    advise.add_argument('--hours', type=float, default=24)
    advise.add_argument('--limit', type=int, default=200)
    advise.add_argument('--json', action='store_true')
    advise.add_argument('--policy', help='Filter by recorded scheduler policy; current selects the loaded policy')
    simulate = sub.add_parser('simulate', help='Compare reservation policies without executing commands or changing state')
    simulate.add_argument('--repo')
    simulate.add_argument('--hours', type=float, default=24)
    simulate.add_argument('--limit', type=int, default=100)
    simulate.add_argument('--fixture', action='store_true', help='Use the documented spare-capacity benchmark')
    simulate.add_argument('--json', action='store_true')
    disk = sub.add_parser('disk', help='Inventory known build caches without deleting them')
    disk.add_argument('--repo')
    disk.add_argument('--path', action='append', default=[], help='Also inventory an explicitly selected cache directory')
    disk.add_argument('--json', action='store_true')
    clean = sub.add_parser('clean', help='Preview retention of old completed-job logs')
    clean.add_argument('--older-than-days', type=float, default=7)
    clean.add_argument('--apply', action='store_true', help='Delete the previewed eligible queue logs')
    clean.add_argument('--json', action='store_true')
    tasks = sub.add_parser('task', help='Manage explicit per-repository build shortcuts')
    task_sub = tasks.add_subparsers(dest='task_action', required=True)
    task_add = task_sub.add_parser('add', usage='%(prog)s NAME [options] -- COMMAND [ARG ...]')
    task_add.add_argument('name')
    task_add.add_argument('--cwd', default=os.getcwd())
    task_add.add_argument('--lane', choices=['quick', 'heavy'], default='quick')
    task_add.add_argument('--backend', choices=['local', 'docker'], default='local')
    task_add.add_argument('--jobs', type=int)
    task_add.add_argument('--resource-key', help='Explicit output-directory lock; omit to keep repository serialization')
    task_add.add_argument('--memory-gb', type=float)
    task_add.add_argument('--timeout', type=float)
    task_add.add_argument('--image', default='')
    task_add.add_argument('--min-free-gb', type=float, default=0)
    task_add.add_argument('--scratch-gb', type=float, default=0)
    task_list = task_sub.add_parser('list')
    task_list.add_argument('--cwd', default=os.getcwd())
    task_run = task_sub.add_parser('run')
    task_run.add_argument('name')
    task_run.add_argument('--cwd', default=os.getcwd())
    task_run.add_argument('--detach', action='store_true')
    task_run.add_argument('--events-json', action='store_true')
    workers = sub.add_parser('worker', help='Explicit dedicated build worker profiles; never connects automatically')
    worker_sub = workers.add_subparsers(dest='worker_action', required=True)
    worker_sub.add_parser('list')
    worker_add = worker_sub.add_parser('add')
    worker_add.add_argument('name')
    worker_add.add_argument('--host', required=True)
    worker_add.add_argument('--state-dir', required=True)
    worker_add.add_argument('--dedicated-build-host', action='store_true')
    worker_check = worker_sub.add_parser('check')
    worker_check.add_argument('name')
    remote = sub.add_parser('remote', help='Explicit SSH submission and inspection on a dedicated build worker')
    remote_sub = remote.add_subparsers(dest='remote_action', required=True)
    remote_sub.add_parser('list')
    remote_run = remote_sub.add_parser('run', usage='%(prog)s WORKER --cwd PATH [options] -- COMMAND [ARG ...]')
    remote_run.add_argument('worker')
    remote_run.add_argument('--cwd', required=True)
    remote_run.add_argument('--lane', choices=['quick', 'heavy'], default='heavy')
    remote_run.add_argument('--backend', choices=['local', 'docker'], default='local')
    remote_run.add_argument('--jobs', type=int)
    remote_run.add_argument('--memory-gb', type=float)
    remote_run.add_argument('--timeout', type=float)
    remote_run.add_argument('--image', default='')
    remote_run.add_argument('--min-free-gb', type=float, default=0)
    remote_run.add_argument('--scratch-gb', type=float, default=0)
    remote_status = remote_sub.add_parser('status')
    remote_status.add_argument('worker')
    for action in ('show', 'logs', 'cancel', 'reconcile'):
        remote_action = remote_sub.add_parser(action)
        remote_action.add_argument('handle')
        if action == 'logs':
            remote_action.add_argument('--follow', '-f', action='store_true')
    plans = sub.add_parser('plan', help='Durable dependency verification with source and artifact evidence')
    plan_sub = plans.add_subparsers(dest='plan_action', required=True)
    for action in ('run', 'validate'):
        plan_action = plan_sub.add_parser(action)
        plan_action.add_argument('manifest')
        plan_action.add_argument('--cwd', default=os.getcwd())
        if action == 'run':
            plan_action.add_argument('--wait', action='store_true')
            plan_action.add_argument('--summary', action='store_true')
            plan_action.add_argument('--tail', type=int, default=0)
    plan_list = plan_sub.add_parser('list')
    plan_list.add_argument('--active', action='store_true')
    plan_list.add_argument('--json', action='store_true')
    plan_list.add_argument('--summary', action='store_true')
    plan_list.add_argument('--limit', type=int, default=50)
    draft = plan_sub.add_parser('draft', help='Draft a reviewable plan from existing jobs without executing it')
    draft.add_argument('jobs', nargs='+')
    draft.add_argument('--cwd', default=os.getcwd())
    draft.add_argument('--output')
    draft.add_argument('--capture-input', action='append', default=[], help='Fingerprint an explicitly named file now; not historical evidence')
    for action in ('show', 'wait', 'cancel', 'resume', 'export'):
        plan_action = plan_sub.add_parser(action)
        plan_action.add_argument('plan')
        plan_action.add_argument('--json', action='store_true')
        if action in ('show', 'wait'):
            plan_action.add_argument('--summary', action='store_true')
            plan_action.add_argument('--tail', type=int, default=0)
        if action == 'wait':
            plan_action.add_argument('--timeout', type=float)
        if action == 'export':
            plan_action.add_argument('--output', help='Write a private JSON evidence report to this path')
    plan_logs = plan_sub.add_parser('logs')
    plan_logs.add_argument('plan')
    plan_logs.add_argument('stage')
    plan_logs.add_argument('--follow', '-f', action='store_true')
    controller = sub.add_parser('plan-worker', help=argparse.SUPPRESS)
    controller.add_argument('plan')
    stage_worker = sub.add_parser('stage-worker', help=argparse.SUPPRESS)
    stage_worker.add_argument('job')
    stage_worker.add_argument('token')
    worker = sub.add_parser('announce-worker', help=argparse.SUPPRESS)
    worker.add_argument('announcement')
    groups = sub.add_parser('group', help='Observe existing job handles under one durable handle; never resubmit them')
    group_sub = groups.add_subparsers(dest='group_action', required=True)
    group_create = group_sub.add_parser('create')
    group_create.add_argument('jobs', nargs='+')
    group_create.add_argument('--name', default='verification group')
    group_list = group_sub.add_parser('list')
    group_list.add_argument('--limit', type=int, default=50)
    group_list.add_argument('--json', action='store_true')
    for action in ('show','wait','export'):
        command = group_sub.add_parser(action)
        command.add_argument('group')
        command.add_argument('--json', action='store_true')
        if action=='wait':
            command.add_argument('--timeout', type=float)
            command.add_argument('--tail', type=int, default=0)
        if action=='export':
            command.add_argument('--output')
    return root


def main():
    arguments = sys.argv[1:]
    if arguments[:2] in (['task', 'add'], ['remote', 'run']):
        boundary = arguments.index('--') if '--' in arguments else len(arguments)
        args = parser().parse_args(arguments[:boundary])
        args.command = arguments[boundary + 1:]
    else:
        args = parser().parse_args(arguments)
    try:
        if args.action in (None, 'tui'):
            from .tui import launch
            launch(getattr(args, 'cwd', os.getcwd()))
            return 0
        if args.action == 'run':
            return execute(args)
        if args.action == 'stage-worker':
            return execute(parser().parse_args(['run', '--', 'true']), prepared=(args.job, args.token))
        if args.action == 'plan-worker':
            from .plans import control
            control(args.plan)
            return 0
        if args.action == 'announce-worker':
            from .announce import work
            work(args.announcement)
            return 0
        inspection = (args.action in ('status', 'logs', 'show', 'wait', 'announcements', 'processes', 'doctor', 'stats', 'disk', 'advise', 'simulate')
            or (args.action == 'config' and not args.init and not args.set)
            or (args.action == 'clean' and not args.apply)
            or (args.action == 'task' and args.task_action == 'list')
            or (args.action == 'worker' and args.worker_action != 'add')
            or (args.action == 'group' and args.group_action != 'create')
            or (args.action == 'plan' and args.plan_action in ('list', 'show', 'wait', 'logs', 'validate', 'export', 'draft'))
            or (args.action == 'remote' and args.remote_action in ('list', 'show', 'logs', 'status')))
        store = Store(read_only=inspection)
        try:
            if args.action == 'group':
                from . import groups
                if args.group_action=='create':
                    handle=groups.create(store,args.jobs,args.name)
                    print(json.dumps(dict(group_id=handle,state='observing existing jobs; no commands submitted')))
                    return 0
                if args.group_action=='list':
                    with store.read_snapshot():
                        result=groups.listing(store,args.limit)
                    print(json.dumps(result,indent=2))
                    return 0
                if args.group_action=='wait':
                    result=groups.wait(store,args.group,args.timeout,args.tail)
                elif args.group_action=='export':
                    result=groups.export(store,args.group)
                    if args.output:
                        from .plans import write_export
                        write_export(args.output,result)
                        print(str(Path(args.output).expanduser().absolute()))
                        return 0
                else:
                    with store.read_snapshot():
                        result=groups.get(store,args.group)
                print(json.dumps(result,indent=2) if args.json or args.group_action=='export' else '\n'.join(groups.text(result)))
                return result['exit_code'] if args.group_action=='wait' else 0
            if args.action == 'plan':
                from . import plans
                from .plan_output import compact, text as summary_text, validate_tail
                if args.plan_action=='draft':
                    from .plan_draft import draft
                    result=draft(store,args.jobs,args.cwd,args.capture_input)
                    if args.output:
                        plans.write_export(args.output,result['manifest'])
                        result['output_path']=str(Path(args.output).expanduser().absolute())
                    print(json.dumps(result,indent=2))
                    return 0
                if getattr(args, 'summary', False) or getattr(args, 'tail', 0):
                    validate_tail(getattr(args, 'tail', 0))
                if getattr(args, 'tail', 0) and not getattr(args, 'summary', False):
                    raise ValueError('--tail requires --summary')
                if args.plan_action in ('run', 'validate'):
                    definition = plans.read_manifest(args.manifest, args.cwd, store.config())
                    if args.plan_action == 'validate':
                        print(json.dumps(definition, indent=2))
                        return 0
                    handle = plans.create(store, definition)
                    plans.spawn(store, handle)
                    print(json.dumps(dict(plan_id=handle, state='submitted', stages=len(definition['stages']))), flush=True)
                    if args.wait:
                        result = plans.wait(store, handle)
                        if args.summary:
                            result = compact(result, args.tail)
                        print(json.dumps(result, indent=2))
                        return result['exit_code']
                    return 0
                if args.plan_action == 'list':
                    if not 1 <= args.limit <= 200:
                        raise ValueError('Plan list limit must be between 1 and 200')
                    with store.read_snapshot():
                        result = plans.listing(store, args.active, args.limit)
                elif args.plan_action == 'show':
                    with store.read_snapshot():
                        result = plans.get(store, args.plan)
                elif args.plan_action == 'wait':
                    result = plans.wait(store, args.plan, args.timeout)
                elif args.plan_action == 'cancel':
                    plans.cancel(store, args.plan)
                    result = plans.get(store, args.plan)
                elif args.plan_action == 'resume':
                    plans.spawn(store, args.plan)
                    result = dict(plan_id=args.plan, state='controller submitted; existing stage jobs will not be retried')
                elif args.plan_action == 'export':
                    result = plans.export(store, args.plan)
                    if args.output:
                        plans.write_export(args.output, result)
                        print(str(Path(args.output).expanduser().absolute()))
                        return 0
                elif args.plan_action == 'logs':
                    plan = plans.get(store, args.plan)
                    stage = next((s for s in plan['stages'] if s['name'] == args.stage), None)
                    if not stage or not stage['job_id']:
                        raise ValueError('Stage has no submitted job: ' + args.stage)
                    args.job = stage['job_id']
                    args.action = 'logs'
                if args.action == 'plan':
                    if getattr(args, 'summary', False):
                        result = compact(result, getattr(args, 'tail', 0))
                    if args.json or args.plan_action in ('export', 'list'):
                        print(json.dumps(result, indent=2))
                    else:
                        if getattr(args, 'summary', False):
                            print('\n'.join(summary_text(result)))
                            return result['exit_code'] if args.plan_action == 'wait' else 0
                        current = result.get('plan', result)
                        if 'stages' in current:
                            print('{} {} | {}'.format(current['id'], current['state'], current['name']))
                            for stage in current['stages']:
                                print('{:18} {:11} {} | {}'.format(stage['name'], stage['state'], stage['job_id'] or '-', stage['reason']))
                        else:
                            print(json.dumps(result, indent=2))
                    return result['exit_code'] if args.plan_action == 'wait' else 0
            if args.action == 'config':
                if args.set:
                    changes = {}
                    for entry in args.set:
                        key, separator, value = entry.partition('=')
                        if not separator or key not in DEFAULTS:
                            raise ValueError('Use a known config key and a JSON value: KEY=JSON')
                        changes[key] = json.loads(value)
                    store.update_config(changes)
                if args.init and not store.config_path.exists():
                    with store.config_path.open('x') as handle:
                        json.dump(DEFAULTS, handle, indent=2)
                        handle.write('\n')
                    os.chmod(str(store.config_path), 0o600)
                print(str(store.config_path))
                print(json.dumps(store.config(), indent=2))
            elif args.action == 'wait':
                from .completion import wait
                result = wait(store, args.jobs, args.timeout, args.tail)
                if args.json:
                    print(json.dumps(result, indent=2))
                else:
                    for row in result['jobs']:
                        print('{} {} | exit {} | {}'.format(row['id'], row['state'], row['exit_code'], row['log_path']))
                        for line in row.get('log_tail', []):
                            print(line)
                    if result['timed_out']:
                        print('Wait timed out; jobs continue.')
                return result['exit_code']
            elif args.action == 'simulate':
                from .simulation import compare, fixture, history
                result = compare(*fixture()) if args.fixture else history(store, args.repo, args.hours, args.limit)
                if args.json:
                    print(json.dumps(result,indent=2))
                else:
                    for name in ('baseline','candidate'):
                        entry=result[name]
                        print('{} {} | {} completed | {:.1f}s modeled makespan'.format(name, entry['policy_version'], entry['completed'], entry['makespan_seconds']))
                        for lane in entry['lanes']:
                            print('  {}: p95 wait {}s'.format(lane['lane'], lane['wait_p95_seconds']))
                    print(result['note'])
            elif args.action == 'advise':
                from .advice import report, lines
                result = report(store, args.repo, args.hours, args.limit, args.policy)
                print(json.dumps(result, indent=2) if args.json else '\n'.join(lines(result)))
            elif args.action == 'stats':
                from .telemetry import statistics
                result = statistics(store, args.repo)
                if args.json:
                    print(json.dumps(result, indent=2))
                else:
                    for row in result['groups']:
                        print('{} jobs | p95 wait {:.1f}s | runtime {:.1f}s | sampled RSS {} GiB | {}'.format(
                            row['jobs'], row['wait_p95_seconds'], row['average_runtime_seconds'],
                            '{:.2f}'.format(row['sampled_peak_rss_gb']) if row['sampled_peak_rss_gb'] is not None else 'unmeasured',
                            row['command_display']))
                    from .waits import summary
                    print('Primary wait blockers: ' + summary(result['wait_breakdown']))
                    print(result['wait_breakdown']['note'])
                    print(result['note'])
            elif args.action in ('disk', 'clean'):
                from .disk import inventory, clean_logs
                result = inventory(store, args.repo, args.path) if args.action == 'disk' else clean_logs(store, args.older_than_days, args.apply)
                if args.json:
                    print(json.dumps(result, indent=2))
                else:
                    for row in result.get('paths', result.get('files', [])):
                        print('{} GiB | {}{}'.format('{:.3f}'.format(row['bytes']/1024**3) if row['bytes'] is not None else '?',
                              row['path'], ' | ' + row['error'] if row.get('error') else ''))
                    print(result['note'])
                    if args.action == 'clean':
                        print('{} {} files ({:.3f} GiB)'.format('Deleted' if args.apply else 'Preview:', len(result['files']), result['bytes']/1024**3))
                        if not args.apply:
                            print('Use --apply to delete eligible logs.')
            elif args.action == 'task':
                from .profiles import load, repository, save_task, task_args
                if args.task_action == 'add':
                    print(json.dumps(save_task(store, args), indent=2))
                elif args.task_action == 'list':
                    prefix = repository(args.cwd) + ':'
                    print(json.dumps([v for k, v in load(store).get('tasks', {}).items() if k.startswith(prefix)], indent=2))
                else:
                    return execute(task_args(store, args))
            elif args.action == 'worker':
                from .profiles import load, save_worker
                from .remote import rpc, worker_for
                if args.worker_action == 'add':
                    result = save_worker(store, args)
                elif args.worker_action == 'list':
                    result = load(store).get('workers', {})
                else:
                    result = rpc(worker_for(store, args.name), {'action': 'ping'})
                print(json.dumps(result, indent=2))
            elif args.action == 'remote':
                from .remote import dispatches, journal, reconcile, rpc, submit, worker_for
                if args.remote_action == 'run':
                    result = submit(store, args)
                elif args.remote_action == 'list':
                    result = dispatches(store)
                elif args.remote_action == 'reconcile':
                    result = reconcile(store, args.handle)
                elif args.remote_action == 'status':
                    result = rpc(worker_for(store, args.worker), {'action': 'status'})
                else:
                    record = journal(store, args.handle)
                    if not record.get('job_id'):
                        raise ValueError('Remote outcome unknown. Use remote reconcile ' + args.handle)
                    worker = record['worker_config']
                    if args.remote_action == 'logs':
                        import base64
                        offset = 0
                        while True:
                            response = rpc(worker, dict(action='logs', job=record['job_id'], offset=offset))
                            chunk = base64.b64decode(response['data'])
                            sys.stdout.buffer.write(chunk)
                            sys.stdout.buffer.flush()
                            offset = response['offset']
                            if not chunk and (not args.follow or response['state'] in ('succeeded', 'failed', 'cancelled', 'lost')):
                                break
                            if not chunk:
                                time.sleep(1)
                        return 0
                    action = 'get' if args.remote_action == 'show' else 'cancel'
                    result = rpc(worker, dict(action=action, job=record['job_id']))
                print(json.dumps(result, indent=2))
                if args.remote_action == 'run' and result['state'] == 'unknown':
                    return 1
            elif args.action == 'status':
                if not 1 <= args.limit <= 2000:
                    raise ValueError('--limit must be between 1 and 2000')
                with store.read_snapshot():
                    result = dict(initialized=store.db is not None, state_path=str(store.root),
                                  machine=snapshot(os.getcwd()), config=store.config(),
                                  counts=store.counts(args.active, args.repo),
                                  count_scope=dict(active_only=args.active,
                                      repository=str(Path(args.repo).expanduser().resolve()) if args.repo else None,
                                      limited_by_result_limit=False))
                    if not args.summary:
                        result['jobs'] = store.rows(args.limit, args.active, args.repo)
                if args.json:
                    print(json.dumps(result, indent=2))
                elif args.summary:
                    print('Machine: ' + json.dumps(result['machine']))
                    print('Jobs: ' + json.dumps(result['counts']))
                else:
                    for row in result['jobs']:
                        print('{} {:11} {:7} {} | {} {}'.format(row['id'], row['state'], row['backend'],
                            Path(row['cwd']).name, shlex.join(json.loads(row['command'])), row['reason'] or ''))
            elif args.action == 'logs':
                store.get(args.job)
                path = store.log_path(args.job)
                offset = 0
                while True:
                    if path.exists():
                        with path.open('rb') as handle:
                            handle.seek(offset)
                            chunk = handle.read()
                            offset = handle.tell()
                        sys.stdout.buffer.write(chunk)
                        sys.stdout.buffer.flush()
                    terminal = store.get(args.job)['state'] in ('succeeded', 'failed', 'cancelled', 'lost')
                    if not args.follow or (terminal and (not path.exists() or path.stat().st_size <= offset)):
                        break
                    time.sleep(.3)
            elif args.action == 'show':
                with store.read_snapshot():
                    row = store.get(args.job)
                if args.json:
                    print(json.dumps(row, indent=2))
                else:
                    print('{} {} | {}'.format(row['id'], row['state'], row['command_display']))
                    print('directory: {}\nlog: {}\nwaited: {:.1f}s | elapsed: {:.1f}s | exit: {}'.format(
                        row['cwd'], row['log_path'], row['wait_seconds'], row['elapsed_seconds'], row['exit_code']))
                    from .waits import summary
                    print('Wait breakdown: ' + summary(row['wait_breakdown']))
                    print(row['wait_breakdown']['note'])
                    if row['reason']:
                        print('waiting: ' + row['reason'])
                        print(json.dumps(row['wait_context'], indent=2))
            elif args.action == 'cancel':
                cancel_job(store, args.job)
                print('Cancellation requested: ' + args.job)
            elif args.action in ('pause', 'resume'):
                store.update_config({'paused': args.action == 'pause'})
                print('Queue ' + ('paused; running jobs continue' if args.action == 'pause' else 'resumed'))
            elif args.action == 'processes':
                sampler = ProcessSampler()
                sampler.sample()
                time.sleep(.3)
                rows = sampler.sample()[:30]
                if args.json:
                    print(json.dumps(rows, indent=2))
                else:
                    print('PID     CPU%   RSS MiB  PROCESS / WORKING DIRECTORY')
                    for row in rows:
                        print('{:<7} {:>5.1f} {:>9.0f}  {} {}'.format(row['pid'], row['cpu'] or 0, row['rss_mb'], row['name'], row['cwd']))
            elif args.action == 'announce':
                from .announce import POLICY, create, preview, spawn_worker
                message = Path(args.message_file).read_text(encoding='utf-8') if args.message_file else POLICY
                if not message.strip() or len(message.encode('utf-8')) > 16384:
                    raise ValueError('Announcement must contain 1 to 16384 bytes of text')
                recipients = preview(args.exclude_pane)
                if args.send:
                    announcement = create(store, recipients, message=message)
                    spawn_worker(announcement)
                    print(json.dumps(dict(announcement=announcement, recipients=len(recipients),
                                          state='queued; check announcements for delivery outcomes')))
                else:
                    for agent in recipients:
                        print('{} {:8} {}'.format(agent['pane_id'], agent.get('agent_status', ''), agent.get('cwd', '')))
                    print('\nMessage:\n' + message)
                    print('\nPreview only. Use announce --send to send to these agents.')
            elif args.action == 'announcements':
                from .announce import deliveries
                rows = deliveries(store)
                if args.json:
                    print(json.dumps(rows, indent=2))
                else:
                    for row in rows:
                        print('{} {} {:10} {} | {}'.format(row['announcement_id'], row['pane'], row['state'],
                            Path(row['repository']).name, row['detail']))
            elif args.action == 'doctor':
                m = snapshot(os.getcwd())
                c = store.config()
                print(json.dumps(m, indent=2))
                print('Managed budget: {} CPU slots; {} GiB reserved; {} concurrent jobs'.format(c['cpu_slots'], c['memory_gb'], c['max_running']))
                print('Local jobs: lower CPU/I/O priority; CPU and RAM reservations are scheduling estimates.')
                print('Docker jobs: --cpus and --memory apply hard container limits.')
                print('Only jobs submitted through this tool share the budget; existing builds continue independently.')
                if m['free_gb'] < c['min_free_gb']:
                    print('BLOCKED: disk free below {} GiB; new jobs wait. No files are deleted automatically.'.format(c['min_free_gb']))
                from .profiles import load
                print('Dedicated worker profiles: {} (explicit SSH commands only; no automatic offload)'.format(len(load(store).get('workers', {}))))
                print('Quick reserve: {} slots; aged-job priority after {}s'.format(c['quick_reserve_slots'], c['aging_seconds']))
                print('Herdr notifications: ' + ('available in this session' if os.environ.get('HERDR_ENV') == '1' else 'disabled outside Herdr'))
            return 0
        finally:
            store.close()
    except (ValueError, OSError, sqlite3.Error, subprocess.TimeoutExpired) as error:
        message = str(error)
        if isinstance(error, OSError) and error.errno in (errno.EACCES, errno.EROFS, errno.EPERM):
            message += '. Shared queue access was blocked by the sandbox. Request normal approval for the original command; status/show/logs use read-only access.'
        print('agent-builds: ' + message, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main())
