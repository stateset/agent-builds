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


def preflight_options(command, batch=False):
    command.add_argument('--cwd',default=os.getcwd())
    command.add_argument('--jobs',type=int)
    command.add_argument('--memory-gb',type=float)
    command.add_argument('--timeout',type=float)
    command.add_argument('--resource-key')
    command.add_argument('--lock',dest='locks',action='append',default=[])
    command.add_argument('--min-free-gb',type=float,default=0)
    command.add_argument('--scratch-gb',type=float,default=0)
    command.add_argument('--compare-jobs',type=int,action='append',default=[])
    command.add_argument('--compare-memory-gb',type=float,action='append',default=[])
    command.add_argument('--json',action='store_true')
    if not batch:
        command.add_argument('--lane',choices=['quick','heavy'],default='heavy')
        command.add_argument('--backend',choices=['local','docker'],default='local')
        command.add_argument('--image')
        command.add_argument('command',nargs=argparse.REMAINDER)


def preflight_command(args):
    from .preflight import report, lines
    fields=('cwd','lane','backend','jobs','memory_gb','timeout','image','resource_key','locks',
            'min_free_gb','scratch_gb','compare_jobs','compare_memory_gb')
    values={key:getattr(args,key) for key in fields if getattr(args,key,None) is not None}
    if args.action=='batch':
        with Path(args.manifest).expanduser().open('rb') as source:
            raw=source.read(32769)
        if len(raw)>32768:
            raise ValueError('Batch manifest exceeds 32 KiB')
        values['manifest']=json.loads(raw)
    else:
        argv=list(args.command)
        if argv and argv[0]=='--':
            argv.pop(0)
        if argv:
            values['command']=argv
    store=Store(read_only=True)
    try:
        result=report(store,values)
    finally:
        store.close()
    print(json.dumps(result,indent=2) if args.json else '\n'.join(lines(result)))
    return 0


def parser():
    root = argparse.ArgumentParser(description='Buildr: shared build queue and TUI for Ghostty and Herdr')
    root.add_argument('--version', action='version', version=__version__)
    sub = root.add_subparsers(dest='action')
    tui = sub.add_parser('tui', help='Open the terminal dashboard (default)')
    tui.add_argument('--cwd', default=os.getcwd())
    workbench=sub.add_parser('workbench',help='Inspect bottlenecks, CPU/RAM comparisons and reviewed trials for one job')
    workbench.add_argument('job')
    workbench.add_argument('--json',action='store_true')
    preflight_options(sub.add_parser('preflight',help='Compare CPU/RAM requests before submission; read only, no reservation or execution'))
    run = sub.add_parser('run', help='Queue work, stream output and preserve its exit code')
    run.add_argument('--cwd', default=os.getcwd())
    run.add_argument('--lane', choices=['quick', 'heavy'], default='heavy')
    run.add_argument('--backend', choices=['local', 'docker'], default='local')
    run.add_argument('--jobs', type=int)
    run.add_argument('--memory-gb', type=float)
    run.add_argument('--resource-key', help='Serialize jobs sharing this build output directory')
    run.add_argument('--lock', dest='locks', action='append', default=[], help='Additional exact resource key; repeat to protect multiple outputs')
    run.add_argument('--image', default='')
    run.add_argument('--timeout', type=float)
    run.add_argument('--notify', action='store_true', help='Notify the current Herdr session on completion')
    run.add_argument('--track-source', action='store_true', help='Record bounded source fingerprints at submission/start/finish')
    run.add_argument('--input-file', dest='input_files', action='append', default=[], help='Fingerprint a literal file (repeat, max 16); enables source tracking')
    run.add_argument('--min-free-gb', type=float, default=0, help='Minimum free disk space required before starting')
    run.add_argument('--scratch-gb', type=float, default=0, help='Conservative additional disk reservation while running')
    run.add_argument('--events-json', action='store_true', help='Emit JSON lifecycle events on stderr; command output stays on stdout')
    run.add_argument('--detach', action='store_true', help='Return a durable job handle and keep the runner alive after this terminal exits')
    run.add_argument('--receipt-file', help=argparse.SUPPRESS)
    run.add_argument('command', nargs=argparse.REMAINDER)
    batches = sub.add_parser('batch', help='Run related short checks in one local reservation')
    batch_sub = batches.add_subparsers(dest='batch_action', required=True)
    batch_preview=batch_sub.add_parser('preflight',help='Inspect a batch and compare CPU/RAM reservations without submitting it')
    batch_preview.add_argument('manifest')
    preflight_options(batch_preview,batch=True)
    batch_run = batch_sub.add_parser('run')
    batch_run.add_argument('manifest')
    batch_run.add_argument('--cwd', default=os.getcwd())
    batch_run.add_argument('--jobs', type=int, default=1)
    batch_run.add_argument('--memory-gb', type=float, default=1)
    batch_run.add_argument('--timeout', type=float, default=60)
    batch_run.add_argument('--resource-key')
    batch_run.add_argument('--lock', dest='locks', action='append', default=[])
    batch_run.add_argument('--input-file', dest='input_files', action='append', default=[])
    batch_run.add_argument('--detach', action='store_true')
    batch_run.add_argument('--events-json', action='store_true')
    batch_show = batch_sub.add_parser('show')
    batch_show.add_argument('job')
    batch_worker = sub.add_parser('batch-worker', help=argparse.SUPPRESS)
    batch_worker.add_argument('definition')
    latency = sub.add_parser('latency', help='Read queue latency, slow checks and measured reservation trials')
    latency.add_argument('--hours', type=float, default=24)
    latency.add_argument('--target-seconds', type=float, default=30)
    latency.add_argument('--repo')
    latency.add_argument('--json', action='store_true')
    diagnose = sub.add_parser('diagnose', help='Explain current blockers, exact headroom deficits and policy draining without changing state')
    diagnose.add_argument('job', nargs='?')
    diagnose.add_argument('--repo')
    diagnose.add_argument('--limit', type=int, default=10)
    diagnose.add_argument('--json', action='store_true')
    status = sub.add_parser('status', help='Show jobs and machine pressure')
    status.add_argument('--json', action='store_true')
    status.add_argument('--summary', action='store_true', help='Queue health, machine resources, budgets and job counts')
    status.add_argument('--active', action='store_true', help='Only queued/running/cancelling jobs')
    status.add_argument('--repo', help='Filter by absolute repository or worktree path')
    status.add_argument('--limit', type=int, default=20, help='Maximum jobs returned (default 20)')
    status.add_argument('--details', action='store_true', help='Include full job evidence instead of concise summaries')
    status.add_argument('--history', action='store_true', help='List completed jobs instead of active jobs')
    status.add_argument('--before', help='Continue completed history before this job handle (stable creation-order cursor)')
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
    announce.add_argument('--send', action='store_true', help='Send only to explicitly selected idle recipients')
    announce.add_argument('--recipient', action='append', default=[], help='Exact idle pane ID to authorize; repeat for each recipient')
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
    proposal=task_sub.add_parser('propose',help='Draft a measured resource trial from an explicitly selected successful job')
    proposal.add_argument('job')
    proposal.add_argument('--kind',choices=['memory_trial','low_average_cpu','tiny_check_trial','long_quick_command'],default='memory_trial')
    proposal.add_argument('--name',required=True)
    proposal.add_argument('--output',required=True)
    trial=task_sub.add_parser('trial',help='Submit a reviewed proposal as three to five strict queued verification stages')
    trial.add_argument('proposal')
    trial.add_argument('--repeats',type=int,default=3)
    for action in ('review','promote'):
        command=task_sub.add_parser(action)
        command.add_argument('proposal')
        command.add_argument('--plan',required=True)
    task_add = task_sub.add_parser('add', usage='%(prog)s NAME [options] -- COMMAND [ARG ...]')
    task_add.add_argument('name')
    task_add.add_argument('--cwd', default=os.getcwd())
    task_add.add_argument('--lane', choices=['quick', 'heavy'], default='quick')
    task_add.add_argument('--backend', choices=['local', 'docker'], default='local')
    task_add.add_argument('--jobs', type=int)
    task_add.add_argument('--input-file', dest='input_files', action='append', default=[])
    task_add.add_argument('--resource-key', help='Explicit output-directory lock; omit to keep repository serialization')
    task_add.add_argument('--lock', dest='locks', action='append', default=[], help='Additional exact resource key; repeat to protect multiple outputs')
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
    plan_review=plan_sub.add_parser('review',help='Review explicit stages for long quick-lane work; never execute commands')
    plan_review.add_argument('manifest')
    plan_review.add_argument('--cwd',default=os.getcwd())
    plan_review.add_argument('--output')
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
    experiment = sub.add_parser('experiment', help='Bounded paired local experiments; run the whole driver through the shared queue')
    experiment_sub=experiment.add_subparsers(dest='experiment_action',required=True)
    study=experiment_sub.add_parser('workflow',help='Run explicit representative plan pairs; stages use the shared queue')
    study.add_argument('baseline')
    study.add_argument('candidate')
    study.add_argument('--cwd',default=os.getcwd())
    study.add_argument('--output',required=True)
    study.add_argument('--pairs',type=int,default=2)
    study.add_argument('--timeout',type=float,default=3600)
    compare=experiment_sub.add_parser('compare',help='Read-only comparison of two existing verification plan handles')
    compare.add_argument('baseline')
    compare.add_argument('candidate')
    compare.add_argument('--output')
    experiment_run=experiment_sub.add_parser('run')
    experiment_run.add_argument('--output',required=True)
    experiment_run.add_argument('--pairs',type=int,default=3)
    experiment_run.add_argument('--agents',type=int,default=4)
    experiment_run.add_argument('--cpus',type=int,default=2)
    experiment_run.add_argument('--scenario',action='append',choices=['shared_output','resource_contention','python_verification'])
    experiment_run.add_argument('--include-batches',action='store_true',help='Compare serial/parallel batches of the independent Python checks under the same outer reservation')
    from .scale import add_parser as add_benchmark_parser
    add_benchmark_parser(sub)
    optimize = sub.add_parser('optimize', help='Optional local AI ranking of evidence-backed trials; never applies changes')
    optimize_sub = optimize.add_subparsers(dest='optimize_action', required=True)
    for action in ('prepare', 'ask'):
        command = optimize_sub.add_parser(action)
        command.add_argument('--repo')
        command.add_argument('--hours', type=float, default=24)
        command.add_argument('--limit', type=int, default=200)
        command.add_argument('--policy', default='current')
        command.add_argument('--output', required=True, help='New private report path; never overwritten')
        if action == 'ask':
            command.add_argument('--model', required=True, help='Already installed local Ollama model; no cloud models/downloads')
            command.add_argument('--threads', type=int, default=1)
    mcp = sub.add_parser('mcp', help='MCP stdio interface to the same queue for coding agents')
    mcp_sub = mcp.add_subparsers(dest='mcp_action', required=True)
    mcp_serve = mcp_sub.add_parser('serve', help='Read newline-delimited MCP on stdin; protocol messages only on stdout')
    mcp_serve.add_argument('--read-only', action='store_true', help='Expose inspection tools only')
    mcp_serve.add_argument('--allow-root', action='append', help='Allow new command working directories only under this absolute root; repeat as needed. Not an OS sandbox.')
    watches = sub.add_parser('watch', help='Observe external completion with short queued read-only probes and no idle reservations')
    watch_sub = watches.add_subparsers(dest='watch_action', required=True)
    watch_start = watch_sub.add_parser('start')
    watch_start.add_argument('--request-key', required=True)
    watch_start.add_argument('--cwd', default=os.getcwd())
    watch_start.add_argument('--external-ref', required=True)
    watch_start.add_argument('--read-only-probe', action='store_true', help='Declare that repeating this foreground status probe has no external side effects')
    watch_start.add_argument('--name', default='external completion')
    watch_start.add_argument('--interval', type=float, default=15)
    watch_start.add_argument('--timeout', type=float, default=3600)
    watch_start.add_argument('--probe-timeout', type=float, default=30)
    watch_start.add_argument('--max-probes', type=int, default=120)
    watch_start.add_argument('--jobs', type=int, default=1)
    watch_start.add_argument('--memory-gb', type=float, default=.5)
    watch_start.add_argument('--resource-key')
    watch_start.add_argument('--lock', dest='locks', action='append', default=[], help='Additional exact resource key; repeat to protect multiple outputs')
    watch_start.add_argument('--no-track-source', action='store_true')
    watch_start.add_argument('command', nargs=argparse.REMAINDER)
    watch_list = watch_sub.add_parser('list')
    watch_list.add_argument('--active', action='store_true')
    watch_list.add_argument('--limit', type=int, default=20)
    watch_list.add_argument('--json', action='store_true')
    for action in ('show', 'wait', 'cancel', 'resume', 'export'):
        command = watch_sub.add_parser(action)
        command.add_argument('watch')
        command.add_argument('--json', action='store_true')
        if action=='wait':
            command.add_argument('--timeout', type=float)
        if action=='export':
            command.add_argument('--output', required=True)
    watch_worker = sub.add_parser('watch-worker', help=argparse.SUPPRESS)
    watch_worker.add_argument('watch')
    watch_probe = sub.add_parser('watch-probe-worker', help=argparse.SUPPRESS)
    watch_probe.add_argument('job')
    watch_probe.add_argument('token')
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
        if args.action=='workbench':
            from .workbench import report,lines
            reader=Store(read_only=True)
            try:
                result=report(reader,args.job)
            finally:
                reader.close()
            print(json.dumps(result,indent=2) if args.json else '\n'.join(lines(result)))
            return 0
        if args.action=='preflight' or (args.action=='batch' and args.batch_action=='preflight'):
            return preflight_command(args)
        if args.action == 'batch-worker':
            from .batch import run
            return run(args.definition)
        if args.action == 'batch':
            from . import batch
            if args.batch_action=='show':
                reader=Store(read_only=True)
                try:
                    print(json.dumps(reader.get(args.job)['batch'],indent=2))
                finally:
                    reader.close()
                return 0
            if args.timeout>300:
                raise ValueError('Batch timeout must be at most 300 seconds')
            with Path(args.manifest).expanduser().open('rb') as manifest:
                raw=manifest.read(32769)
            if len(raw)>32768:
                raise ValueError('Batch manifest exceeds 32 KiB')
            definition=batch.validate(json.loads(raw))
            batch.worker_slots(definition, args.jobs)
            run_args=parser().parse_args(['run','--','placeholder'])
            for key in ('cwd','jobs','memory_gb','timeout','resource_key','locks','input_files','detach','events_json'):
                setattr(run_args,key,getattr(args,key))
            run_args.lane='quick'
            run_args.track_source=True
            run_args.command=batch.command(definition)
            return execute(run_args)
        if args.action == 'watch-worker':
            from .external import control
            control(args.watch)
            return 0
        if args.action == 'watch-probe-worker':
            return execute(parser().parse_args(['run', '--', 'true']), external_probe=(args.job, args.token))
        if args.action == 'watch':
            from . import external
            if args.watch_action=='start':
                from .submissions import submit
                command = args.command[1:] if args.command[:1]==['--'] else args.command
                spec = {key:getattr(args,key) for key in ('request_key','cwd','external_ref','read_only_probe','name','interval',
                    'timeout','probe_timeout','max_probes','jobs','memory_gb','resource_key','locks') if getattr(args,key) is not None and (key!='locks' or args.locks)}
                result = submit('buildr_watch_submit', dict(spec, command=command, track_source=not args.no_track_source))
                print(json.dumps(result, indent=2))
                return 2 if result['submission_state']=='uncertain' else 0
            reader = Store(read_only=args.watch_action not in ('cancel','resume'))
            try:
                if args.watch_action=='list':
                    with reader.read_snapshot():
                        result = external.listing(reader, args.active, args.limit)
                    print(json.dumps(result,indent=2) if args.json else '\n\n'.join('\n'.join(external.lines(row)) for row in result) or 'No external watches')
                    return 0
                if args.watch_action=='cancel':
                    external.cancel(reader,args.watch)
                elif args.watch_action=='resume':
                    external.spawn(reader,args.watch)
                if args.watch_action=='wait':
                    result = external.wait(reader,args.watch,args.timeout)
                    print(json.dumps(result,indent=2) if args.json else '\n'.join(external.lines(result['watch'])))
                    return result['exit_code']
                with reader.read_snapshot():
                    result = external.get(reader,args.watch,limit=1000 if args.watch_action=='export' else 20)
                if args.watch_action=='export':
                    from .plans import write_export
                    write_export(args.output,result)
                    print(args.output)
                else:
                    print(json.dumps(result,indent=2) if args.json else '\n'.join(external.lines(result)))
                return 0
            finally:
                reader.close()
        if args.action == 'mcp':
            from .execution_policy import configure
            configure(args.allow_root)
            from .mcp import serve
            return serve(args.read_only)
        if args.action in (None, 'tui'):
            from .tui import launch
            launch(getattr(args, 'cwd', os.getcwd()))
            return 0
        if args.action == 'run':
            return execute(args)
        if args.action == 'experiment':
            if args.experiment_action in ('workflow','compare'):
                from . import workflow_study
                study_store=Store(read_only=args.experiment_action=='compare')
                try:
                    if args.experiment_action=='workflow':
                        result=workflow_study.run(study_store,args.baseline,args.candidate,args.cwd,args.output,args.pairs,args.timeout)
                        print(json.dumps(dict(output=args.output,pairs=result['pairs'],comparisons=result['comparisons']),indent=2))
                    else:
                        result=workflow_study.compare(study_store,args.baseline,args.candidate)
                        if args.output:
                            from .plans import write_export
                            write_export(args.output,result)
                        print(json.dumps(result,indent=2))
                finally:
                    study_store.close()
                return 0
            from .experiments import run as experiment_run
            result=experiment_run(args.output,args.pairs,args.agents,args.cpus,args.scenario,args.include_batches)
            print(json.dumps(dict(output=args.output,comparisons=result['comparisons'],note=result['note']),indent=2))
            return 0
        if args.action == 'benchmark':
            from .scale import run as benchmark_run
            benchmark_run(args)
            return 0
        if args.action == 'optimize':
            from . import optimize
            reader = Store(read_only=True)
            try:
                pack = optimize.prepare(reader, args.repo, args.hours, args.limit, args.policy)
            finally:
                reader.close()
            if args.optimize_action == 'prepare':
                from .plans import write_export
                write_export(args.output, pack)
                result = dict(status='prepared', candidates=len(pack['payload']['candidates']))
            else:
                report = optimize.ask(pack, args.model, args.output, args.threads)
                result = {key: report[key] for key in ('status', 'model_called', 'changes_applied')}
                if 'response' in report:
                    result['response'] = report['response']
            print(json.dumps(dict(result, output=args.output, note=optimize.NOTE), indent=2))
            return 2 if result['status'] == 'model_rejected' else 0
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
        inspection = (args.action in ('diagnose', 'latency', 'status', 'logs', 'show', 'wait', 'announcements', 'processes', 'doctor', 'stats', 'disk', 'advise', 'simulate')
            or (args.action == 'announce' and not args.send)
            or (args.action == 'config' and not args.init and not args.set)
            or (args.action == 'clean' and not args.apply)
            or (args.action == 'task' and args.task_action in ('list','propose','review'))
            or (args.action == 'worker' and args.worker_action != 'add')
            or (args.action == 'group' and args.group_action != 'create')
            or (args.action == 'plan' and args.plan_action in ('list', 'show', 'wait', 'logs', 'validate', 'review', 'export', 'draft'))
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
                if args.plan_action=='review':
                    from .workflow import report
                    with Path(args.manifest).expanduser().open('rb') as source:
                        raw=source.read(256*1024+1)
                    if len(raw)>256*1024:
                        raise ValueError('Plan review input exceeds 256 KiB')
                    result=report(store,json.loads(raw),args.cwd)
                    if args.output:
                        plans.write_export(args.output,result['manifest'])
                        result['output_path']=str(Path(args.output).expanduser().absolute())
                    print(json.dumps(result,indent=2))
                    return 0
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
            if args.action == 'diagnose':
                from .diagnostics import report, lines
                result = report(store, args.job, args.repo, args.limit)
                print(json.dumps(result, indent=2) if args.json else '\n'.join(lines(result)))
            elif args.action == 'latency':
                from .latency import report
                print(json.dumps(report(store,args.hours,args.target_seconds,args.repo),indent=2))
            elif args.action == 'config':
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
                from .profiles import load, repository, save_task, task_args, tasks
                if args.task_action in ('propose','trial','review','promote'):
                    from . import task_review
                    if args.task_action=='propose':
                        result=task_review.propose(store,args.job,args.kind,args.name)
                        target=Path(args.output).expanduser().absolute()
                        if Path(result['source']['repository']) in target.parents:
                            raise ValueError('Save proposals outside the source checkout, for example under /tmp')
                        from .plans import write_export
                        write_export(target,result)
                        print(json.dumps(dict(proposal_path=str(target),proposal=result),indent=2))
                    elif args.task_action=='trial':
                        if os.environ.get('AGENT_BUILDS_JOB_ID'):
                            raise ValueError('Profile trial is a controller; do not run it inside a queued job')
                        task_review.trial(store,args.proposal,args.repeats)
                    else:
                        result=getattr(task_review,args.task_action)(store,args.proposal,args.plan)
                        print(json.dumps(result,indent=2))
                    return 0
                if args.task_action == 'add':
                    print(json.dumps(save_task(store, args), indent=2))
                elif args.task_action == 'list':
                    prefix = repository(args.cwd) + ':'
                    print(json.dumps([v for k, v in tasks(store).items() if k.startswith(prefix)], indent=2))
                else:
                    values=task_args(store,args)
                    if getattr(values,'review',None):
                        from .task_review import run_saved
                        return run_saved(store,values)
                    return execute(values)
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
                from .external import counts as watch_counts
                from .diagnostics import brief, history, policy_summary
                from .queue_health import report as queue_health, lines as queue_health_lines
                if not 1 <= args.limit <= 2000:
                    raise ValueError('--limit must be between 1 and 2000')
                if args.active and args.history:
                    raise ValueError('--active and --history cannot be combined')
                if args.before and not args.history:
                    raise ValueError('--before requires --history')
                with store.read_snapshot():
                    result = dict(initialized=store.db is not None, state_path=str(store.root),
                                  machine=snapshot(os.getcwd()), config=store.config(),
                                  counts=store.counts(args.active, args.repo),
                                  protection=policy_summary(store),
                                  external_watch_counts=watch_counts(store),
                                  count_scope=dict(active_only=args.active,
                                      repository=str(Path(args.repo).expanduser().resolve()) if args.repo else None,
                                      limited_by_result_limit=False))
                    result['queue_health'] = queue_health(store, args.repo, result['config'])
                    if not args.summary:
                        jobs = history(store, args.limit+1, args.repo, args.before) if args.history else store.rows(args.limit+1, True, args.repo)
                        result['jobs'] = [row if args.details else brief(row) for row in jobs[:args.limit]]
                        result['job_scope'] = 'completed' if args.history else 'active'
                        result['jobs_truncated'] = len(jobs)>args.limit
                        result['next_cursor'] = jobs[args.limit-1]['id'] if args.history and len(jobs)>args.limit else None
                if args.json:
                    print(json.dumps(result, indent=2))
                elif args.summary:
                    print('Machine: ' + json.dumps(result['machine']))
                    print('Jobs: ' + json.dumps(result['counts']))
                    print('External watches: '+json.dumps(result['external_watch_counts']))
                    print('\n'.join(queue_health_lines(result['queue_health'])))
                else:
                    print('\n'.join(queue_health_lines(result['queue_health'])))
                    for row in result['jobs']:
                        print('{} {:11} {:7} {} | {} {}'.format(row['id'], row['state'], row['backend'],
                            Path(row['cwd']).name, row['command_display'], row['reason'] or ''))
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
                from .announce import POLICY, create, preview, select, spawn_worker
                message = Path(args.message_file).read_text(encoding='utf-8') if args.message_file else POLICY
                if not message.strip() or len(message.encode('utf-8')) > 16384:
                    raise ValueError('Announcement must contain 1 to 16384 bytes of text')
                recipients = preview(args.exclude_pane)
                if args.send:
                    recipients = select(recipients, args.recipient)
                    announcement = create(store, recipients, message=message)
                    spawn_worker(announcement)
                    print(json.dumps(dict(announcement=announcement, recipients=len(recipients),
                                          state='queued; check announcements for delivery outcomes')))
                else:
                    for agent in recipients:
                        print('{} {:8} {}'.format(agent['pane_id'], agent.get('agent_status', ''), agent.get('cwd', '')))
                    print('\nMessage:\n' + message)
                    print('\nPreview only. Select each idle recipient: announce --send --recipient PANE (repeat as needed).')
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
        print('buildr: ' + message, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main())
