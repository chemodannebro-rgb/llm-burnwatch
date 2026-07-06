"""Internal sanity check for both anomaly detectors (baseline and ML)
against a deterministic synthetic log with a known number of injected
anomalies (`demo_data.py`, fixed seed).

This is intentionally not a public `evaluate` command: recall/precision
can only be computed here because we know the ground truth for our own
synthetic data. On a real customer log there is no ground truth, so this
kind of metric would be meaningless/dishonest as a user-facing feature.
Here it only exists to catch a regression that silently breaks detection.
"""

from __future__ import annotations

import pytest

from llm_burnwatch.anomaly.baseline import analyze
from llm_burnwatch.demo_data import (
    gradual_drift,
    model_swap,
    prompt_regression,
    runaway_loop,
    weekend_pattern,
    write_demo_log,
)
from llm_burnwatch.detectors.cusum_detector import CusumDetector
from llm_burnwatch.detectors.engine import run_detectors
from llm_burnwatch.detectors.frequency_detector import FrequencyDetector
from llm_burnwatch.detectors.rules_detector import RulesDetector

N_ANOMALIES = 10
N_NORMAL = 200


def _generate(tmp_path):
    log_path = tmp_path / "demo.jsonl"
    results = write_demo_log(log_path, n_normal=N_NORMAL, n_anomalies=N_ANOMALIES)
    records = [r for r, _ in results]
    is_anomaly = [flag for _, flag in results]
    return records, is_anomaly


def test_baseline_detector_finds_all_injected_anomalies_with_few_false_positives(
    tmp_path,
):
    records, is_anomaly = _generate(tmp_path)
    analyses = analyze(records)

    injected_idx = {i for i, flag in enumerate(is_anomaly) if flag}
    detected_idx = {i for i, a in enumerate(analyses) if a.status == "anomaly"}

    true_positives = injected_idx & detected_idx
    false_negatives = injected_idx - detected_idx
    false_positives = detected_idx - injected_idx

    assert not false_negatives, "baseline detector missed an injected anomaly"
    assert len(true_positives) == N_ANOMALIES
    # A handful of false positives on 200 normal calls is expected for a
    # z-score threshold tuned for statistical soundness, not for a
    # zero-false-positive demo; this bounds it isn't unreasonably noisy.
    assert len(false_positives) <= 5


def test_ml_detector_finds_all_injected_anomalies(tmp_path):
    pytest.importorskip("sklearn")

    from llm_burnwatch.anomaly.features import extract_features
    from llm_burnwatch.anomaly.registry import load_model
    from llm_burnwatch.anomaly.train import train

    records, is_anomaly = _generate(tmp_path)
    version_dir, _eval_metrics = train(records, model_dir=tmp_path / "models")
    model, _metadata = load_model(version_dir)

    X, kept_indices = extract_features(records)
    predictions = model.predict(X)  # -1 == anomaly, 1 == normal

    injected_idx = {i for i, flag in enumerate(is_anomaly) if flag}
    detected_idx = {
        kept_indices[i] for i, pred in enumerate(predictions) if pred == -1
    }

    false_negatives = injected_idx - detected_idx
    assert not false_negatives, "ML detector missed an injected anomaly"


# --- Per-scenario recall/precision against each scenario's own detector ----
#
# Unlike the two tests above (single amplitude-outlier profile, one line per
# call), these exercise the independent scenario generators added in 0.8.7,
# each paired with the specific detector it's designed to be caught by.


def test_runaway_loop_scenario_is_flagged_by_frequency_detector():
    results = runaway_loop()
    records = [r for r, _ in results]
    injected_idx = {i for i, (_, label) in enumerate(results) if label == "runaway_loop"}

    alerts = FrequencyDetector().analyze(records)
    detected_windows = {a.record_ref for a in alerts}

    assert detected_windows, "frequency detector missed the runaway burst entirely"
    assert detected_windows <= injected_idx, "frequency detector flagged a normal window"
    # One alert per burst window (a burst can also trip the global,
    # cross-label check, but with a single label/model group that alert's
    # record_ref coincides with the per-group one, so the deduplicated set
    # should still equal the number of burst windows).
    assert len(detected_windows) == 3


def test_runaway_loop_history_alone_is_never_flagged():
    results = runaway_loop()
    records = [r for r, _ in results]
    normal_idx = {i for i, (_, label) in enumerate(results) if label is None}

    alerts = FrequencyDetector().analyze(records)
    detected = {a.record_ref for a in alerts}

    assert detected.isdisjoint(normal_idx)


def test_model_swap_scenario_is_flagged_by_rules_detector():
    results = model_swap()
    records = [r for r, _ in results]
    injected_idx = {i for i, (_, label) in enumerate(results) if label == "model_swap"}

    alerts = RulesDetector(allowed_models=["gpt-4o-mini"]).analyze(records)
    detected_idx = {a.record_ref for a in alerts}

    assert detected_idx == injected_idx
    assert all(a.kind == "model_not_allowed" for a in alerts)


def test_model_swap_scenario_is_silent_without_an_allowed_models_rule():
    results = model_swap()
    records = [r for r, _ in results]

    alerts = RulesDetector().analyze(records)
    assert alerts == []


def test_prompt_regression_scenario_is_flagged_by_cusum_detector():
    n_pre = 30
    results = prompt_regression(n_pre=n_pre)
    records = [r for r, _ in results]
    injected_idx = {i for i, (_, label) in enumerate(results) if label == "prompt_regression"}

    alerts = run_detectors(records, registry=[CusumDetector()], enabled_overrides={"cusum": True})
    output_alerts = [a for a in alerts if a.evidence.get("feature") == "output_tokens"]

    assert output_alerts, "CUSUM missed the abrupt prompt-regression level shift"
    alert = output_alerts[0]
    assert alert.evidence["shift_started_at_record"] == n_pre
    assert alert.record_ref in injected_idx


def test_gradual_drift_scenario_is_flagged_by_cusum_detector():
    results = gradual_drift()
    records = [r for r, _ in results]
    injected_idx = {i for i, (_, label) in enumerate(results) if label == "gradual_drift"}

    alerts = run_detectors(records, registry=[CusumDetector()], enabled_overrides={"cusum": True})
    output_alerts = [a for a in alerts if a.evidence.get("feature") == "output_tokens"]

    assert output_alerts, "CUSUM missed the gradual drift"
    assert output_alerts[0].record_ref in injected_idx


def test_weekend_pattern_scenario_flagged_without_seasonal_coverage():
    # A single week has no seasonal (>= MIN_SEASONAL_SPAN_DAYS) coverage
    # yet, so the recurring Saturday burst is still indistinguishable from
    # a one-off spike and should be flagged.
    results = weekend_pattern(n_weeks=1)
    records = [r for r, _ in results]
    injected_idx = {i for i, (_, label) in enumerate(results) if label == "weekend_pattern"}

    alerts = FrequencyDetector().analyze(records)
    detected = {a.record_ref for a in alerts}

    assert detected, "frequency detector missed the weekend burst with no seasonal history"
    assert detected <= injected_idx


def test_weekend_pattern_scenario_learned_as_normal_with_seasonal_coverage():
    # Once the identical Saturday burst has recurred across enough weeks
    # to give seasonal bucketing real history, it should stop being flagged
    # -- this is the whole point of 0.8.4's seasonal baselines.
    results = weekend_pattern(n_weeks=6)
    records = [r for r, _ in results]

    alerts = FrequencyDetector().analyze(records)
    assert alerts == []


def test_weekend_pattern_scenario_still_flagged_if_abnormal_for_its_own_slot():
    # Even with seasonal coverage, a burst far bigger than every other
    # week's Saturday burst is still a genuine anomaly for that slot.
    results = weekend_pattern(n_weeks=6, last_week_multiplier=5.0)
    records = [r for r, _ in results]
    injected_idx = {i for i, (_, label) in enumerate(results) if label == "weekend_pattern"}

    alerts = FrequencyDetector().analyze(records)
    detected = {a.record_ref for a in alerts}

    assert detected, "frequency detector missed the abnormally large last-week burst"
    assert detected <= injected_idx


def test_clean_normal_log_triggers_no_false_positives_across_all_detectors(tmp_path):
    log_path = tmp_path / "clean.jsonl"
    results = write_demo_log(log_path, n_normal=N_NORMAL, n_anomalies=0)
    records = [r for r, _ in results]

    # FrequencyDetector deliberately excluded here: write_demo_log logs all
    # calls back-to-back at the real "now" (no synthetic spacing), so every
    # call lands in the same 60s window -- a demo-script artifact of
    # `CostTracker.log_call()`'s real-time stamping, not a realistic call
    # rate. Frequency's own false-positive behavior against a realistic,
    # time-spaced history is already covered by
    # `test_runaway_loop_history_alone_is_never_flagged` above.
    registry = [CusumDetector(), RulesDetector()]
    alerts = run_detectors(records, registry=registry)

    # RulesDetector is an unconfigured no-op here, so any alert can only
    # come from CusumDetector. Like the baseline detector's own false-positive
    # allowance below, CUSUM's threshold is tuned for a <1% false-positive
    # rate (see `CUSUM_H_MULTIPLIER`'s docstring), not a zero-false-positive
    # guarantee on random Gaussian noise -- bound it instead of requiring
    # exactly none.
    assert len(alerts) <= 2

    analyses = analyze(records)
    false_positives = [a for a in analyses if a.status == "anomaly"]
    assert len(false_positives) <= 5
