"""Cooldown/deduplication for alert storms in `detect --follow`.

`detect --follow` re-runs every detector over the whole rolling window on
each poll (see `cli._detect_follow_poll`). Its own filter --
`record_ref >= new_start_index` -- stops the *same past record* from being
reported twice, but does nothing to stop a *still-ongoing* condition (the
same `(detector, kind, group_key)` triple) from re-triggering, and therefore
being re-sent to every configured sink, on every single poll for as long as
it keeps holding: a runaway agent looping for a few seconds can produce a
handful of alerts and just as many webhook/Slack/Telegram/exec deliveries
for what is, semantically, one incident.

This module is the pure, side-effect-free core of the fix: given the alerts
a poll produced and some small bit of persisted state, decide which alerts
are actually worth telling a human/sink about right now. It performs no I/O
itself -- `cli._run_detect_follow` is the only caller, and it owns reading/
persisting the state dict via `follow_state.py`.

Two distinct policies live here, because "alert repeats" means something
different for statistical detectors than it does for `rules`:

- `apply_cooldown` -- for baseline/frequency/cusum/budget: a per-key cooldown
  window, but only while the *same* incident continues (see "continuity"
  below), with a magnitude-based escalation override so a cooldown window
  can't hide a rapidly worsening situation.
- `aggregate_rules_alerts` -- for `rules`: these are money/policy violations
  the user explicitly configured (`--max-call-cost`, `--max-trace-cost`,
  `--allowed-models`), always `severity="critical"` -- going quiet on them
  for a full cooldown window is not acceptable. Instead, violations are
  aggregated into a periodic summary (first one sent immediately, the rest
  folded into a flush at most once a minute) so nothing is silently lost.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from .detectors.protocol import Alert

# If the magnitude of a still-cooling-down alert has grown by at least this
# factor relative to the last alert actually *sent* for the same key, send
# again immediately rather than wait out the rest of the cooldown window --
# a fixed factor, not a flag, per the scope this was asked for.
ESCALATION_FACTOR = 2.0

# `rules` violations are aggregated (not cooldown-suppressed) into a summary
# sent at most this often while a violation series is ongoing.
RULES_AGGREGATION_FLUSH_SECONDS = 60.0


def cooldown_key(alert: Alert) -> str:
    """A stable string key for `(detector, kind, group_key)`.

    Deliberately *not* `(detector, group_key)` alone: `budget` reports two
    different `kind`s (`budget_pace_warning`, then `budget_exceeded`) for the
    same `group_key` as a month gets worse, and the transition between them
    must not be suppressed by whatever cooldown the first `kind` started --
    keying on the full triple means that transition is simply a new key with
    no cooldown history yet, with no special-case code required.
    """
    return json.dumps([alert.detector, alert.kind, list(alert.group_key)], sort_keys=True)


def _baseline_magnitude(alert: Alert) -> float | None:
    scores = alert.evidence.get("scores")
    if not scores:
        return None
    return max(abs(s["z_score"]) for s in scores)


def _frequency_magnitude(alert: Alert) -> float | None:
    z = alert.evidence.get("z")
    if z is not None:
        return abs(z)
    window_calls = alert.evidence.get("window_calls")
    return float(window_calls) if window_calls is not None else None


def _cusum_magnitude(alert: Alert) -> float | None:
    return alert.evidence.get("cusum_value")


def _budget_exceeded_magnitude(alert: Alert) -> float | None:
    return alert.evidence.get("month_to_date_usd")


def _budget_pace_magnitude(alert: Alert) -> float | None:
    return alert.evidence.get("forecast_usd")


# (detector, kind) -> function extracting a magnitude from that alert's
# evidence. A pair not present here simply never escalates (cooldown falls
# back to the plain timer) -- forward-compatible with future detectors/kinds
# without needing to touch this table.
_MAGNITUDE_EXTRACTORS: dict[tuple[str, str], Callable[[Alert], float | None]] = {
    ("baseline", "zscore_outlier"): _baseline_magnitude,
    ("frequency", "frequency_spike"): _frequency_magnitude,
    ("cusum", "level_shift"): _cusum_magnitude,
    ("budget", "budget_exceeded"): _budget_exceeded_magnitude,
    ("budget", "budget_pace_warning"): _budget_pace_magnitude,
}


def extract_magnitude(alert: Alert) -> float | None:
    """The alert's severity-of-condition as a single number, for escalation
    comparisons -- or `None` if `(alert.detector, alert.kind)` has no known
    magnitude (e.g. `baseline`'s `insufficient_data`, or anything not in
    `_MAGNITUDE_EXTRACTORS`), in which case escalation never applies and
    cooldown falls back to a plain timer.
    """
    extractor = _MAGNITUDE_EXTRACTORS.get((alert.detector, alert.kind))
    if extractor is None:
        return None
    return extractor(alert)


def apply_cooldown(
    new_alerts: list[Alert],
    all_alerts: list[Alert],
    cooldown_state: dict[str, dict[str, Any]],
    poll_seq: int,
    cooldown_minutes: float,
    now: float,
) -> tuple[list[Alert], dict[str, dict[str, Any]]]:
    """Filter `new_alerts` down to the ones actually worth sending, updating
    and returning `cooldown_state` (a plain JSON-able dict, persisted by the
    caller across polls/restarts via `follow_state.py`).

    `new_alerts` are the alerts this poll should consider sending (already
    restricted to `record_ref >= new_start_index` by `_detect_follow_poll`).
    `all_alerts` is the *unfiltered* set of alerts this same poll produced --
    needed only so a key that's still triggering on old records (therefore
    absent from `new_alerts`) is still recognized as "continuing" rather
    than "gone quiet", so a later reappearance in `new_alerts` isn't
    mistaken for a fresh incident. `poll_seq` is a counter the caller
    increments once per poll that actually had new records; passing `0` for
    `cooldown_minutes` disables cooldown entirely (every alert is sent).

    A key is treated as a *continuation* of the same incident only if it was
    also present (in `all_alerts`) on the immediately preceding counted poll
    (`last_poll_seq == poll_seq - 1`); any gap means the next occurrence is a
    fresh incident, sent immediately regardless of how much cooldown time
    has "elapsed" on stale state. A continuing incident is re-sent once
    `cooldown_minutes` have passed since the last actual send, or immediately
    if its magnitude has grown by at least `ESCALATION_FACTOR` relative to
    the last *sent* alert's magnitude (not the last *seen* one, so a slow
    climb can't creep past the threshold unnoticed).
    """
    cooldown_state = dict(cooldown_state)
    to_send: list[Alert] = []

    for alert in new_alerts:
        key = cooldown_key(alert)
        entry = cooldown_state.get(key)
        magnitude = extract_magnitude(alert)

        # Continuous only if this key was also seen (in all_alerts, whether
        # sent or suppressed) on the immediately preceding counted poll --
        # compared against `last_poll_seq` as it stood *before* this poll,
        # so any gap resets it to a fresh incident rather than a continuation.
        is_continuation = entry is not None and entry.get("last_poll_seq") == poll_seq - 1

        if not is_continuation:
            to_send.append(alert)
            cooldown_state[key] = {
                "last_sent_at": now,
                "last_poll_seq": poll_seq,
                "last_magnitude": magnitude,
                "suppressed_count": 0,
                "suppressed_since": None,
            }
            continue

        # `is_continuation` being true already implies `entry is not None`
        # (see its definition above) -- this assert just lets mypy narrow
        # `entry`'s type accordingly for the rest of this branch.
        assert entry is not None
        elapsed = now - entry["last_sent_at"]
        last_magnitude = entry.get("last_magnitude")
        escalated = (
            magnitude is not None
            and last_magnitude is not None
            and magnitude >= ESCALATION_FACTOR * last_magnitude
        )

        if elapsed >= cooldown_minutes * 60 or escalated:
            if entry.get("suppressed_count"):
                alert.message = (
                    f"{alert.message} ({entry['suppressed_count']} similar alert(s) "
                    f"suppressed since {entry['suppressed_since']})"
                ).strip()
            to_send.append(alert)
            entry["last_sent_at"] = now
            entry["last_magnitude"] = magnitude
            entry["suppressed_count"] = 0
            entry["suppressed_since"] = None
        else:
            entry["suppressed_count"] = entry.get("suppressed_count", 0) + 1
            if entry.get("suppressed_since") is None:
                entry["suppressed_since"] = now

        entry["last_poll_seq"] = poll_seq
        cooldown_state[key] = entry

    # Keys that are still triggering (present in all_alerts) but weren't
    # "new" this poll per `_detect_follow_poll`'s own record_ref filter --
    # their continuity marker still needs to move forward, or a later
    # reappearance in `new_alerts` would wrongly look like a gap.
    new_alert_keys = {cooldown_key(a) for a in new_alerts}
    for alert in all_alerts:
        key = cooldown_key(alert)
        if key in new_alert_keys:
            continue
        entry = cooldown_state.get(key)
        if entry is not None:
            entry["last_poll_seq"] = poll_seq
            cooldown_state[key] = entry

    return to_send, cooldown_state


def aggregate_rules_alerts(
    rules_alerts: list[Alert],
    aggregation_state: dict[str, dict[str, Any]],
    window_minutes: float,
    flush_seconds: float,
    now: float,
) -> tuple[list[Alert], dict[str, dict[str, Any]]]:
    """Aggregate `rules` violations instead of cooldown-suppressing them:
    the first violation of a new `(kind, group_key)` series is returned
    as-is immediately (opening an aggregation window); later violations
    within the same window are folded into a summary `Alert`, emitted at
    most once every `flush_seconds`. `window_minutes` bounds how long a
    series stays open before the next violation starts a brand new one
    (mirrors `apply_cooldown`'s window, but here it governs aggregation
    rather than suppression -- nothing from `rules` is ever fully dropped).

    Cost is summed into the summary from `evidence["call_cost_usd"]`/
    `evidence["trace_cost_usd"]` when present; `model_not_allowed` has no
    cost field, so its summary reports only a count.
    """
    aggregation_state = dict(aggregation_state)
    to_send: list[Alert] = []

    for alert in rules_alerts:
        key = cooldown_key(alert)
        entry = aggregation_state.get(key)
        cost = alert.evidence.get("call_cost_usd", alert.evidence.get("trace_cost_usd"))

        if entry is None or now - entry["window_start"] >= window_minutes * 60:
            to_send.append(alert)
            aggregation_state[key] = {
                "detector": alert.detector,
                "kind": alert.kind,
                "group_key": list(alert.group_key),
                "window_start": now,
                "last_flush_at": now,
                "pending_count": 0,
                "pending_total_cost_usd": 0.0,
                "has_cost": cost is not None,
            }
            continue

        entry["pending_count"] += 1
        if cost is not None:
            entry["pending_total_cost_usd"] += cost
            entry["has_cost"] = True
        aggregation_state[key] = entry

    # Flush pass over *every* open series (not just ones with a new alert
    # this poll) so a series that stops producing new violations still gets
    # its last pending summary out instead of holding it forever, and so a
    # fully expired window is closed even with nothing pending to flush.
    for key, entry in list(aggregation_state.items()):
        window_expired = now - entry["window_start"] >= window_minutes * 60
        due_for_flush = now - entry["last_flush_at"] >= flush_seconds
        if entry["pending_count"] and (window_expired or due_for_flush):
            cost_note = (
                f": total ${entry['pending_total_cost_usd']:.2f}" if entry["has_cost"] else ""
            )
            to_send.append(
                Alert(
                    detector=entry["detector"],
                    severity="critical",
                    kind=entry["kind"],
                    group_key=tuple(entry["group_key"]),
                    record_ref=None,
                    evidence={
                        "count": entry["pending_count"],
                        "total_cost_usd": entry["pending_total_cost_usd"] if entry["has_cost"] else None,
                    },
                    message=(
                        f"{entry['pending_count']} more violation(s) of {entry['kind']} "
                        f"for {tuple(entry['group_key'])} in the last "
                        f"~{flush_seconds:.0f}s{cost_note}"
                    ),
                )
            )
            entry["pending_count"] = 0
            entry["pending_total_cost_usd"] = 0.0
            entry["last_flush_at"] = now

        if window_expired:
            del aggregation_state[key]

    return to_send, aggregation_state
