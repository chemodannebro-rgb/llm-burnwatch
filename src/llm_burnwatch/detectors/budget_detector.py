"""Budget detector: flags month-to-date spend that has already exceeded a
user-configured monthly budget, or is on pace to exceed it by month-end.

Unlike the statistical detectors (baseline/frequency/cusum), this is the
caller's own explicit policy -- a monthly USD budget and a warn-at fraction,
set via `llm-burnwatch budget set` and persisted to `budget.json`
(`budget.load_budget`/`tracker.user_budget_path`) -- the same "hard-limit
policy, not a heuristic" category as `RulesDetector`'s `--max-call-cost`/
`--max-trace-cost`.

`enabled_by_default = False`: stays a deliberate no-op -- silent, not an
error -- until the caller has actually run `budget set` (mirrors the
"unconfigured is a no-op" precedent set by `RulesDetector`, even though
`RulesDetector` itself is enabled by default; `BudgetDetector` additionally
needs an explicit `enabled_overrides={"budget": True}` from the engine's
caller once `budget.json` exists, since -- unlike `RulesDetector` -- its
constructor arguments come from a config file the CLI must check for on
every run, not from flags on `detect` itself).

Algorithm is deliberately simple, by design: sum `cost_micros` for every
record whose timestamp falls in the current UTC calendar month, then
linearly extrapolate "month-to-date total / days elapsed so far * days in
month" to a projected month-end total. This does *not* reuse the seasonal
(weekday x hour) baselines from `anomaly/seasonal.py` -- that answers a
different statistical question ("is this hour unusual for this weekday?"),
not "will this month's spend exceed the budget at the current pace?" --
conflating the two would add complexity without actually improving the
forecast.

The linear-pace forecast is inherently noisy early in the month (few days of
data, high variance) -- `compute_budget_status` flags this via
`low_confidence`/`days_elapsed` in its result so callers (this detector's
alert message, and `report`'s Budget section) can say so explicitly instead
of presenting an early-month projection with unwarranted confidence.
"""

from __future__ import annotations

import calendar
from datetime import datetime, timezone
from typing import Sequence

from ..logreader import parse_timestamp
from .protocol import Alert

MICROS_PER_USD = 1_000_000

# Below this many elapsed days in the current month, the linear-pace
# forecast is flagged as low-confidence (too little data for the
# extrapolation to be trustworthy) -- surfaced, not hidden, so an operator
# isn't misled by an early-month projection swinging wildly call to call.
LOW_CONFIDENCE_DAY_THRESHOLD = 3


def compute_budget_status(
    records: Sequence[dict],
    monthly_usd: float,
    warn_at_fraction: float,
    now: datetime | None = None,
) -> dict | None:
    """Compute this UTC calendar month's budget status from `records`.

    Returns `None` if no record in `records` falls in the current month --
    there is nothing to report yet, distinct from "zero cost so far" (which
    would still produce a status with `month_to_date_usd == 0.0`).

    Returns a dict: `month` (`"YYYY-MM"`), `month_to_date_usd`,
    `monthly_usd`, `warn_at_fraction`, `forecast_usd`, `days_elapsed`,
    `days_in_month`, `low_confidence`, `last_record_index` (index into
    `records` of the last record counted -- not necessarily the record that
    pushed the total over a threshold, just the most recent one in-month),
    `over_budget` and `pace_warning` (booleans).
    """
    now = now or datetime.now(timezone.utc)
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    days_elapsed = now.day

    total_micros = 0
    last_record_index: int | None = None
    for i, r in enumerate(records):
        ts = parse_timestamp(r.get("timestamp"))
        if ts is None or ts.year != now.year or ts.month != now.month:
            continue
        total_micros += r.get("cost_micros") or 0
        last_record_index = i

    if last_record_index is None:
        return None

    month_to_date_usd = total_micros / MICROS_PER_USD
    avg_daily_usd = month_to_date_usd / days_elapsed
    forecast_usd = avg_daily_usd * days_in_month

    return {
        "month": f"{now.year:04d}-{now.month:02d}",
        "month_to_date_usd": month_to_date_usd,
        "monthly_usd": monthly_usd,
        "warn_at_fraction": warn_at_fraction,
        "forecast_usd": forecast_usd,
        "days_elapsed": days_elapsed,
        "days_in_month": days_in_month,
        "low_confidence": days_elapsed < LOW_CONFIDENCE_DAY_THRESHOLD,
        "last_record_index": last_record_index,
        "over_budget": month_to_date_usd > monthly_usd,
        "pace_warning": forecast_usd > monthly_usd * warn_at_fraction,
    }


class BudgetDetector:
    name = "budget"
    enabled_by_default = False

    def __init__(
        self,
        monthly_usd: float | None = None,
        warn_at_fraction: float | None = None,
        *,
        now: datetime | None = None,
    ) -> None:
        self.monthly_usd = monthly_usd
        self.warn_at_fraction = warn_at_fraction
        self._now = now

    def analyze(self, records: Sequence[dict]) -> list[Alert]:
        if self.monthly_usd is None or self.warn_at_fraction is None:
            return []

        status = compute_budget_status(
            records, self.monthly_usd, self.warn_at_fraction, now=self._now
        )
        if status is None:
            return []

        group_key = ("budget", status["month"])
        low_confidence_note = (
            f" (low confidence: only {status['days_elapsed']} day(s) elapsed this month)"
            if status["low_confidence"]
            else ""
        )

        if status["over_budget"]:
            return [
                Alert(
                    detector=self.name,
                    severity="critical",
                    kind="budget_exceeded",
                    group_key=group_key,
                    record_ref=status["last_record_index"],
                    evidence=status,
                    message=(
                        f"month-to-date cost ${status['month_to_date_usd']:.2f} exceeds "
                        f"monthly budget ${status['monthly_usd']:.2f}" + low_confidence_note
                    ),
                )
            ]

        if status["pace_warning"]:
            return [
                Alert(
                    detector=self.name,
                    severity="warning",
                    kind="budget_pace_warning",
                    group_key=group_key,
                    record_ref=status["last_record_index"],
                    evidence=status,
                    message=(
                        f"projected month-end cost ${status['forecast_usd']:.2f} exceeds "
                        f"{status['warn_at_fraction']:.0%} of monthly budget "
                        f"${status['monthly_usd']:.2f} (month-to-date: "
                        f"${status['month_to_date_usd']:.2f})" + low_confidence_note
                    ),
                )
            ]

        return []
