"""Bounded system PSI observations; never influence admission or retry work."""

import math
from pathlib import Path


PRESSURE_KINDS = ("cpu", "memory", "io")
PRESSURE_LIMIT = 4096


def parse_pressure(text, kind):
    """Parse system PSI; CPU full is undefined, never a healthy zero."""
    result = dict(some=None, full=None)
    seen = set()
    for line in text.splitlines():
        fields = line.split()
        if not fields or fields[0] not in result:
            continue
        name = fields[0]
        if name in seen:
            result[name] = None
            continue
        seen.add(name)
        if name == "full" and kind == "cpu":
            continue
        try:
            pairs = [item.split("=", 1) for item in fields[1:]]
            values = dict(pairs)
            if len(values) != len(pairs):
                continue
            averages = {key: float(values[key]) for key in ("avg10", "avg60", "avg300")}
            if any(
                not math.isfinite(value) or not 0 <= value <= 100
                for value in averages.values()
            ):
                continue
            total = int(values["total"])
            if total < 0:
                continue
            result[name] = dict(averages, total_us=total)
        except (ValueError, KeyError):
            continue
    return result


def pressure_sample(kind):
    if kind not in PRESSURE_KINDS:
        raise ValueError("Unknown PSI resource")
    try:
        with (Path("/proc/pressure") / kind).open() as source:
            text = source.read(PRESSURE_LIMIT + 1)
        if len(text) > PRESSURE_LIMIT:
            return dict(some=None, full=None)
        return parse_pressure(text, kind)
    except (OSError, UnicodeError):
        return dict(some=None, full=None)


def pressure_value(machine, kind, scope="some", window="avg10"):
    """Accept old retained snapshots without inventing missing full readings."""
    sample = machine.get("psi", {}).get(kind, {}).get(scope)
    if sample is not None:
        return sample.get(window)
    if scope == "some" and window == "avg10" and "psi" not in machine:
        return machine.get(kind + "_pressure")
    return None


def pressure_percent(value):
    return "unknown" if value is None else "{:.1f}%".format(value)


def pressure_summary(machine):
    return "PSI10s some/full: I/O {}/{} mem {}/{} | CPU {} some".format(
        pressure_percent(pressure_value(machine, "io")),
        pressure_percent(pressure_value(machine, "io", "full")),
        pressure_percent(pressure_value(machine, "memory")),
        pressure_percent(pressure_value(machine, "memory", "full")),
        pressure_percent(pressure_value(machine, "cpu")),
    )


def pressure_lines(machine):
    yield "Host pressure: stalled time %, some/full (10s / 60s / 300s averages)"
    for kind, label in (("io", "I/O"), ("memory", "Memory"), ("cpu", "CPU")):
        some = " / ".join(
            pressure_percent(pressure_value(machine, kind, window=window))
            for window in ("avg10", "avg60", "avg300")
        )
        yield "  {} some: {}".format(label, some)
        if kind != "cpu":
            full = " / ".join(
                pressure_percent(pressure_value(machine, kind, "full", window))
                for window in ("avg10", "avg60", "avg300")
            )
            yield "  {} full: {}".format(label, full)
    yield "Host-wide observations, not CPU utilization or a queue admission decision."
