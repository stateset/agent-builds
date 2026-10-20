"""User display preferences, separate from shared queue configuration."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
import tempfile


@dataclass(frozen=True)
class Layout:
    focused: bool = False
    table: bool = False


def path():
    base = os.environ.get("XDG_CONFIG_HOME")
    directory = (
        Path(base) if base and Path(base).is_absolute() else Path.home() / ".config"
    )
    return directory / "buildr" / "tui.json"


def load():
    """Opening the dashboard never creates configuration or queue state."""
    try:
        descriptor = os.open(str(path()), os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return Layout(), ""
    except OSError:
        return Layout(), "Layout preferences unavailable; using defaults."
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("Not a regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read(4097)
        if len(data) > 4096:
            raise ValueError("Preferences too large")
        value = json.loads(data)
        if (
            not isinstance(value, dict)
            or type(value.get("version")) is not int
            or value["version"] != 1
        ):
            raise ValueError("Unsupported preferences")
        focused, table = value.get("focused", False), value.get("table", False)
        if type(focused) is not bool or type(table) is not bool:
            raise ValueError("Invalid display options")
        return Layout(focused, table), ""
    except (OSError, ValueError, UnicodeError, RecursionError):
        return Layout(), "Layout preferences invalid; using defaults."
    finally:
        os.close(descriptor)


def save(layout):
    """Only an explicit save writes display options; replace atomically."""
    target = path()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".tui-", suffix=".json", dir=str(target.parent)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as handle:
            json.dump(
                dict(version=1, focused=layout.focused, table=layout.table), handle
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, str(target))
    finally:
        os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
