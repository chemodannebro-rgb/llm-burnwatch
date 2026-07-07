from __future__ import annotations

from datetime import datetime, timezone

from llm_burnwatch.detectors.budget_detector import BudgetDetector, compute_budget_status
from llm_burnwatch.detectors.engine import DEFAULT_REGISTRY, run_detectors


def _record(cost_micros=1_000_000, timestamp="2026-07-15T12:00:00Z"):
    return {"cost_micros": cost_micros, "timestamp": timestamp}


_NOW = datetime(2026, 7, 15, tzinfo=timezone.utc)  # 15th of a 31-day month


# --- BudgetDetector: registration / default gating --------------------------


def test_budget_detector_is_registered_in_default_registry():
    names = [d.name for d in DEFAULT_REGISTRY]
    assert "budget" in names


def test_budget_detector_is_disabled_by_default():
    detector = BudgetDetector()
    assert detector.name == "budget"
    assert detector.enabled_by_default is False


def test_budget_detector_stays_silent_without_any_configuration():
    records = [_record(cost_micros=999_999_999)]
    alerts = BudgetDetector(now=_NOW).analyze(records)
    assert alerts == []


def test_budget_detector_stays_silent_when_no_record_falls_in_current_month():
    records = [_record(cost_micros=999_999_999, timestamp="2026-06-15T12:00:00Z")]
    detector = BudgetDetector(monthly_usd=1.0, warn_at_fraction=0.8, now=_NOW)
    assert detector.analyze(records) == []


# --- BudgetDetector: budget_exceeded -----------------------------------------


def test_budget_detector_flags_budget_exceeded():
    records = [
        _record(cost_micros=1_000_000, timestamp="2026-07-01T00:00:00Z"),  # $1.00
        _record(cost_micros=2_000_000, timestamp="2026-07-15T00:00:00Z"),  # $2.00, total $3.00
    ]
    detector = BudgetDetector(monthly_usd=2.0, warn_at_fraction=0.8, now=_NOW)
    alerts = detector.analyze(records)

    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.detector == "budget"
    assert alert.severity == "critical"
    assert alert.kind == "budget_exceeded"
    assert alert.group_key == ("budget", "2026-07")
    assert alert.record_ref == 1  # last in-month record, not the one that crossed the cap
    assert alert.evidence["month_to_date_usd"] == 3.0
    assert alert.evidence["over_budget"] is True
    assert "exceeds monthly budget $2.00" in alert.message


def test_budget_detector_ignores_out_of_month_records_when_summing():
    records = [
        _record(cost_micros=999_999_999, timestamp="2026-06-30T23:59:59Z"),  # last month
        _record(cost_micros=1_000_000, timestamp="2026-07-15T00:00:00Z"),  # $1.00 this month
    ]
    detector = BudgetDetector(monthly_usd=100.0, warn_at_fraction=0.8, now=_NOW)
    alerts = detector.analyze(records)
    assert alerts == []  # $1.00 month-to-date, nowhere near $100 budget or its 80% pace


# --- BudgetDetector: budget_pace_warning -------------------------------------


def test_budget_detector_flags_pace_warning_without_being_over_budget_yet():
    # $50 spent by day 15 of a 31-day month -> forecast = 50/15*31 ~= $103.33,
    # which exceeds 80% of a $100 budget ($80), but $50 itself is not over $100.
    records = [_record(cost_micros=50_000_000, timestamp="2026-07-15T00:00:00Z")]
    detector = BudgetDetector(monthly_usd=100.0, warn_at_fraction=0.8, now=_NOW)
    alerts = detector.analyze(records)

    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.severity == "warning"
    assert alert.kind == "budget_pace_warning"
    assert alert.evidence["over_budget"] is False
    assert alert.evidence["pace_warning"] is True
    assert "projected month-end cost" in alert.message


def test_budget_detector_silent_when_within_budget_and_pace():
    records = [_record(cost_micros=1_000_000, timestamp="2026-07-15T00:00:00Z")]  # $1.00
    detector = BudgetDetector(monthly_usd=100.0, warn_at_fraction=0.8, now=_NOW)
    assert detector.analyze(records) == []


# --- BudgetDetector: low-confidence early-month forecast ---------------------


def test_budget_detector_flags_low_confidence_early_in_month():
    early = datetime(2026, 7, 2, tzinfo=timezone.utc)  # day 2 -- below the 3-day threshold
    # Huge single-day spend so the forecast trivially triggers a pace warning,
    # isolating the low-confidence flag as the thing under test here.
    records = [_record(cost_micros=50_000_000, timestamp="2026-07-02T00:00:00Z")]
    detector = BudgetDetector(monthly_usd=100.0, warn_at_fraction=0.8, now=early)
    alerts = detector.analyze(records)

    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.evidence["low_confidence"] is True
    assert "low confidence" in alert.message


def test_budget_detector_does_not_flag_low_confidence_later_in_month():
    records = [_record(cost_micros=50_000_000, timestamp="2026-07-15T00:00:00Z")]
    detector = BudgetDetector(monthly_usd=100.0, warn_at_fraction=0.8, now=_NOW)
    alerts = detector.analyze(records)
    assert alerts[0].evidence["low_confidence"] is False
    assert "low confidence" not in alerts[0].message


# --- BudgetDetector: via run_detectors (registry integration) ---------------


def test_budget_detector_runs_via_run_detectors_with_enabled_override():
    records = [_record(cost_micros=999_999_999, timestamp="2026-07-15T00:00:00Z")]
    alerts = run_detectors(
        records,
        registry=[BudgetDetector(monthly_usd=1.0, warn_at_fraction=0.8, now=_NOW)],
        enabled_overrides={"budget": True},
    )
    assert len(alerts) == 1
    assert alerts[0].kind == "budget_exceeded"


def test_budget_detector_does_not_run_via_run_detectors_without_enabled_override():
    records = [_record(cost_micros=999_999_999, timestamp="2026-07-15T00:00:00Z")]
    alerts = run_detectors(
        records,
        registry=[BudgetDetector(monthly_usd=1.0, warn_at_fraction=0.8, now=_NOW)],
        enabled_overrides={},
    )
    assert alerts == []


# --- compute_budget_status: pure-function unit tests -------------------------


def test_compute_budget_status_returns_none_without_in_month_records():
    records = [_record(timestamp="2026-06-15T00:00:00Z")]
    assert compute_budget_status(records, 100.0, 0.8, now=_NOW) is None


def test_compute_budget_status_computes_forecast_and_flags():
    records = [
        _record(cost_micros=10_000_000, timestamp="2026-07-01T00:00:00Z"),
        _record(cost_micros=20_000_000, timestamp="2026-07-15T00:00:00Z"),
    ]
    status = compute_budget_status(records, 100.0, 0.5, now=_NOW)

    assert status["month"] == "2026-07"
    assert status["month_to_date_usd"] == 30.0
    assert status["days_elapsed"] == 15
    assert status["days_in_month"] == 31
    assert status["forecast_usd"] == 30.0 / 15 * 31
    assert status["last_record_index"] == 1
    assert status["over_budget"] is False
    assert status["pace_warning"] is True  # forecast ~62 > 50% of 100
    assert status["low_confidence"] is False


def test_compute_budget_status_zero_cost_still_returns_a_status():
    records = [_record(cost_micros=0, timestamp="2026-07-15T00:00:00Z")]
    status = compute_budget_status(records, 100.0, 0.8, now=_NOW)
    assert status is not None
    assert status["month_to_date_usd"] == 0.0
    assert status["over_budget"] is False
    assert status["pace_warning"] is False
