from __future__ import annotations

from llm_burnwatch.detectors.engine import DEFAULT_REGISTRY, run_detectors
from llm_burnwatch.detectors.rules_detector import RulesDetector


def _record(label="chat", model="gpt-4o", cost_micros=1000, trace_id=None):
    r = {"label": label, "model": model, "cost_micros": cost_micros}
    if trace_id is not None:
        r["trace_id"] = trace_id
    return r


def test_rules_detector_is_enabled_by_default():
    detector = RulesDetector()
    assert detector.name == "rules"
    assert detector.enabled_by_default is True


def test_rules_detector_is_registered_in_default_registry():
    names = [d.name for d in DEFAULT_REGISTRY]
    assert "rules" in names


def test_rules_detector_stays_silent_without_any_configuration():
    # No allowed_models, no max_call_cost_usd, no max_trace_cost_usd -- the
    # detector must never generate alerts of its own accord.
    records = [
        _record(model="some-expensive-model", cost_micros=999_999_999, trace_id="t1"),
        _record(model="another-model", cost_micros=999_999_999, trace_id="t1"),
    ]
    alerts = RulesDetector().analyze(records)
    assert alerts == []


def test_rules_detector_flags_model_not_in_allowlist():
    records = [
        _record(model="gpt-4o"),
        _record(model="untrusted-model"),
    ]
    detector = RulesDetector(allowed_models=["gpt-4o"])
    alerts = detector.analyze(records)

    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.detector == "rules"
    assert alert.severity == "critical"
    assert alert.kind == "model_not_allowed"
    assert alert.record_ref == 1
    assert alert.evidence["model"] == "untrusted-model"
    assert alert.evidence["allowed_models"] == ["gpt-4o"]


def test_rules_detector_flags_call_cost_exceeded():
    records = [
        _record(cost_micros=1_000_000),  # $1.00
        _record(cost_micros=5_000_000),  # $5.00
    ]
    detector = RulesDetector(max_call_cost_usd=2.0)
    alerts = detector.analyze(records)

    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.kind == "call_cost_exceeded"
    assert alert.severity == "critical"
    assert alert.record_ref == 1
    assert alert.evidence["call_cost_usd"] == 5.0
    assert alert.evidence["max_call_cost_usd"] == 2.0


def test_rules_detector_flags_trace_cost_exceeded():
    records = [
        _record(cost_micros=1_000_000, trace_id="t1"),  # $1.00, running total $1
        _record(cost_micros=1_000_000, trace_id="t1"),  # $1.00, running total $2
        _record(cost_micros=1_000_000, trace_id="t1"),  # $1.00, running total $3 -- crosses $2.5 cap here
    ]
    detector = RulesDetector(max_trace_cost_usd=2.5)
    alerts = detector.analyze(records)

    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.kind == "trace_cost_exceeded"
    assert alert.severity == "critical"
    assert alert.group_key == ("t1",)
    # The 3rd call (index 2) is the one that pushed the running total past
    # the $2.50 cap.
    assert alert.record_ref == 2
    assert alert.evidence["trace_id"] == "t1"
    assert alert.evidence["trace_cost_usd"] == 3.0
    assert alert.evidence["max_trace_cost_usd"] == 2.5


def test_rules_detector_ignores_records_without_a_trace_id_for_trace_rule():
    records = [_record(cost_micros=999_999_999)]  # no trace_id at all
    detector = RulesDetector(max_trace_cost_usd=0.01)
    alerts = detector.analyze(records)
    assert alerts == []


def test_rules_detector_combines_all_configured_rules_via_run_detectors():
    records = [
        _record(model="untrusted-model", cost_micros=10_000_000, trace_id="t1"),
    ]
    alerts = run_detectors(
        records,
        registry=[
            RulesDetector(
                allowed_models=["gpt-4o"],
                max_call_cost_usd=1.0,
                max_trace_cost_usd=1.0,
            )
        ],
        enabled_overrides={"rules": True},
    )
    kinds = {a.kind for a in alerts}
    assert kinds == {"model_not_allowed", "call_cost_exceeded", "trace_cost_exceeded"}
