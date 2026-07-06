"""Seasonal (day-of-week x hour-of-day) coverage check.

Whether a log spans enough calendar time for a day-of-week/hour-of-day
comparison to be meaningful is a property of its calendar *range*, not its
record count: a million calls crammed into one afternoon still can't
distinguish a routine Monday morning from a quiet Friday night, while a
sparse log spanning several weeks can. `MIN_SEASONAL_SPAN_DAYS` is that
range requirement.

This module only answers "is seasonal comparison worth attempting at all
for this log" -- it doesn't itself bucket anything. `FrequencyDetector`
separately still falls back to a flat, non-seasonal comparison for any
individual (weekday, hour) bucket that lacks enough distinct-date history of
its own, the same graceful degradation already used throughout `anomaly/`
(e.g. `baseline.analyze`'s group-to-model fallback).
"""

from __future__ import annotations

from typing import Sequence

from ..logreader import parse_timestamp
from .constants import MIN_SEASONAL_SPAN_DAYS


def log_span_days(records: Sequence[dict]) -> float | None:
    """Calendar span between the earliest and latest parseable timestamp in
    `records`, in days. `None` if fewer than 2 parseable timestamps exist.
    """
    timestamps = [
        ts for ts in (parse_timestamp(r.get("timestamp")) for r in records) if ts is not None
    ]
    if len(timestamps) < 2:
        return None
    return (max(timestamps) - min(timestamps)).total_seconds() / 86400


def has_seasonal_coverage(records: Sequence[dict]) -> bool:
    """Whether `records` span at least `MIN_SEASONAL_SPAN_DAYS` calendar
    days -- the gate `cmd_detect` uses to decide whether to auto-enable the
    frequency detector, and that `FrequencyDetector.analyze` uses to decide
    whether to attempt seasonal bucketing at all for this log.
    """
    span = log_span_days(records)
    return span is not None and span >= MIN_SEASONAL_SPAN_DAYS


def seasonal_coverage_message(records: Sequence[dict]) -> str:
    """Human-readable, honest explanation of the seasonal-coverage decision
    for `records` -- never a silent fallback (mirrors the `insufficient_data`
    status already used elsewhere in `anomaly/`).
    """
    span = log_span_days(records)
    if span is None:
        return (
            "insufficient data for seasonal baseline: fewer than 2 records "
            "with a parseable timestamp"
        )
    if span < MIN_SEASONAL_SPAN_DAYS:
        return (
            f"insufficient data for seasonal baseline: log spans {span:.1f} "
            f"day(s), need at least {MIN_SEASONAL_SPAN_DAYS}"
        )
    return f"seasonal baseline available: log spans {span:.1f} day(s)"
