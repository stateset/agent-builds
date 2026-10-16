"""Exec a native command only after its parent persists our process identity.

The parent owns the write side of an inherited pipe. EOF before authorization
means the runner exited during attachment, so no user command may execute.
This module intentionally imports no application code and runs in isolated Python.
"""

import os
import sys


def main() -> int:
    descriptor = int(sys.argv[1])
    try:
        authorized = os.read(descriptor, 1) == b"1"
    finally:
        os.close(descriptor)
    if not authorized:
        return 125
    try:
        os.execvpe(sys.argv[3], sys.argv[3:], os.environ)
    except OSError as error:
        print("Buildr could not execute command: " + str(error), file=sys.stderr)
        return 127


if __name__ == "__main__":
    sys.exit(main())
