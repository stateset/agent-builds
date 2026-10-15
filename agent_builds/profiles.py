"""Private, explicit per-repository shortcuts and dedicated worker profiles."""
import json
import os
from pathlib import Path
import re
import uuid


def load(store):
    path = store.root / 'profiles.json'
    return json.loads(path.read_text()) if path.exists() else {'tasks': {}, 'workers': {}}


def update(store, section, key, value):
    with store.transaction():
        data = load(store)
        data.setdefault(section, {})[key] = value
        temporary = store.root / ('profiles-' + uuid.uuid4().hex + '.tmp')
        try:
            with temporary.open('x') as out:
                os.chmod(str(temporary), 0o600)
                json.dump(data, out, indent=2)
                out.flush()
                os.fsync(out.fileno())
            temporary.replace(store.root / 'profiles.json')
        finally:
            if temporary.exists():
                temporary.unlink()


def name(value):
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}', value):
        raise ValueError('Use a name of 1 to 64 letters, digits, dots, dashes or underscores')
    return value


def repository(cwd):
    # Shortcuts belong to the checkout, independently of CARGO_TARGET_DIR.
    import subprocess
    path = Path(cwd).expanduser().resolve()
    if not path.is_dir():
        raise ValueError('Repository directory does not exist')
    result = subprocess.run(['git', '-C', str(path), 'rev-parse', '--show-toplevel'],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    return result.stdout.strip() if result.returncode == 0 else str(path)


def task_key(cwd, task):
    return repository(cwd) + ':' + name(task)


def save_task(store, args):
    command = list(args.command)
    if command and command[0] == '--':
        command.pop(0)
    if not command:
        raise ValueError('A shortcut requires a command')
    values = {field: getattr(args, field) for field in
              ('lane', 'backend', 'jobs', 'memory_gb', 'image', 'timeout', 'min_free_gb', 'scratch_gb', 'resource_key')}
    from .locks import normalize
    additional = getattr(args, 'locks', [])
    normalize(values['resource_key'] or 'default', additional)
    values['locks'] = additional
    values.update(command=command, cwd=str(Path(args.cwd).expanduser().resolve()), name=name(args.name))
    from .source import normalize_inputs
    values['input_files'] = normalize_inputs(values['cwd'], getattr(args, 'input_files', []))
    config = store.config()
    import math
    if values['jobs'] is not None and not 1 <= values['jobs'] <= config['cpu_slots']:
        raise ValueError('Shortcut CPU slots exceed machine budget')
    for field in ('memory_gb', 'timeout'):
        if values[field] is not None and (not math.isfinite(values[field]) or values[field] <= 0):
            raise ValueError('Shortcut memory and timeout must be positive')
    if any(not math.isfinite(values[f]) or values[f] < 0 for f in ('min_free_gb', 'scratch_gb')):
        raise ValueError('Disk values must be nonnegative')
    update(store, 'tasks', task_key(args.cwd, args.name), values)
    return values


def task_args(store, args):
    from argparse import Namespace
    task = load(store).get('tasks', {}).get(task_key(args.cwd, args.name))
    if task is None:
        raise ValueError('Unknown shortcut in this repository: ' + args.name)
    values = dict(task)
    values.pop('name', None)
    values.setdefault('resource_key', None)
    values.update(detach=args.detach, events_json=args.events_json, notify=False, receipt_file=None)
    return Namespace(**values)


def save_worker(store, args):
    if not args.dedicated_build_host:
        raise ValueError('Only explicitly designated dedicated build hosts are supported')
    # SSH aliases may use user@host; never accept SSH option injection.
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.@:-]{0,252}', args.host):
        raise ValueError('Use an SSH host alias or user@host, without options')
    if not args.state_dir.startswith('/') or '\n' in args.state_dir:
        raise ValueError('Worker state directory must be absolute')
    worker = dict(host=args.host, state_dir=args.state_dir, dedicated_build_host=True)
    update(store, 'workers', name(args.name), worker)
    return worker
