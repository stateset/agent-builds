import argparse
import errno
import json
import os
from pathlib import Path
import shlex
import sys
import sqlite3
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
    cancel = sub.add_parser('cancel', help='Cancel a queued or running job')
    cancel.add_argument('job')
    config = sub.add_parser('config', help='Print defaults or write an initial editable config')
    config.add_argument('--init', action='store_true')
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
    worker = sub.add_parser('announce-worker', help=argparse.SUPPRESS)
    worker.add_argument('announcement')
    return root


def main():
    args = parser().parse_args()
    try:
        if args.action in (None, 'tui'):
            from .tui import launch
            launch(getattr(args, 'cwd', os.getcwd()))
            return 0
        if args.action == 'run':
            return execute(args)
        if args.action == 'announce-worker':
            from .announce import work
            work(args.announcement)
            return 0
        inspection = args.action in ('status', 'logs', 'show', 'announcements', 'processes', 'doctor') or (args.action == 'config' and not args.init)
        store = Store(read_only=inspection)
        try:
            if args.action == 'config':
                if args.init and not store.config_path.exists():
                    with store.config_path.open('x') as handle:
                        json.dump(DEFAULTS, handle, indent=2)
                        handle.write('\n')
                    os.chmod(str(store.config_path), 0o600)
                print(str(store.config_path))
                print(json.dumps(store.config(), indent=2))
            elif args.action == 'status':
                if not 1 <= args.limit <= 2000:
                    raise ValueError('--limit must be between 1 and 2000')
                result = dict(initialized=store.db is not None, state_path=str(store.root),
                              machine=snapshot(os.getcwd()), config=store.config(),
                              counts=store.counts(args.active, args.repo))
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
                row = store.get(args.job)
                if args.json:
                    print(json.dumps(row, indent=2))
                else:
                    print('{} {} | {}'.format(row['id'], row['state'], row['command_display']))
                    print('directory: {}\nlog: {}\nwaited: {:.1f}s | elapsed: {:.1f}s | exit: {}'.format(
                        row['cwd'], row['log_path'], row['wait_seconds'], row['elapsed_seconds'], row['exit_code']))
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
                import time
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
                print('Hetzner: not configured. Use a separate build host, not production k3s.')
                print('Herdr notifications: ' + ('available in this session' if os.environ.get('HERDR_ENV') == '1' else 'disabled outside Herdr'))
            return 0
        finally:
            store.close()
    except (ValueError, OSError, sqlite3.Error) as error:
        message = str(error)
        if isinstance(error, OSError) and error.errno in (errno.EACCES, errno.EROFS, errno.EPERM):
            message += '. Shared queue access was blocked by the sandbox. Request normal approval for the original command; status/show/logs use read-only access.'
        print('agent-builds: ' + message, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main())
