from __future__ import annotations

import json
from importlib import resources

import pytest

from llm_burnwatch.validation import validate_record


@pytest.fixture
def schema():
    text = resources.files("llm_burnwatch").joinpath("schema.json").read_text(encoding="utf-8")
    return json.loads(text)


def _valid_record(**overrides):
    record = {
        "schema_version": "1.0",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "label": "summarize",
        "model": "gpt-4o",
        "input_tokens": 100,
        "output_tokens": 20,
        "cost_micros": 500,
    }
    record.update(overrides)
    return record


def test_valid_record_has_no_errors(schema):
    assert validate_record(_valid_record(), schema) == []


def test_missing_required_field_is_reported(schema):
    record = _valid_record()
    del record["model"]
    errors = validate_record(record, schema)
    assert any("missing required field 'model'" in e for e in errors)


def test_wrong_type_is_reported(schema):
    record = _valid_record(input_tokens="not-a-number")
    errors = validate_record(record, schema)
    assert any("input_tokens" in e and "expected type" in e for e in errors)


def test_negative_number_below_minimum_is_reported(schema):
    record = _valid_record(cost_micros=-5)
    errors = validate_record(record, schema)
    assert any("below minimum" in e for e in errors)


def test_empty_label_below_min_length_is_reported(schema):
    record = _valid_record(label="")
    errors = validate_record(record, schema)
    assert any("below minLength" in e for e in errors)


def test_unexpected_field_is_reported(schema):
    record = _valid_record(totally_unknown_field="x")
    errors = validate_record(record, schema)
    assert any("unexpected field" in e for e in errors)


def test_optional_fields_are_accepted_when_present(schema):
    record = _valid_record(trace_id="req-1", cached_input_tokens=10, extra={"k": "v"})
    assert validate_record(record, schema) == []


def test_null_trace_id_is_accepted(schema):
    record = _valid_record(trace_id=None)
    assert validate_record(record, schema) == []


def test_bool_is_not_accepted_as_integer(schema):
    record = _valid_record(input_tokens=True)
    errors = validate_record(record, schema)
    assert any("input_tokens" in e and "expected type" in e for e in errors)


@pytest.fixture
def alert_schema():
    text = (
        resources.files("llm_burnwatch").joinpath("alert_schema.json").read_text(encoding="utf-8")
    )
    return json.loads(text)


def _valid_alert(**overrides):
    alert = {
        "alert_schema_version": 1,
        "call_count": 10,
        "threshold": 3.5,
        "anomaly_count": 0,
        "anomalies": [],
        "frequency_detector_enabled": True,
        "cusum_detector_enabled": True,
        "budget_detector_enabled": False,
        "ml": None,
    }
    alert.update(overrides)
    return alert


def test_array_type_field_with_empty_list_has_no_errors(alert_schema):
    # `_TYPE_MAP` previously had no "array" entry, so `anomalies: []` (a
    # `list`) would spuriously fail its `"type": "array"` check.
    assert validate_record(_valid_alert(), alert_schema) == []


def test_array_type_field_with_populated_list_has_no_errors(alert_schema):
    record = _valid_alert(anomalies=[{"index": 0}], anomaly_count=1)
    assert validate_record(record, alert_schema) == []


def test_boolean_type_field_is_accepted(alert_schema):
    # `_TYPE_MAP` previously had no "boolean" entry, so a `True`/`False`
    # value for a `"type": "boolean"` field would spuriously fail.
    record = _valid_alert(frequency_detector_enabled=False)
    assert validate_record(record, alert_schema) == []


def test_number_type_field_accepts_int_and_float(alert_schema):
    # `_TYPE_MAP` previously had no "number" entry, so `threshold` (a
    # `"type": "number"` field) would spuriously fail for both ints and
    # floats.
    assert validate_record(_valid_alert(threshold=3.5), alert_schema) == []
    assert validate_record(_valid_alert(threshold=3), alert_schema) == []


def test_bool_is_not_accepted_as_number(alert_schema):
    record = _valid_alert(threshold=True)
    errors = validate_record(record, alert_schema)
    assert any("threshold" in e and "expected type" in e for e in errors)
