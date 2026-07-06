"""Synthetic call log generator for demos and internal sanity testing.

Produces a realistic-looking mixture of calls across a handful of
(label, model) pairs, with a small, known number of injected anomalies
(unusually large token counts) so `llm-burnwatch detect` has something to find
on a first run, and so the internal anomaly sanity test can check that
detection actually finds them. The seed is fixed by default so demo runs
and the sanity test are deterministic, not flaky.

Beyond that single amplitude-outlier profile (consumed by `BaselineDetector`
via `analyze()`/`write_demo_log()` above), this module also exposes five
independent *scenario* generators below -- `runaway_loop`, `model_swap`,
`prompt_regression`, `gradual_drift`, `weekend_pattern` -- one per detector
added in v0.8.1-0.8.4, each modeling a distinct real money-losing incident
that the amplitude-outlier profile alone can't exercise (a burst of call
*volume*, a swapped-in disallowed model, a sudden or gradual level shift in
response size, a recurring calendar pattern). Each has its own fixed seed
(`_SCENARIO_SEEDS` below) so scenarios never share RNG state with each other
or with `generate_demo_calls` -- a risk raised on review, since two
generators drawing from the same `random.Random` instance would make each
one's output depend on whichever other scenarios happened to run first.
Every scenario returns `list[tuple[dict, str | None]]`: a schema-compliant
record plus the injected scenario name for calls that are part of the
incident, `None` for normal calls -- mirroring the existing `is_anomaly`
convention, kept out of the record itself since real logs have no such
field.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from .anomaly.constants import FREQUENCY_WINDOW_SECONDS
from .tracker import SCHEMA_VERSION, CostTracker, load_default_pricing

DEFAULT_SEED = 42

_LABELS_MODELS = [
    ("summarize", "gpt-4o"),
    ("summarize", "gpt-4o-mini"),
    ("retrieval", "gpt-4o-mini"),
    ("chat", "claude-sonnet-4"),
    ("tool-call", "claude-haiku-3.5"),
]


class DemoCall(NamedTuple):
    label: str
    model: str
    input_tokens: int
    output_tokens: int
    is_anomaly: bool


def generate_demo_calls(
    n_normal: int = 200,
    n_anomalies: int = 10,
    seed: int = DEFAULT_SEED,
) -> list[DemoCall]:
    """Return a shuffled list of synthetic calls: `n_normal` calls with
    realistic token counts drawn from a per-(label, model) baseline, plus
    `n_anomalies` calls with token counts far outside that baseline.
    """
    rng = random.Random(seed)
    calls: list[DemoCall] = []

    for _ in range(n_normal):
        label, model = rng.choice(_LABELS_MODELS)
        input_tokens = max(1, int(rng.gauss(800, 150)))
        output_tokens = max(1, int(rng.gauss(150, 40)))
        calls.append(DemoCall(label, model, input_tokens, output_tokens, False))

    for _ in range(n_anomalies):
        label, model = rng.choice(_LABELS_MODELS)
        input_tokens = int(rng.uniform(8_000, 20_000))
        output_tokens = int(rng.uniform(2_000, 5_000))
        calls.append(DemoCall(label, model, input_tokens, output_tokens, True))

    rng.shuffle(calls)
    return calls


def write_demo_log(
    path,
    *,
    n_normal: int = 200,
    n_anomalies: int = 10,
    seed: int = DEFAULT_SEED,
) -> list[tuple[dict, bool]]:
    """Generate synthetic calls and log them via a real `CostTracker` at
    `path`. Returns `(logged_record, is_anomaly)` pairs in the order they
    were written, so tests can check detection recall/precision without
    the anomaly flag needing to live in the on-disk record itself.
    """
    tracker = CostTracker(path)
    calls = generate_demo_calls(n_normal=n_normal, n_anomalies=n_anomalies, seed=seed)
    results = []
    for call in calls:
        record = tracker.log_call(
            label=call.label,
            model=call.model,
            input_tokens=call.input_tokens,
            output_tokens=call.output_tokens,
        )
        results.append((record, call.is_anomaly))
    return results


# --- Scenario generators (v0.8.7) -------------------------------------------
#
# Each scenario builds records directly (rather than via `CostTracker`,
# which always stamps the real wall-clock time) because several scenarios
# need precise, synthetic timestamps spanning windows/weeks that a live
# `log_call()` can't produce. Cost is computed with the same per-token
# formula `CostTracker._resolve_cost_micros` uses, against the packaged
# default pricing, so records stay realistic and schema-compliant.

_SCENARIO_SEEDS = {
    "runaway_loop": 101,
    "model_swap": 102,
    "prompt_regression": 103,
    "gradual_drift": 104,
    "weekend_pattern": 105,
}

_PRICING = load_default_pricing()


def _cost_micros(model: str, input_tokens: int, output_tokens: int, cached_input_tokens: int = 0) -> int:
    rates = _PRICING["models"][model]
    input_rate = rates.get("input_per_1m", 0.0)
    output_rate = rates.get("output_per_1m", 0.0)
    cached_rate = rates.get("cached_input_per_1m", input_rate)
    micros = (
        input_tokens * input_rate
        + cached_input_tokens * cached_rate
        + output_tokens * output_rate
    )
    return round(micros)


def _make_record(
    *,
    label: str,
    model: str,
    timestamp: datetime,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "timestamp": timestamp.isoformat(),
        "label": label,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_input_tokens": cached_input_tokens,
        "cost_micros": _cost_micros(model, input_tokens, output_tokens, cached_input_tokens),
    }


def runaway_loop(
    seed: int = _SCENARIO_SEEDS["runaway_loop"],
    *,
    n_history_windows: int = 30,
    calls_per_window: int = 2,
    burst_windows: int = 3,
    burst_calls_per_window: int = 40,
) -> list[tuple[dict, str | None]]:
    """A `tool-call` agent loop that runs away: `n_history_windows` windows
    of routine call volume, then `burst_windows` windows where the call
    count spikes far above that history -- the incident `FrequencyDetector`
    (0.8.1) is meant to catch. The whole span is well under
    `MIN_SEASONAL_SPAN_DAYS`, so this exercises the flat (non-seasonal)
    comparison path.
    """
    rng = random.Random(seed)
    label, model = "tool-call", "claude-haiku-3.5"
    base = datetime(2026, 2, 2, tzinfo=timezone.utc)
    results: list[tuple[dict, str | None]] = []

    for w in range(n_history_windows):
        window_start = base + timedelta(seconds=w * FREQUENCY_WINDOW_SECONDS)
        for c in range(calls_per_window):
            ts = window_start + timedelta(seconds=c * 10)
            input_tokens = max(1, int(rng.gauss(500, 50)))
            output_tokens = max(1, int(rng.gauss(120, 20)))
            record = _make_record(
                label=label, model=model, timestamp=ts,
                input_tokens=input_tokens, output_tokens=output_tokens,
            )
            results.append((record, None))

    for bw in range(burst_windows):
        window_start = base + timedelta(seconds=(n_history_windows + bw) * FREQUENCY_WINDOW_SECONDS)
        for c in range(burst_calls_per_window):
            offset = c * (FREQUENCY_WINDOW_SECONDS / (burst_calls_per_window + 1))
            ts = window_start + timedelta(seconds=offset)
            input_tokens = max(1, int(rng.gauss(500, 50)))
            output_tokens = max(1, int(rng.gauss(120, 20)))
            record = _make_record(
                label=label, model=model, timestamp=ts,
                input_tokens=input_tokens, output_tokens=output_tokens,
            )
            results.append((record, "runaway_loop"))

    return results


def model_swap(
    seed: int = _SCENARIO_SEEDS["model_swap"],
    *,
    n_normal: int = 40,
    n_swapped: int = 6,
    allowed_model: str = "gpt-4o-mini",
    swapped_model: str = "o1",
) -> list[tuple[dict, str | None]]:
    """A code change quietly swaps in a disallowed, far pricier model: `n_normal`
    calls consistently use `allowed_model`, then `n_swapped` calls in a row use
    `swapped_model` instead -- the incident `RulesDetector`'s `allowed_models`
    check (0.8.3) is meant to catch. `RulesDetector` itself ignores timestamps,
    so calls are simply ordered chronologically, one minute apart.
    """
    rng = random.Random(seed)
    label = "chat"
    base = datetime(2026, 3, 2, tzinfo=timezone.utc)
    results: list[tuple[dict, str | None]] = []

    for i in range(n_normal):
        ts = base + timedelta(minutes=i)
        input_tokens = max(1, int(rng.gauss(800, 100)))
        output_tokens = max(1, int(rng.gauss(150, 30)))
        record = _make_record(
            label=label, model=allowed_model, timestamp=ts,
            input_tokens=input_tokens, output_tokens=output_tokens,
        )
        results.append((record, None))

    for i in range(n_swapped):
        ts = base + timedelta(minutes=n_normal + i)
        input_tokens = max(1, int(rng.gauss(800, 100)))
        output_tokens = max(1, int(rng.gauss(150, 30)))
        record = _make_record(
            label=label, model=swapped_model, timestamp=ts,
            input_tokens=input_tokens, output_tokens=output_tokens,
        )
        results.append((record, "model_swap"))

    return results


def prompt_regression(
    seed: int = _SCENARIO_SEEDS["prompt_regression"],
    *,
    n_pre: int = 30,
    n_post: int = 20,
) -> list[tuple[dict, str | None]]:
    """A prompt change makes every response abruptly, sustainedly pricier:
    `n_pre` calls oscillate around a stable `output_tokens` baseline, then
    `n_post` calls step up ~35% and stay there -- the incident `CusumDetector`
    (0.8.2) is meant to catch even though no single post-shift call's own
    z-score need cross the baseline detector's threshold.
    """
    rng = random.Random(seed)
    label, model = "chat", "gpt-4o"
    base = datetime(2026, 4, 6, tzinfo=timezone.utc)
    results: list[tuple[dict, str | None]] = []

    for i in range(n_pre):
        ts = base + timedelta(minutes=i)
        input_tokens = max(1, int(rng.gauss(800, 50)))
        output_tokens = max(1, int(rng.gauss(100, 8)))
        record = _make_record(
            label=label, model=model, timestamp=ts,
            input_tokens=input_tokens, output_tokens=output_tokens,
        )
        results.append((record, None))

    for i in range(n_post):
        ts = base + timedelta(minutes=n_pre + i)
        input_tokens = max(1, int(rng.gauss(800, 50)))
        output_tokens = max(1, int(rng.gauss(135, 10)))
        record = _make_record(
            label=label, model=model, timestamp=ts,
            input_tokens=input_tokens, output_tokens=output_tokens,
        )
        results.append((record, "prompt_regression"))

    return results


def gradual_drift(
    seed: int = _SCENARIO_SEEDS["gradual_drift"],
    *,
    n_pre: int = 25,
    n_ramp: int = 30,
) -> list[tuple[dict, str | None]]:
    """A slow, creeping level shift rather than `prompt_regression`'s abrupt
    step: `n_pre` calls at a stable baseline, then `n_ramp` calls whose mean
    `output_tokens` rises smoothly call-by-call -- exercising `CusumDetector`
    (0.8.2) against a gradual rather than sudden shift.
    """
    rng = random.Random(seed)
    label, model = "chat", "gpt-4o"
    base = datetime(2026, 5, 4, tzinfo=timezone.utc)
    results: list[tuple[dict, str | None]] = []

    for i in range(n_pre):
        ts = base + timedelta(minutes=i)
        input_tokens = max(1, int(rng.gauss(800, 50)))
        output_tokens = max(1, int(rng.gauss(100, 8)))
        record = _make_record(
            label=label, model=model, timestamp=ts,
            input_tokens=input_tokens, output_tokens=output_tokens,
        )
        results.append((record, None))

    for i in range(n_ramp):
        ts = base + timedelta(minutes=n_pre + i)
        mean = 100 + 80 * (i + 1) / n_ramp
        input_tokens = max(1, int(rng.gauss(800, 50)))
        output_tokens = max(1, int(rng.gauss(mean, 8)))
        record = _make_record(
            label=label, model=model, timestamp=ts,
            input_tokens=input_tokens, output_tokens=output_tokens,
        )
        results.append((record, "gradual_drift"))

    return results


def weekend_pattern(
    seed: int = _SCENARIO_SEEDS["weekend_pattern"],
    *,
    n_weeks: int = 6,
    quiet_calls_per_slot: int = 2,
    burst_windows: int = 10,
    burst_calls_per_window: int = 12,
    last_week_multiplier: float = 1.0,
) -> list[tuple[dict, str | None]]:
    """A legitimate, recurring Saturday-morning batch job: routine light
    weekday/weekend traffic plus a heavier recurring burst every Saturday at
    10:00 UTC. With enough weeks of history this is exactly the pattern
    `FrequencyDetector`'s seasonal (day-of-week x hour-of-day) bucketing
    (0.8.4) is meant to learn as normal and stop flagging -- while
    `last_week_multiplier != 1.0` (an unusually large burst even for its own
    slot) or too few weeks of history (no seasonal coverage yet) should
    still be caught.
    """
    rng = random.Random(seed)
    label, model = "chat", "claude-sonnet-4"
    # 2026-01-05 is a Monday.
    anchor_monday = datetime(2026, 1, 5, tzinfo=timezone.utc)
    results: list[tuple[dict, str | None]] = []

    for week in range(n_weeks):
        week_monday = anchor_monday + timedelta(weeks=week)
        burst_start = week_monday + timedelta(days=5, hours=10)  # Saturday 10:00
        burst_end = burst_start + timedelta(seconds=burst_windows * FREQUENCY_WINDOW_SECONDS)

        # Routine quiet traffic every half hour across the week, skipping
        # the burst's own window so it isn't double-counted.
        for half_hour in range(48 * 7):
            ts = week_monday + timedelta(minutes=30 * half_hour)
            if burst_start <= ts < burst_end:
                continue
            for s in range(quiet_calls_per_slot):
                input_tokens = max(1, int(rng.gauss(800, 100)))
                output_tokens = max(1, int(rng.gauss(150, 30)))
                record = _make_record(
                    label=label, model=model, timestamp=ts + timedelta(seconds=s),
                    input_tokens=input_tokens, output_tokens=output_tokens,
                )
                results.append((record, None))

        multiplier = last_week_multiplier if week == n_weeks - 1 else 1.0
        calls_this_week = max(1, int(round(burst_calls_per_window * multiplier)))
        for w in range(burst_windows):
            window_start = burst_start + timedelta(seconds=w * FREQUENCY_WINDOW_SECONDS)
            for c in range(calls_this_week):
                ts = window_start + timedelta(seconds=c * (FREQUENCY_WINDOW_SECONDS / (calls_this_week + 1)))
                input_tokens = max(1, int(rng.gauss(800, 100)))
                output_tokens = max(1, int(rng.gauss(150, 30)))
                record = _make_record(
                    label=label, model=model, timestamp=ts,
                    input_tokens=input_tokens, output_tokens=output_tokens,
                )
                results.append((record, "weekend_pattern"))

    return results
