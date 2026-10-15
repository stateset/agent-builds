"""Bounded source fingerprints and file evidence; never persist source contents."""
import hashlib
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import time


def git(cwd, arguments, deadline, limit=16*1024**2):
    child = subprocess.Popen(['git', '-c', 'core.fsmonitor=false', '-C', str(cwd)] + arguments,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    selector = selectors.DefaultSelector()
    selector.register(child.stdout, selectors.EVENT_READ)
    result = bytearray()
    try:
        while selector.get_map():
            if time.monotonic() >= deadline:
                raise ValueError('source capture time limit reached')
            for key, _ in selector.select(.1):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                else:
                    result.extend(chunk)
                    if len(result) > limit:
                        raise ValueError('source capture size limit reached')
        if child.wait(timeout=max(.01, deadline-time.monotonic())):
            raise ValueError('Git source identity unavailable')
        return bytes(result)
    finally:
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        child.wait()
        selector.close()
        child.stdout.close()


def capture_repository(cwd):
    result = dict(captured_at=time.time(), status='unavailable',
        scope='Git HEAD, tracked diff, and nonignored untracked files; ignored files, environment and external inputs are excluded')
    deadline = time.monotonic()+6
    try:
        root = Path(os.fsdecode(git(cwd, ['rev-parse', '--show-toplevel'], deadline, 65536)).strip())
        head = git(root, ['rev-parse', '--verify', 'HEAD'], deadline, 1024).decode().strip()
        diff = git(root, ['diff', '--no-ext-diff', '--no-textconv', '--binary', 'HEAD', '--'], deadline)
        untracked = git(root, ['ls-files', '--others', '--exclude-standard', '-z'], deadline, 1024**2).split(b'\0')
        digest = hashlib.sha256()
        size = 0
        count = 0
        for name in sorted(n for n in untracked if n):
            if time.monotonic() >= deadline:
                raise ValueError('source capture time limit reached')
            path = root / os.fsdecode(name)
            info = path.lstat()
            digest.update(len(name).to_bytes(8, 'big')+name+stat.S_IMODE(info.st_mode).to_bytes(4, 'big'))
            content = hashlib.sha256()
            if stat.S_ISLNK(info.st_mode):
                content.update(os.fsencode(os.readlink(str(path))))
            elif stat.S_ISREG(info.st_mode):
                if root not in path.resolve().parents:
                    raise ValueError('untracked source path escapes checkout')
                evidence = digest_file(path, limit=64*1024**2-size, seconds=max(0, deadline-time.monotonic()))
                if evidence['status'] != 'captured':
                    raise ValueError('untracked source capture incomplete: '+evidence['status'])
                size += evidence['bytes']
            else:
                raise ValueError('unsupported untracked source file type')
            current = path.lstat()
            if (info.st_ino, info.st_size, info.st_mtime_ns) != (current.st_ino, current.st_size, current.st_mtime_ns):
                raise ValueError('source changed during capture')
            digest.update(bytes.fromhex(evidence['sha256']) if stat.S_ISREG(info.st_mode) else content.digest())
            count += 1
        if git(root, ['rev-parse', '--verify', 'HEAD'], deadline, 1024).decode().strip() != head:
            raise ValueError('HEAD changed during source capture')
        if git(root, ['diff', '--no-ext-diff', '--no-textconv', '--binary', 'HEAD', '--'], deadline) != diff:
            raise ValueError('tracked source changed during capture')
        if git(root, ['ls-files', '--others', '--exclude-standard', '-z'], deadline, 1024**2).split(b'\0') != untracked:
            raise ValueError('untracked source list changed during capture')
        result.update(status='captured', repository=str(root), head=head,
            tracked_diff_sha256=hashlib.sha256(diff).hexdigest(), tracked_diff_bytes=len(diff),
            untracked_sha256=digest.hexdigest(), untracked_files=count, untracked_bytes=size,
            dirty=bool(diff or count))
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        result['reason'] = str(error) if isinstance(error, ValueError) else type(error).__name__
    return result


def normalize_inputs(cwd, paths):
    """Normalize literal paths without resolving a missing file or leaf symlink."""
    if not isinstance(paths, (list, tuple)) or len(paths) > 16:
        raise ValueError('input_files must be an array of at most 16 literal file paths')
    result = set()
    for value in paths:
        if not isinstance(value, str) or not value or len(value) > 4096 or '\0' in value:
            raise ValueError('Input file paths must contain 1 to 4096 characters without NUL')
        path = os.path.expanduser(value)
        path = os.path.abspath(os.path.join(str(cwd), path))
        if len(path) > 4096:
            raise ValueError('Normalized input file path exceeds 4096 characters')
        result.add(path)
    return sorted(result)


def capture(cwd, input_files=()):
    paths = normalize_inputs(cwd, input_files)
    result = capture_repository(cwd)
    if not paths:
        return result
    result['repository_status'] = result['status']
    deadline = time.monotonic()+3
    remaining = 16*1024**2
    files = []
    for path in paths:
        if time.monotonic() >= deadline:
            evidence = dict(path=path, status='capture_limit', sha256=None)
        else:
            evidence = digest_file(path, limit=min(8*1024**2, remaining),
                                   seconds=max(0, deadline-time.monotonic()))
        remaining = max(0, remaining-evidence.get('read_bytes', 0))
        files.append(evidence)
    complete = all(item['status'] == 'captured' for item in files)
    result['inputs'] = dict(status='captured' if complete else 'incomplete', files=files,
        max_files=16, max_file_bytes=8*1024**2, max_total_bytes=16*1024**2, max_seconds=3)
    result['scope'] = ('Git HEAD, tracked diff, nonignored untracked files and explicitly declared input files; '
                       'other ignored files, environment and undeclared external inputs are excluded')
    if not complete:
        result.update(status='unavailable', reason='Declared input capture incomplete')
    return result


def changed(first, second):
    """True means definite drift; None means incomplete required observations."""
    complete = True
    if (first.get('repository_status', first.get('status')) == 'captured' and
            second.get('repository_status', second.get('status')) == 'captured'):
        keys = ('repository', 'head', 'tracked_diff_sha256', 'untracked_sha256')
        if any(first.get(key) != second.get(key) for key in keys):
            return True
    else:
        complete = False
    left, right = first.get('inputs', {}), second.get('inputs', {})
    a = {item['path']: item for item in left.get('files', [])}
    b = {item['path']: item for item in right.get('files', [])}
    if set(a) != set(b):
        return True
    for path in a:
        if a[path].get('status') == b[path].get('status') == 'captured':
            if any(a[path].get(key) != b[path].get(key) for key in ('sha256', 'bytes', 'mode')):
                return True
        else:
            complete = False
    if ((left and left.get('status') != 'captured') or
            (right and right.get('status') != 'captured')):
        complete = False
    return False if complete else None


def digest_file(path, limit=256*1024**2, seconds=5):
    path = Path(path)
    result = dict(path=str(path), status='unavailable', sha256=None)
    try:
        info = path.lstat()
        result['bytes'] = info.st_size
        result['mode'] = stat.S_IMODE(info.st_mode)
        if not stat.S_ISREG(info.st_mode):
            result['status'] = 'not_regular_file'
            return result
        if info.st_size > limit:
            result['status'] = 'size_limit'
            return result
        deadline = time.monotonic()+seconds
        digest = hashlib.sha256()
        total = 0
        descriptor = os.open(str(path), os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
        opened = os.fstat(descriptor)
        identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns, value.st_mode)
        if not stat.S_ISREG(opened.st_mode) or identity(opened) != identity(info):
            os.close(descriptor)
            result['status'] = 'changed_during_capture'
            return result
        with os.fdopen(descriptor, 'rb') as source:
            while total < limit:
                chunk = source.read(min(65536, limit-total))
                if not chunk:
                    break
                total += len(chunk)
                result['read_bytes'] = total
                if total > limit or time.monotonic() > deadline:
                    result['status'] = 'capture_limit'
                    return result
                digest.update(chunk)
        current = path.lstat()
        if identity(info) != identity(current) or total != info.st_size or time.monotonic() > deadline:
            result['status'] = 'changed_during_capture'
        else:
            result.update(status='captured', sha256=digest.hexdigest())
    except FileNotFoundError:
        result['status'] = 'missing'
    except OSError as error:
        result['reason'] = type(error).__name__
    return result
