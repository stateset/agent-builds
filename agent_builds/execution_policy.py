"""Optional cwd scope for MCP descendants; deliberately not an OS sandbox."""

import json
import os
from pathlib import Path
from typing import Optional, Sequence, Union

VARIABLE = "BUILDR_ALLOWED_ROOTS"


def configure(roots: Optional[Sequence[str]]) -> None:
    if roots is None:
        return
    normalized = []
    for root in roots:
        path = Path(root).expanduser()
        if not path.is_absolute() or not path.is_dir():
            raise ValueError(
                "Every --allow-root must be an existing absolute directory"
            )
        normalized.append(str(path.resolve()))
    if not normalized:
        raise ValueError("At least one allowed root is required")
    os.environ[VARIABLE] = json.dumps(list(dict.fromkeys(normalized)))


def check_cwd(cwd: Union[str, Path]) -> None:
    encoded = os.environ.get(VARIABLE)
    if encoded is None:
        return
    try:
        roots = json.loads(encoded)
        if (
            not isinstance(roots, list)
            or not roots
            or any(
                not isinstance(item, str) or not Path(item).is_absolute()
                for item in roots
            )
        ):
            raise ValueError()
        location = Path(cwd).resolve()
        if any(
            location == Path(root).resolve() or Path(root).resolve() in location.parents
            for root in roots
        ):
            return
    except (ValueError, TypeError, OSError):
        raise ValueError("Invalid Buildr execution policy; no command was launched")
    raise ValueError(
        "Command cwd is outside configured --allow-root directories; no command was launched"
    )
