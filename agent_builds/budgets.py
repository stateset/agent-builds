"""Describe configured worker hints without changing the user's command."""
import os


def option(tokens, names, short=None):
    if '--' in tokens:
        tokens = tokens[:tokens.index('--')]
    selected = None
    for index, token in enumerate(tokens):
        for name in names:
            if token == name and index + 1 < len(tokens):
                selected = (name, tokens[index + 1])
            elif token.startswith(name + '='):
                selected = (name, token[len(name) + 1:])
        if short and token.startswith(short) and token != short and not token.startswith('--'):
            selected = (short, token[len(short):].lstrip('='))
    return selected


def setting(default, explicit):
    result = dict(queue_default=default, configured_value=default, source='queue_environment')
    if explicit:
        flag, raw = explicit
        try:
            value = int(raw)
        except ValueError:
            value = raw
        result.update(configured_value=value, source='command_argument', flag=flag,
                      exceeds_reservation=isinstance(value, int) and value > default)
    return result


def worker_budget(argv, slots):
    cargo = bool(argv and os.path.basename(argv[0]) == 'cargo')
    split = argv.index('--') if cargo and '--' in argv else len(argv)
    build = option(argv[1:split], ('--jobs', '-j'), short='-j') if cargo else None
    tests = option(argv[split + 1:], ('--test-threads',)) if cargo else None
    return dict(
        environment={'CARGO_BUILD_JOBS': str(slots), 'RUST_TEST_THREADS': str(slots)},
        cargo_build=setting(slots, build), rust_tests=setting(slots, tests),
        direct_cargo_command=cargo, runtime_concurrency_measured=False,
        inspection_scope='direct Cargo argv; shell scripts, custom harnesses, and subprocess settings are not inspected')
