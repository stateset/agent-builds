import json
import math
import os
from pathlib import Path
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

from .store import Store, TERMINAL, alive, identity
from .telemetry import JobSampler
from .source import capture, normalize_inputs


def resource_key(cwd):
    target = os.environ.get('CARGO_TARGET_DIR')
    if target:
        return str((Path(cwd) / target).resolve())
    try:
        result = subprocess.run(['git', '-C', str(cwd), 'rev-parse', '--show-toplevel'],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except OSError:
        return str(Path(cwd).resolve())
    return result.stdout.strip() if result.returncode == 0 else str(Path(cwd).resolve())


def command_for(row, config):
    command = json.loads(row['command'])
    if row['backend'] == 'docker':
        return ['docker', 'run', '--rm', '--init', '--name', 'agent-builds-' + row['id'],
                '--label', 'agent-builds.job=' + row['id'],
                '--cpus', str(row['slots']), '--memory', str(row['memory_gb']) + 'g',
                '--memory-swap', str(row['memory_gb']) + 'g',
                '--user', '{}:{}'.format(os.getuid(), os.getgid()),
                '--mount', 'type=bind,src={},dst=/workspace'.format(row['cwd']),
                '--workdir', '/workspace', '--env', 'HOME=/tmp',
                '--env', 'CARGO_BUILD_JOBS=' + str(row['slots']),
                '--env', 'RUST_TEST_THREADS=' + str(row['slots']),
                '--env', 'AGENT_BUILDS_JOB_ID=' + row['id'],
                row['image']] + command
    prefix = []
    if shutil.which('ionice'):
        prefix += ['ionice', '-c', '2', '-n', '7']
    if shutil.which('nice'):
        prefix += ['nice', '-n', str(config['nice'])]
    return prefix + command


def terminate(row, grace=3):
    if row['backend'] == 'docker':
        try:
            subprocess.run(['docker', 'stop', '--time', str(grace), 'agent-builds-' + row['id']],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=grace + 10)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if alive(row['child_pid'], row['child_start']):
        try:
            os.killpg(row['child_pid'], signal.SIGTERM)
        except ProcessLookupError:
            pass


def docker_stopped(row):
    """False also means unknown: a daemon error cannot release a reservation."""
    try:
        result = subprocess.run(['docker', 'container', 'ls', '-a',
            '--filter', 'name=^/agent-builds-' + row['id'] + '$',
            '--format', '{{.State}}'], stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, timeout=10)
        return result.returncode == 0 and all(
            state in ('exited', 'dead') for state in result.stdout.splitlines())
    except (OSError, subprocess.TimeoutExpired):
        return False


def exited(child):
    # Keep the process leader unreaped until cleanup, so its process-group ID
    # cannot be reused while cancellation is signalling descendants.
    info = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    return info is not None and info.si_pid != 0


class LocalDeadline:
    """Signal only our unreaped native process group, independent of telemetry.

    The owner must close/join this guard before reaping its child, including on
    exceptions. That pins the process-group ID until all watchdog signals stop.
    Docker continues through its existing stop/reconciliation path.
    """
    def __init__(self, pid, deadline, grace=3):
        self.pid, self.deadline, self.grace = pid, deadline, grace
        self.stopped, self.expired = threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self._watch, name='buildr-local-deadline', daemon=True)

    def start(self):
        self.thread.start()

    def _signal(self, signum):
        try:
            os.killpg(self.pid, signum)
        except ProcessLookupError:
            pass

    def _watch(self):
        if self.stopped.wait(max(0, self.deadline-time.monotonic())):
            return
        self.expired.set()
        self._signal(signal.SIGTERM)
        if not self.stopped.wait(self.grace):
            self._signal(signal.SIGKILL)

    def close(self):
        self.stopped.set()
        if self.thread.ident is not None:
            self.thread.join()


def notify(row):
    if os.environ.get('HERDR_ENV') != '1' or not shutil.which('herdr'):
        return
    try:
        subprocess.run(['herdr', 'notification', 'show',
                        'Build {}: {}'.format(row['id'], row['state']),
                        '--body', Path(row['cwd']).name, '--sound', 'none'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        pass


def detach(args):
    result = submit_detached(args)
    if args.events_json:
        print(json.dumps(dict(result, state='submitted', timestamp=time.time())), file=sys.stderr, flush=True)
    else:
        print('submitted {} detached | log {}'.format(result['job_id'], result['log_path']), flush=True)
    return 0


def submit_detached(args, receipt=None, retain_receipt=False):
    """Submit one detached runner and return its receipt without printing."""
    if os.environ.get('AGENT_BUILDS_JOB_ID'):
        raise ValueError('Already inside a queued job; nested submissions are not allowed')
    store = Store()
    receipt = Path(receipt) if receipt is not None else store.root / ('receipt-' + uuid.uuid4().hex + '.json')
    if receipt.parent != store.root or not receipt.name.startswith('receipt-') or receipt.exists() or receipt.is_symlink():
        store.close()
        raise ValueError('Detached receipt must be a new private queue path')
    transcript = store.root / 'logs' / (receipt.stem + '.log')
    env = os.environ.copy()
    root = str(Path(__file__).resolve().parent.parent)
    env['PYTHONPATH'] = root + os.pathsep + env.get('PYTHONPATH', '')
    env['AGENT_BUILDS_STATE'] = str(store.root)
    cwd = str(Path(args.cwd).expanduser().resolve())
    input_files = normalize_inputs(cwd, getattr(args, 'input_files', []))
    command = [sys.executable, '-m', 'agent_builds', 'run', '--cwd', cwd,
               '--lane', args.lane, '--backend', args.backend, '--receipt-file', str(receipt),
               '--min-free-gb', str(args.min_free_gb), '--scratch-gb', str(args.scratch_gb)]
    for name, value in [('jobs', args.jobs), ('memory-gb', args.memory_gb), ('timeout', args.timeout),
                        ('image', args.image), ('resource-key', args.resource_key)]:
        if value is not None and value != '':
            command += ['--' + name, str(value)]
    for key in getattr(args, 'locks', []):
        command += ['--lock', key]
    for path in input_files:
        command += ['--input-file', path]
    if args.notify:
        command.append('--notify')
    if getattr(args, 'track_source', False):
        command.append('--track-source')
    argv = list(args.command)
    if argv and argv[0] == '--':
        argv.pop(0)
    command += ['--'] + argv
    try:
        with transcript.open('xb') as output:
            os.chmod(str(transcript), 0o600)
            child = subprocess.Popen(command, cwd=root, env=env, stdin=subprocess.DEVNULL,
                                     stdout=output, stderr=output, start_new_session=True)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if receipt.exists():
                result = json.loads(receipt.read_text())
                if not retain_receipt:
                    receipt.unlink()
                return result
            if child.poll() is not None:
                raise ValueError('Detached runner failed: ' + transcript.read_text(errors='replace')[-500:])
            time.sleep(.05)
        raise ValueError('Runner started, but receipt is delayed. Inspect status and ' + str(receipt))
    finally:
        store.close()


def spawn_gated(command, cwd, env):
    read_fd, release_fd = os.pipe()
    try:
        child = subprocess.Popen([sys.executable, '-I', str(Path(__file__).with_name('launch.py')),
            str(read_fd), '--']+command, cwd=str(cwd), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True, pass_fds=(read_fd,))
    except BaseException:
        os.close(release_fd)
        raise
    finally:
        os.close(read_fd)
    return child, release_fd


def execute(args, prepared=None, external_probe=None):
    if os.environ.get('AGENT_BUILDS_JOB_ID'):
        raise ValueError('Already inside queued job {}. Queue the whole driver once, or its individual build steps; do not nest wrappers.'.format(os.environ['AGENT_BUILDS_JOB_ID']))
    if args.detach:
        return detach(args)
    store = Store()
    job = None
    if prepared is not None:
        from .plans import attach_job
        args, job = attach_job(store, *prepared)
    if external_probe is not None:
        from .external import attach_job
        args, job = attach_job(store, *external_probe)
    config = store.config()
    cwd = Path(args.cwd).expanduser().resolve()
    command = list(args.command)
    if command and command[0] == '--':
        command.pop(0)
    slots = args.jobs if args.jobs is not None else config[args.lane + '_jobs']
    memory = args.memory_gb if args.memory_gb is not None else (6 if args.lane == 'heavy' else 2)
    timeout = args.timeout if args.timeout is not None else config['timeout_seconds']
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('Timeout must be positive')
    image = args.image or config['docker_image']
    if args.backend == 'docker' and ',' in str(cwd):
        raise ValueError('Docker bind paths containing commas are unsupported')
    input_files = normalize_inputs(cwd, getattr(args, 'input_files', []))
    track_source = getattr(args, 'track_source', False) or bool(input_files)
    if job is None:
        source = {'submission': capture(cwd, input_files)} if track_source else {}
        job = store.submit(cwd, command, args.backend, args.lane, slots, memory,
                           args.resource_key or resource_key(cwd), image, timeout,
                           min_free_gb=args.min_free_gb, scratch_gb=args.scratch_gb, locks=getattr(args, 'locks', []), source=source)
    if args.receipt_file:
        receipt = Path(args.receipt_file)
        if receipt.parent != store.root or not receipt.name.startswith('receipt-'):
            store.finish(job, 'failed', 2, 'invalid receipt path')
            store.close()
            raise ValueError('Receipt must be in the private queue directory')
        temporary = receipt.with_suffix('.tmp')
        temporary.write_text(json.dumps(dict(job_id=job, log_path=str(store.log_path(job)), cwd=str(cwd))))
        os.chmod(str(temporary), 0o600)
        temporary.replace(receipt)
    def event(state, text, **extra):
        if args.events_json:
            print(json.dumps(dict(job_id=job, state=state, timestamp=time.time(),
                                  log_path=str(store.log_path(job)), **extra)), file=sys.stderr, flush=True)
        else:
            print(text, flush=True)
    event('queued', 'queued {} ({}, {} CPU slots, {} GiB reserved)'.format(job, args.backend, slots, memory),
          argv=command, cwd=str(cwd))
    cancelled = [False]
    previous = {}
    def on_signal(signum, frame):
        cancelled[0] = True
    for sig in (signal.SIGTERM, signal.SIGINT):
        previous[sig] = signal.signal(sig, on_signal)
    child, deadline_guard, release_fd = None, None, None
    try:
        from .waits import WaitUpdates
        wait_updates = WaitUpdates()
        from .polling import AdmissionPoll
        admission_poll = AdmissionPoll(store)
        while True:
            store.recover_if_due()
            control = store.get_control(job)
            if cancelled[0] or control['cancel_requested']:
                store.finish(job, 'cancelled', 130, 'cancelled while queued')
                event('cancelled', 'cancelled ' + job, exit_code=130)
                return 130
            if not admission_poll.due():
                time.sleep(0.3)
                continue
            if store.claim(job):
                break
            row = store.get(job)
            # Cancellation can arrive between the pre-claim read and claim's
            # transaction. It returns False without recording a wait blocker;
            # settle cancellation at the top rather than formatting NULL reason.
            if cancelled[0] or row['cancel_requested']:
                continue
            reason = row['reason'] or ''
            if wait_updates.due(reason, row['wait_context'], time.monotonic()):
                context = row['wait_context']
                blocker = context.get('blocking_job')
                detail = ' | blocker: ' + blocker['command_display'] if blocker else ''
                detail += ' | disk {:.1f}/{:.1f} GiB (free/required)'.format(context.get('disk_free_gb', 0), context.get('required_free_gb', 0))
                event('waiting', 'waiting {} ({:.1f}s): {}{}'.format(job, row['wait_seconds'], reason, detail),
                      wait_seconds=row['wait_seconds'], reason=reason, resources=context,
                      wait_breakdown=row['wait_breakdown'])
            time.sleep(0.3)
        row = store.get(job)
        event('running', 'running {} after {:.1f}s waiting | log {}'.format(job, row['wait_seconds'], row['log_path']),
              wait_seconds=row['wait_seconds'])
        source = row['source']
        if track_source:
            from .source import changed
            source.update(start=capture(cwd, input_files), command_started=False)
            source['changed_since_submission'] = changed(source.get('submission', {}), source['start'])
            store.record_source(job, source)
            if getattr(args, 'source_policy', 'report') == 'require_unchanged' and source['changed_since_submission'] is not False:
                reason = 'source changed or identity unavailable before start; command was not executed'
                store.finish(job, 'failed', 2, reason)
                event('failed', 'failed {}: {}'.format(job, reason), exit_code=2)
                return 2
        if cancelled[0] or store.get(job)['cancel_requested']:
            store.finish(job, 'cancelled', 130, 'cancelled before command launch')
            event('cancelled', 'cancelled '+job, exit_code=130)
            return 130
        env = os.environ.copy()
        env.update(CARGO_BUILD_JOBS=str(slots), RUST_TEST_THREADS=str(slots), AGENT_BUILDS_JOB_ID=job)
        # Command arguments remain arguments: no implicit shell evaluation.
        from .execution_policy import check_cwd
        check_cwd(cwd)
        from .batch import pending_report
        batch = pending_report(json.loads(row['command']),slots)
        if batch is not None:
            store.db.execute('UPDATE jobs SET batch=? WHERE id=?',(json.dumps(batch),job))
        child, release_fd = spawn_gated(command_for(row, config), cwd, env)
        deadline = time.monotonic() + timeout
        if row['backend'] == 'local':
            deadline_guard = LocalDeadline(child.pid, deadline)
            deadline_guard.start()
        store.db.execute('UPDATE jobs SET child_pid=?,child_start=? WHERE id=?',
                         (child.pid, identity(child.pid), job))
        released = not cancelled[0] and not store.get(job)['cancel_requested'] and (deadline_guard is None or not deadline_guard.expired.is_set())
        if released:
            try:
                os.write(release_fd, b'1')
            except BrokenPipeError:
                if deadline_guard is None or not deadline_guard.expired.is_set():
                    raise
                released = False
        os.close(release_fd)
        release_fd = None
        if track_source:
            source['command_started'] = released
            store.record_source(job, source)
        sampler = JobSampler(child.pid, identity(child.pid)) if row['backend'] == 'local' else None
        last_sample = 0
        last_persist = 0
        selector = selectors.DefaultSelector()
        selector.register(child.stdout, selectors.EVENT_READ)
        stopped_at = None
        reason = ''
        with store.log_path(job).open('ab', buffering=0) as log:
            os.chmod(str(store.log_path(job)), 0o600)
            log.write(('$ ' + shlex.join(command) + '\n').encode())
            while selector.get_map() or not exited(child):
                if sampler is not None and time.monotonic() - last_sample >= .5:
                    usage = sampler.sample()
                    last_sample = time.monotonic()
                    if last_sample - last_persist >= 2:
                        store.record_usage(job, usage)
                        last_persist = last_sample
                row = store.get(job)
                timed_out = time.monotonic() >= deadline
                if stopped_at is None and (cancelled[0] or row['cancel_requested'] or timed_out):
                    reason = 'timeout' if timed_out else 'cancelled'
                    store.cancel(job)
                    terminate(row)
                    stopped_at = time.monotonic()
                if stopped_at is not None and time.monotonic() - stopped_at > 3:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    # A descendant could retain the output pipe after its leader exits.
                    if exited(child):
                        break
                for key, _ in selector.select(0.2):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        log.write(chunk)
                        if not getattr(args, 'quiet_output', False):
                            try:
                                sys.stdout.buffer.write(chunk)
                                sys.stdout.buffer.flush()
                            except BrokenPipeError:
                                pass
            # The leader is deliberately unreaped, so its process-group ID
            # cannot be reused. A crashed wrapper may leave descendants with
            # redirected output; stop those before releasing its reservation.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if deadline_guard is not None:
                deadline_guard.close()
                if not reason and deadline_guard.expired.is_set():
                    reason = 'timeout'
            code = child.wait()
        selector.close()
        child.stdout.close()
        if sampler is not None:
            store.record_usage(job, sampler.result(finished=True))
        if track_source:
            source['finish'] = capture(cwd, input_files)
            source['changed_during_run'] = changed(source['start'], source['finish'])
            store.record_source(job, source)
        if row['backend'] == 'docker' and not docker_stopped(row):
            store.db.execute("UPDATE jobs SET state='cancelling',reason=? WHERE id=?",
                             ('Docker outcome unknown; cancel to reconcile', job))
            return 1
        if reason:
            result = 124 if reason == 'timeout' else 130
            store.finish(job, 'cancelled', result, reason)
        else:
            result = code if code >= 0 else 128 - code
            store.finish(job, 'succeeded' if code == 0 else 'failed', result)
        if args.notify:
            notify(store.get(job))
        final = store.get(job)
        event(final['state'], 'finished {}: {} (exit {})'.format(job, final['state'], result),
              exit_code=result, elapsed_seconds=final['elapsed_seconds'])
        return result
    except BaseException as error:
        row = store.get(job)
        if child is not None and child.returncode is None:
            if deadline_guard is not None:
                deadline_guard.close()
            if row['backend'] == 'local':
                # This child is still ours and unreaped even if persisting its
                # PID failed. Do not rely on a possibly absent database identity.
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            else:
                terminate(row)
            try:
                child.wait(timeout=4)
            except subprocess.TimeoutExpired:
                if row['backend'] == 'local' or alive(child.pid, row['child_start']):
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                child.wait()
        if row['backend'] == 'docker' and not docker_stopped(row):
            store.db.execute("UPDATE jobs SET state='cancelling',reason=? WHERE id=?",
                             ('Docker outcome unknown; cancel to reconcile', job))
        else:
            store.finish(job, 'failed', 1, type(error).__name__ + ': ' + str(error))
        event('failed', 'failed {}: {}'.format(job, error), exit_code=1, error=str(error))
        raise
    finally:
        if release_fd is not None:
            os.close(release_fd)
        if deadline_guard is not None:
            deadline_guard.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        store.close()


def cancel_job(store, job):
    store.cancel(job)
    row = store.get(job)
    if row['state'] in TERMINAL:
        return
    if not alive(row['owner_pid'], row['owner_start']):
        terminate(row)
        if row['backend'] == 'docker':
            if not docker_stopped(row):
                raise ValueError('Docker job still running or unreachable; reservation retained')
        if alive(row['child_pid'], row['child_start']):
            # Recovery will settle it after the child has actually stopped.
            return
        store.finish(job, 'cancelled', 130, 'orphan cancelled')
