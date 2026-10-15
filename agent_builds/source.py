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


def capture(cwd):
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


def changed(first, second):
    if first.get('status') != 'captured' or second.get('status') != 'captured':
        return None
    keys = ('repository', 'head', 'tracked_diff_sha256', 'untracked_sha256')
    return any(first.get(key) != second.get(key) for key in keys)


def digest_file(path, limit=256*1024**2, seconds=5):
    path = Path(path)
    result = dict(path=str(path), status='unavailable', sha256=None)
    try:
        info = path.lstat()
        result['bytes'] = info.st_size
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
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            os.close(descriptor)
            result['status'] = 'changed_during_capture'
            return result
        with os.fdopen(descriptor, 'rb') as source:
            while True:
                chunk = source.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit or time.monotonic() > deadline:
                    result['status'] = 'capture_limit'
                    return result
                digest.update(chunk)
        current = path.lstat()
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns):
            result['status'] = 'changed_during_capture'
        else:
            result.update(status='captured', sha256=digest.hexdigest())
    except FileNotFoundError:
        result['status'] = 'missing'
    except OSError as error:
        result['reason'] = type(error).__name__
    return result
