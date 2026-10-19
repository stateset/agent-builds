"""Bounded sampled host context for workflow studies, not causal attribution."""

import copy
import math
import time

from .host_pressure import PRESSURE_KINDS, pressure_value

WINDOWS = ("avg10", "avg60", "avg300")
LEGACY = {kind: kind + "_pressure" for kind in PRESSURE_KINDS}


def number(value, maximum=None):
    if type(value) not in (int, float) or value < 0:
        return None
    if maximum is not None and value > maximum:
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


class StudyPressure:
    limit = 300
    interval = 0.5
    gap_threshold = 1.0

    def __init__(self):
        self.samples = 0
        self.retained = []
        self.last = None
        self.first_at = None
        self.first_clock = self.last_clock = None
        self.max_gap = None
        self.long_gaps = 0
        self.min_available = None
        self.available_readings = 0
        self.maxima = {
            kind: {
                scope: {window: None for window in WINDOWS}
                for scope in ("some", "full")
            }
            for kind in PRESSURE_KINDS
        }
        self.valid = {
            kind: {
                scope: {window: 0 for window in WINDOWS} for scope in ("some", "full")
            }
            for kind in PRESSURE_KINDS
        }

    def observe(self, measured, at=None, clock=None):
        at = time.time() if at is None else at
        clock = time.monotonic() if clock is None else clock
        if self.first_clock is None:
            self.first_clock, self.first_at = clock, at
        if self.last_clock is not None:
            gap = max(0, clock - self.last_clock)
            self.max_gap = gap if self.max_gap is None else max(gap, self.max_gap)
            self.long_gaps += gap > self.gap_threshold
        self.last_clock = clock
        sample = dict(at=at, available_gb=number(measured.get("available_gb")), psi={})
        for kind in PRESSURE_KINDS:
            sample["psi"][kind] = {}
            for scope in ("some", "full"):
                values = {}
                for window in WINDOWS:
                    value = (
                        None
                        if kind == "cpu" and scope == "full"
                        else number(pressure_value(measured, kind, scope, window), 100)
                    )
                    values[window] = value
                    if value is not None:
                        self.valid[kind][scope][window] += 1
                        previous = self.maxima[kind][scope][window]
                        self.maxima[kind][scope][window] = (
                            value if previous is None else max(previous, value)
                        )
                raw = measured.get("psi", {}).get(kind, {}).get(scope) or {}
                total = raw.get("total_us")
                values["total_us"] = (
                    total
                    if type(total) is int
                    and total >= 0
                    and not (kind == "cpu" and scope == "full")
                    else None
                )
                sample["psi"][kind][scope] = (
                    values if any(v is not None for v in values.values()) else None
                )
            sample[LEGACY[kind]] = (sample["psi"][kind]["some"] or {}).get("avg10")
        available = sample["available_gb"]
        if available is not None:
            self.available_readings += 1
            self.min_available = (
                available
                if self.min_available is None
                else min(available, self.min_available)
            )
        self.samples += 1
        self.last = sample
        if len(self.retained) < self.limit:
            self.retained.append(sample)

    def report(self):
        return copy.deepcopy(
            dict(
                samples=self.samples,
                retained=self.retained,
                max_avg10={
                    LEGACY[kind]: self.maxima[kind]["some"]["avg10"]
                    for kind in PRESSURE_KINDS
                },
                min_available_gb=self.min_available,
                valid_available_memory_readings=self.available_readings,
                sampling_interval_seconds=self.interval,
                retention="first_samples",
                retention_limit=self.limit,
                retained_samples=len(self.retained),
                omitted_samples=self.samples - len(self.retained),
                last_observation=self.last,
                first_observation_at=self.first_at,
                last_observation_at=self.last["at"] if self.last else None,
                observation_span_seconds=max(0, self.last_clock - self.first_clock)
                if self.samples
                else None,
                max_sample_gap_seconds=self.max_gap,
                long_sample_gaps=self.long_gaps,
                gap_threshold_seconds=self.gap_threshold,
                valid_readings=self.valid,
                unavailable_readings={
                    kind: {
                        scope: {
                            window: self.samples - self.valid[kind][scope][window]
                            for window in WINDOWS
                        }
                        for scope in ("some", "full")
                    }
                    for kind in PRESSURE_KINDS
                },
                max_psi_percent=self.maxima,
                note="Sample counts and gaps describe observation coverage, not time-weighted stall duration. Host PSI does not attribute contention or prove a queue speedup.",
            )
        )
