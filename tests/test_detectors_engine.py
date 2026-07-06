from __future__ import annotations

from llm_burnwatch.anomaly.baseline import analyze as baseline_analyze
from llm_burnwatch.detectors.baseline_detector import BaselineDetector
from llm_burnwatch.detectors.engine import run_detectors
from llm_burnwatch.detectors.protocol import Alert


class _StubDetector:
    """Minimal `Detector` used only to exercise the engine's own mechanics
    (registry filtering, enabled/override handling, sorting) independently
    of any real detection logic.
    """

    def __init__(self, name, enabled_by_default, alerts):
        self.name = name
        self.enabled_by_default = enabled_by_default
        self._alerts = alerts
        self.called_with = None

    def analyze(self, records):
        self.called_with = records
        return self._alerts


def _record(label, model, input_tokens, output_tokens, cost_micros):
    return {
        "label": label,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_micros": cost_micros,
    }


def test_run_detectors_calls_every_enabled_detector_and_merges_alerts():
    a1 = Alert(detector="a", severity="warning", kind="k", group_key=(), record_ref=0)
    a2 = Alert(detector="b", severity="warning", kind="k", group_key=(), record_ref=1)
    det_a = _StubDetector("a", True, [a1])
    det_b = _StubDetector("b", True, [a2])

    records = [{"x": 1}]
    result = run_detectors(records, registry=[det_a, det_b])

    assert det_a.called_with is records
    assert det_b.called_with is records
    assert result == [a1, a2]


def test_run_detectors_skips_disabled_by_default_detector():
    off_alert = Alert(detector="off", severity="warning", kind="k", group_key=(), record_ref=0)
    det_off = _StubDetector("off", False, [off_alert])
    det_on = _StubDetector("on", True, [])

    result = run_detectors([{}], registry=[det_off, det_on])

    assert det_off.called_with is None
    assert result == []


def test_run_detectors_enabled_overrides_can_turn_a_detector_on_or_off():
    off_alert = Alert(detector="off", severity="warning", kind="k", group_key=(), record_ref=0)
    on_alert = Alert(detector="on", severity="warning", kind="k", group_key=(), record_ref=0)
    det_off = _StubDetector("off", False, [off_alert])
    det_on = _StubDetector("on", True, [on_alert])

    result = run_detectors(
        [{}],
        registry=[det_off, det_on],
        enabled_overrides={"off": True, "on": False},
    )

    assert result == [off_alert]


def test_run_detectors_sorts_by_record_ref_then_severity():
    critical_later = Alert(
        detector="a", severity="critical", kind="k", group_key=(), record_ref=2
    )
    warning_earlier = Alert(
        detector="a", severity="warning", kind="k", group_key=(), record_ref=1
    )
    info_same_record = Alert(
        detector="a", severity="info", kind="k", group_key=(), record_ref=1
    )
    critical_same_record = Alert(
        detector="a", severity="critical", kind="k", group_key=(), record_ref=1
    )
    unattached = Alert(
        detector="a", severity="warning", kind="k", group_key=(), record_ref=None
    )
    det = _StubDetector(
        "a",
        True,
        [critical_later, unattached, info_same_record, warning_earlier, critical_same_record],
    )

    result = run_detectors([{}], registry=[det])

    assert result == [
        critical_same_record,
        warning_earlier,
        info_same_record,
        critical_later,
        unattached,
    ]


def test_baseline_detector_matches_direct_baseline_analyze_call():
    normal = [
        _record("summarize", "gpt-4o", 800 + i, 150 + i, 2000 + i) for i in range(20)
    ]
    outlier = _record("summarize", "gpt-4o", 50_000, 10_000, 200_000)
    records = normal + [outlier]

    direct = baseline_analyze(records)
    via_engine = run_detectors(records, registry=[BaselineDetector()])

    direct_anomaly_indices = {i for i, a in enumerate(direct) if a.status == "anomaly"}
    engine_anomaly_indices = {
        a.record_ref for a in via_engine if a.kind == "zscore_outlier"
    }
    assert engine_anomaly_indices == direct_anomaly_indices

    outlier_alert = next(a for a in via_engine if a.record_ref == len(records) - 1)
    assert outlier_alert.detector == "baseline"
    assert outlier_alert.severity == "warning"
    assert outlier_alert.group_key == ("summarize", "gpt-4o")
    assert outlier_alert.evidence["scores"]


def test_baseline_detector_emits_insufficient_data_alert():
    # A single (label, model) call with no history at all -- below
    # MIN_GROUP_SAMPLES for both the exact group and the model fallback.
    records = [_record("summarize", "gpt-4o", 100, 10, 100)]

    via_engine = run_detectors(records, registry=[BaselineDetector()])

    assert len(via_engine) == 1
    assert via_engine[0].kind == "insufficient_data"
    assert via_engine[0].severity == "info"
    assert via_engine[0].record_ref == 0


def test_baseline_detector_is_enabled_by_default():
    detector = BaselineDetector()
    assert detector.name == "baseline"
    assert detector.enabled_by_default is True
