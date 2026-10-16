"""Read Linux process counters without command lines or cached identities."""

import os

MAX_STAT_BYTES = 4096


def read_processes(root="/proc"):
    processes = []
    uid = os.getuid()
    try:
        with os.scandir(root) as directories:
            for entry in directories:
                if not entry.name.isascii() or not entry.name.isdigit():
                    continue
                try:
                    if entry.stat().st_uid != uid:
                        continue
                    with open(entry.path + "/stat", "rb") as source:
                        data = source.read(MAX_STAT_BYTES + 1)
                    closing = data.rfind(b")")
                    if closing < 0 or len(data) > MAX_STAT_BYTES:
                        continue
                    fields = data[closing + 2 :].split()
                    if not fields[19].isdigit():
                        continue
                    processes.append(
                        (
                            int(entry.name),
                            fields[19].decode("ascii"),
                            int(fields[1]),
                            int(fields[2]),
                            int(fields[11]) + int(fields[12]),
                            max(0, int(fields[21])),
                        )
                    )
                except (OSError, ValueError, IndexError, UnicodeError):
                    # Processes may disappear, change credentials or expose an
                    # unreadable stat file between directory and counter reads.
                    continue
    except OSError:
        pass
    return processes


def select_tree(processes, pid, start, known):
    """Expand current identities once, including previously observed children.

    A group ID is trusted only while its original leader is present. Known
    identities still allow observing reparented descendants after that leader
    disappears. Neither this snapshot nor its membership authorizes signalling.
    """
    leader_present = any(p == pid and s == start for p, s, *_ in processes)
    selected = set()
    children = {}
    for child, child_start, parent, group, *_ in processes:
        key = (child, child_start)
        children.setdefault(parent, []).append(key)
        if key in known or (leader_present and group == pid):
            selected.add(key)
    pending = list(selected)
    while pending:
        parent, _ = pending.pop()
        for key in children.get(parent, ()):
            if key not in selected:
                selected.add(key)
                pending.append(key)
    return selected
