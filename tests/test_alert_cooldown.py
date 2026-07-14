"""Unit tests for `alert_cooldown.py` -- the pure cooldown/aggregation core
used by `cli._run_detect_follow` to stop an alert storm from re-hitting
every sink on every poll. Exercises `apply_cooldown`/`aggregate_rules_alerts`/
`extract_magnitude`/`cooldown_key` directly, with no `--follow` loop or log
file involved.
"""

from __future__ import annotations

from llm_burnwatch.alert_cooldown import (
    ESCALATION_FACTOR,
    aggregate_rules_alerts,
    apply_cooldown,
    cooldown_key,
    extract_magnitude,
)
from llm_burnwatch.detectors.protocol import Alert


def _alert(detector="baseline", kind="zscore_outlier", group_key=("chat", "gpt-4o"), z=3.0, **kw):
    evidence = kw.pop("evidence", None)
    if evidence is None:
        evidence = {"scores": [{"feature": "output_tokens", "z_score": z}]}
    return Alert(
        detector=detector,
        severity="warning",
        kind=kind,
        group_key=group_key,
        record_ref=kw.pop("record_ref", 0),
        evidence=evidence,
        message=kw.pop("message", "anomalous call"),
    )


# --- cooldown_key ------------------------------------------------------------


def test_cooldown_key_differs_by_kind_for_the_same_detector_and_group_key():
    pace = _alert(detector="budget", kind="budget_pace_warning", group_key=("budget", "2026-01"))
    exceeded = _alert(detector="budget", kind="budget_exceeded", group_key=("budget", "2026-01"))
    assert cooldown_key(pace) != cooldown_key(exceeded)


def test_cooldown_key_is_stable_for_equivalent_alerts():
    a = _alert()
    b = _alert()
    assert cooldown_key(a) == cooldown_key(b)


# --- extract_magnitude --------------------------------------------------------


def test_extract_magnitude_baseline_is_max_abs_z_score_across_features():
    a = _alert(
        evidence={
            "scores": [
                {"feature": "output_tokens", "z_score": -2.0},
                {"feature": "cost_micros", "z_score": 4.5},
            ]
        }
    )
    assert extract_magnitude(a) == 4.5


def test_extract_magnitude_frequency_uses_z_when_present():
    a = _alert(detector="frequency", kind="frequency_spike", evidence={"z": 3.2, "window_calls": 40})
    assert extract_magnitude(a) == 3.2


def test_extract_magnitude_frequency_falls_back_to_window_calls_when_z_is_none():
    a = _alert(
        detector="frequency",
        kind="frequency_spike",
        evidence={"z": None, "window_calls": 105},
    )
    assert extract_magnitude(a) == 105.0


def test_extract_magnitude_cusum_is_cusum_value():
    a = _alert(detector="cusum", kind="level_shift", evidence={"cusum_value": 12.5})
    assert extract_magnitude(a) == 12.5


def test_extract_magnitude_budget_exceeded_is_month_to_date_usd():
    a = _alert(detector="budget", kind="budget_exceeded", evidence={"month_to_date_usd": 150.0})
    assert extract_magnitude(a) == 150.0


def test_extract_magnitude_budget_pace_warning_is_forecast_usd():
    a = _alert(detector="budget", kind="budget_pace_warning", evidence={"forecast_usd": 90.0})
    assert extract_magnitude(a) == 90.0


def test_extract_magnitude_is_none_for_unmapped_detector_kind():
    a = _alert(detector="baseline", kind="insufficient_data", evidence={})
    assert extract_magnitude(a) is None


# --- apply_cooldown -----------------------------------------------------------


def test_apply_cooldown_sends_a_brand_new_key_immediately():
    a = _alert()
    to_send, state = apply_cooldown([a], [a], {}, poll_seq=1, cooldown_minutes=15.0, now=1000.0)
    assert to_send == [a]
    assert state[cooldown_key(a)]["last_sent_at"] == 1000.0


def test_apply_cooldown_suppresses_a_continuing_incident_within_the_window():
    a1 = _alert()
    to_send1, state = apply_cooldown([a1], [a1], {}, poll_seq=1, cooldown_minutes=15.0, now=1000.0)
    assert to_send1 == [a1]

    a2 = _alert()  # same key, still triggering on the very next counted poll
    to_send2, state = apply_cooldown([a2], [a2], state, poll_seq=2, cooldown_minutes=15.0, now=1010.0)
    assert to_send2 == []
    entry = state[cooldown_key(a2)]
    assert entry["suppressed_count"] == 1
    assert entry["suppressed_since"] == 1010.0


def test_apply_cooldown_sends_again_once_the_window_has_elapsed():
    a1 = _alert()
    _, state = apply_cooldown([a1], [a1], {}, poll_seq=1, cooldown_minutes=15.0, now=1000.0)

    a2 = _alert()
    to_send, state = apply_cooldown(
        [a2], [a2], state, poll_seq=2, cooldown_minutes=15.0, now=1000.0 + 15 * 60
    )
    assert to_send == [a2]
    assert state[cooldown_key(a2)]["suppressed_count"] == 0


def test_apply_cooldown_a_gap_in_continuity_resets_and_sends_immediately():
    a1 = _alert()
    _, state = apply_cooldown([a1], [a1], {}, poll_seq=1, cooldown_minutes=15.0, now=1000.0)

    # Poll 2: this key doesn't trigger at all (gap) -- not passed to
    # apply_cooldown this round since it's absent from both new_alerts and
    # all_alerts.
    _, state = apply_cooldown([], [], state, poll_seq=2, cooldown_minutes=15.0, now=1005.0)

    # Poll 3: the same key reappears -- even though only ~5s elapsed (well
    # inside the 15-minute window), the gap at poll 2 means this is treated
    # as a fresh incident and sent right away.
    a3 = _alert()
    to_send, state = apply_cooldown([a3], [a3], state, poll_seq=3, cooldown_minutes=15.0, now=1010.0)
    assert to_send == [a3]


def test_apply_cooldown_escalation_bypasses_the_cooldown_window():
    a1 = _alert(evidence={"scores": [{"feature": "output_tokens", "z_score": 3.0}]})
    _, state = apply_cooldown([a1], [a1], {}, poll_seq=1, cooldown_minutes=15.0, now=1000.0)

    # Well within the cooldown window, but magnitude has more than doubled.
    a2 = _alert(
        evidence={"feature": "output_tokens", "scores": [{"feature": "output_tokens", "z_score": 7.0}]}
    )
    to_send, state = apply_cooldown([a2], [a2], state, poll_seq=2, cooldown_minutes=15.0, now=1010.0)
    assert to_send == [a2]
    assert state[cooldown_key(a2)]["last_magnitude"] == 7.0


def test_apply_cooldown_escalation_requires_at_least_the_full_factor():
    a1 = _alert(evidence={"scores": [{"feature": "output_tokens", "z_score": 3.0}]})
    _, state = apply_cooldown([a1], [a1], {}, poll_seq=1, cooldown_minutes=15.0, now=1000.0)

    just_under = ESCALATION_FACTOR * 3.0 - 0.01
    a2 = _alert(evidence={"scores": [{"feature": "output_tokens", "z_score": just_under}]})
    to_send, _ = apply_cooldown([a2], [a2], state, poll_seq=2, cooldown_minutes=15.0, now=1010.0)
    assert to_send == []


def test_apply_cooldown_suppressed_count_and_since_appear_in_the_next_real_message():
    a1 = _alert()
    _, state = apply_cooldown([a1], [a1], {}, poll_seq=1, cooldown_minutes=15.0, now=1000.0)

    a2 = _alert()
    _, state = apply_cooldown([a2], [a2], state, poll_seq=2, cooldown_minutes=15.0, now=1010.0)
    a3 = _alert()
    _, state = apply_cooldown([a3], [a3], state, poll_seq=3, cooldown_minutes=15.0, now=1020.0)

    a4 = _alert()
    to_send, state = apply_cooldown(
        [a4], [a4], state, poll_seq=4, cooldown_minutes=15.0, now=1000.0 + 15 * 60
    )
    assert len(to_send) == 1
    assert "2 similar alert(s) suppressed since 1010.0" in to_send[0].message


def test_apply_cooldown_zero_minutes_disables_cooldown_entirely():
    a1 = _alert()
    _, state = apply_cooldown([a1], [a1], {}, poll_seq=1, cooldown_minutes=0.0, now=1000.0)

    a2 = _alert()
    to_send, _ = apply_cooldown([a2], [a2], state, poll_seq=2, cooldown_minutes=0.0, now=1000.001)
    assert to_send == [a2]


def test_apply_cooldown_tracks_continuity_via_all_alerts_even_when_not_new():
    a1 = _alert()
    _, state = apply_cooldown([a1], [a1], {}, poll_seq=1, cooldown_minutes=15.0, now=1000.0)

    # Poll 2: the key is still triggering (present in all_alerts, e.g. on an
    # old record), but _detect_follow_poll's own record_ref filter excluded
    # it from new_alerts -- continuity must still be tracked.
    still_triggering = _alert()
    _, state = apply_cooldown([], [still_triggering], state, poll_seq=2, cooldown_minutes=15.0, now=1005.0)

    # Poll 3: it becomes "new" again (e.g. a fresh record triggers it) --
    # since there was no gap, this should be a continuation and suppressed.
    a3 = _alert()
    to_send, _ = apply_cooldown([a3], [a3], state, poll_seq=3, cooldown_minutes=15.0, now=1006.0)
    assert to_send == []


# --- aggregate_rules_alerts ----------------------------------------------------


def _rules_alert(kind="call_cost_exceeded", group_key=("chat", "gpt-4o"), cost=None, model=None):
    if kind == "model_not_allowed":
        evidence = {"model": model or "gpt-5", "allowed_models": ["gpt-4o"]}
    else:
        evidence = {"call_cost_usd": cost if cost is not None else 1.0, "max_call_cost_usd": 0.5}
    return Alert(
        detector="rules",
        severity="critical",
        kind=kind,
        group_key=group_key,
        record_ref=0,
        evidence=evidence,
        message="rule violated",
    )


def test_aggregate_rules_alerts_sends_the_first_violation_immediately():
    a = _rules_alert()
    to_send, state = aggregate_rules_alerts([a], {}, window_minutes=15.0, flush_seconds=60.0, now=1000.0)
    assert to_send == [a]
    assert state[cooldown_key(a)]["pending_count"] == 0


def test_aggregate_rules_alerts_accumulates_subsequent_violations_without_sending():
    a1 = _rules_alert(cost=1.0)
    _, state = aggregate_rules_alerts([a1], {}, window_minutes=15.0, flush_seconds=60.0, now=1000.0)

    a2 = _rules_alert(cost=2.0)
    to_send, state = aggregate_rules_alerts(
        [a2], state, window_minutes=15.0, flush_seconds=60.0, now=1010.0
    )
    assert to_send == []
    entry = state[cooldown_key(a2)]
    assert entry["pending_count"] == 1
    assert entry["pending_total_cost_usd"] == 2.0


def test_aggregate_rules_alerts_flushes_a_summary_after_flush_seconds():
    a1 = _rules_alert(cost=1.0)
    _, state = aggregate_rules_alerts([a1], {}, window_minutes=15.0, flush_seconds=60.0, now=1000.0)

    a2 = _rules_alert(cost=2.0)
    _, state = aggregate_rules_alerts([a2], state, window_minutes=15.0, flush_seconds=60.0, now=1010.0)

    # No new violation this round, but flush_seconds have elapsed since the
    # last flush -- the pending one must still go out.
    to_send, state = aggregate_rules_alerts([], state, window_minutes=15.0, flush_seconds=60.0, now=1061.0)
    assert len(to_send) == 1
    summary = to_send[0]
    assert summary.evidence["count"] == 1
    assert summary.evidence["total_cost_usd"] == 2.0
    assert "total $2.00" in summary.message


def test_aggregate_rules_alerts_reopens_a_new_series_after_the_window_expires():
    a1 = _rules_alert()
    _, state = aggregate_rules_alerts([a1], {}, window_minutes=15.0, flush_seconds=60.0, now=1000.0)

    a2 = _rules_alert()
    to_send, state = aggregate_rules_alerts(
        [a2], state, window_minutes=15.0, flush_seconds=60.0, now=1000.0 + 15 * 60
    )
    # Window expired -- this is a brand new series, sent immediately again
    # (not folded silently into a summary that would delay it).
    assert to_send == [a2]


def test_aggregate_rules_alerts_model_not_allowed_summary_has_no_cost_field():
    a1 = _rules_alert(kind="model_not_allowed", group_key=("chat",), model="gpt-5")
    _, state = aggregate_rules_alerts([a1], {}, window_minutes=15.0, flush_seconds=60.0, now=1000.0)

    a2 = _rules_alert(kind="model_not_allowed", group_key=("chat",), model="gpt-5")
    _, state = aggregate_rules_alerts([a2], state, window_minutes=15.0, flush_seconds=60.0, now=1010.0)

    to_send, _ = aggregate_rules_alerts([], state, window_minutes=15.0, flush_seconds=60.0, now=1061.0)
    assert len(to_send) == 1
    summary = to_send[0]
    assert summary.evidence["total_cost_usd"] is None
    assert "total $" not in summary.message
