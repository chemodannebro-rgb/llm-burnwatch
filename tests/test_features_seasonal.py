from __future__ import annotations

from datetime import datetime, timedelta, timezone

from llm_burnwatch.anomaly.constants import FREQUENCY_WINDOW_SECONDS, MIN_SEASONAL_SPAN_DAYS
from llm_burnwatch.anomaly.seasonal import (
    has_seasonal_coverage,
    log_span_days,
    seasonal_coverage_message,
)
from llm_burnwatch.detectors.engine import run_detectors
from llm_burnwatch.detectors.frequency_detector import FrequencyDetector

BASE = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)  # a Monday, 09:00 UTC


def _record(label, model, ts):
    return {"label": label, "model": model, "timestamp": ts.isoformat()}


def _window_record(w, s=0, label="chat", model="gpt-4o"):
    ts = BASE + timedelta(seconds=w * FREQUENCY_WINDOW_SECONDS + s)
    return _record(label, model, ts)


def test_log_span_days_is_none_with_fewer_than_two_timestamps():
    records = [_record("chat", "gpt-4o", BASE)]
    assert log_span_days(records) is None
    assert log_span_days([]) is None


def test_log_span_days_computes_calendar_range():
    records = [
        _record("chat", "gpt-4o", BASE),
        _record("chat", "gpt-4o", BASE + timedelta(days=5)),
    ]
    assert log_span_days(records) == 5.0


def test_has_seasonal_coverage_false_below_minimum_span():
    records = [
        _record("chat", "gpt-4o", BASE),
        _record("chat", "gpt-4o", BASE + timedelta(days=MIN_SEASONAL_SPAN_DAYS - 1)),
    ]
    assert has_seasonal_coverage(records) is False


def test_has_seasonal_coverage_true_at_minimum_span():
    records = [
        _record("chat", "gpt-4o", BASE),
        _record("chat", "gpt-4o", BASE + timedelta(days=MIN_SEASONAL_SPAN_DAYS)),
    ]
    assert has_seasonal_coverage(records) is True


def test_seasonal_coverage_message_is_honest_about_insufficient_data():
    records = [_record("chat", "gpt-4o", BASE)]
    message = seasonal_coverage_message(records)
    assert "insufficient data" in message

    records = [
        _record("chat", "gpt-4o", BASE),
        _record("chat", "gpt-4o", BASE + timedelta(days=3)),
    ]
    message = seasonal_coverage_message(records)
    assert "insufficient data" in message
    assert "3.0" in message


def test_seasonal_coverage_message_confirms_availability():
    records = [
        _record("chat", "gpt-4o", BASE),
        _record("chat", "gpt-4o", BASE + timedelta(days=MIN_SEASONAL_SPAN_DAYS)),
    ]
    message = seasonal_coverage_message(records)
    assert "seasonal baseline available" in message


def _weekly_burst_records(n_weeks: int, burst_windows: int = 10, quiet_calls_per_window: int = 2):
    """`n_weeks` of data: routine `quiet_calls_per_window`-call windows all
    week, plus a recurring `burst_windows`-window burst (10 calls/window)
    every Monday at 09:00 UTC -- the "every Monday morning" scenario from
    the frequency detector's own docstring.
    """
    records = []
    for week in range(n_weeks):
        week_start = BASE + timedelta(weeks=week)
        # Quiet baseline traffic every 30 minutes, all week round the
        # clock (~336 windows/week) -- comfortably the majority class, so
        # the burst genuinely stands out as a minority pattern rather than
        # accidentally becoming the flat comparison's own "typical" value.
        # The burst's own hour (09:00-09:10 Monday) is skipped here so it
        # never lands in the same 60s window as the burst itself.
        for half_hour in range(48 * 7):
            ts = week_start + timedelta(minutes=30 * half_hour)
            if ts.weekday() == 0 and ts.hour == 9:
                continue
            for s in range(quiet_calls_per_window):
                records.append(_record("chat", "gpt-4o", ts + timedelta(seconds=s)))
        # The Monday-morning burst itself, `burst_windows` consecutive
        # 60s windows, 10 calls each.
        for w in range(burst_windows):
            window_start = week_start + timedelta(seconds=w * FREQUENCY_WINDOW_SECONDS)
            for s in range(10):
                records.append(_record("chat", "gpt-4o", window_start + timedelta(seconds=s)))
    return records


def test_frequency_detector_flags_recurring_burst_when_seasonal_coverage_is_insufficient():
    # Only 1 week of data -- well under MIN_SEASONAL_SPAN_DAYS, so no
    # seasonal bucketing is attempted; the flat comparison (correctly)
    # flags the burst windows as spikes relative to the week's quiet
    # baseline traffic.
    records = _weekly_burst_records(n_weeks=1)
    assert has_seasonal_coverage(records) is False

    alerts = run_detectors(
        records, registry=[FrequencyDetector()], enabled_overrides={"frequency": True}
    )
    burst_alerts = [a for a in alerts if a.group_key == ("chat", "gpt-4o")]
    assert len(burst_alerts) > 0


def test_frequency_detector_does_not_flag_a_routine_monday_burst_once_seasonal():
    # Several weeks of the identical recurring Monday-morning burst -- once
    # the log has enough calendar span, the burst becomes the *expected*
    # pattern for that (weekday, hour) bucket and should stop being flagged.
    records = _weekly_burst_records(n_weeks=4)
    assert has_seasonal_coverage(records) is True

    alerts = run_detectors(
        records, registry=[FrequencyDetector()], enabled_overrides={"frequency": True}
    )
    burst_alerts = [a for a in alerts if a.group_key == ("chat", "gpt-4o")]
    assert burst_alerts == []


def test_frequency_detector_still_flags_a_burst_unusually_large_even_for_monday():
    # Same recurring Monday pattern, but the final week's burst is much
    # larger than every prior Monday's -- seasonal bucketing should still
    # catch a burst that's abnormal even by that time slot's own standards.
    records = _weekly_burst_records(n_weeks=4, burst_windows=10)
    # Inflate the last week's burst windows heavily.
    last_week_start = BASE + timedelta(weeks=3)
    for w in range(10):
        window_start = last_week_start + timedelta(seconds=w * FREQUENCY_WINDOW_SECONDS)
        for s in range(10, 60):
            records.append(_record("chat", "gpt-4o", window_start + timedelta(seconds=s)))
    assert has_seasonal_coverage(records) is True

    alerts = run_detectors(
        records, registry=[FrequencyDetector()], enabled_overrides={"frequency": True}
    )
    burst_alerts = [a for a in alerts if a.group_key == ("chat", "gpt-4o")]
    assert len(burst_alerts) > 0
