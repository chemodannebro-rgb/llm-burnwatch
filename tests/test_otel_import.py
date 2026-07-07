from __future__ import annotations

import json

import pytest

from llm_burnwatch.otel_import import OtelImportError, import_otel, parse_otel_spans

SAMPLE_PRICING = {
    "models": {
        "gpt-4o": {
            "input_per_1m": 5.0,
            "output_per_1m": 15.0,
            "cached_input_per_1m": 2.5,
        },
    },
}


def _attr(key, value):
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    return {"key": key, "value": {"stringValue": value}}


def _span(attrs, *, name="chat gpt-4o", trace_id="trace-abc", start_nanos=1_700_000_000_000_000_000):
    span = {
        "traceId": trace_id,
        "spanId": "span-1",
        "name": name,
        "attributes": [_attr(k, v) for k, v in attrs.items()],
    }
    if start_nanos is not None:
        span["startTimeUnixNano"] = str(start_nanos)
    return span


def _export(spans):
    return {"resourceSpans": [{"resource": {}, "scopeSpans": [{"scope": {}, "spans": spans}]}]}


# --- current (v1.36+) attribute names ---------------------------------------


def test_parse_current_attribute_names():
    span = _span(
        {
            "gen_ai.request.model": "gpt-4o",
            "gen_ai.provider.name": "openai",
            "gen_ai.usage.input_tokens": 100,
            "gen_ai.usage.output_tokens": 50,
        }
    )
    records = parse_otel_spans(json.dumps(_export([span])), pricing=SAMPLE_PRICING)

    assert len(records) == 1
    record = records[0]
    assert record["model"] == "gpt-4o"
    assert record["input_tokens"] == 100
    assert record["output_tokens"] == 50
    assert record["cached_input_tokens"] == 0
    assert record["cost_micros"] == 100 * 5.0 + 50 * 15.0
    assert record["trace_id"] == "trace-abc"
    assert record["schema_version"] == "1.0"


# --- legacy/OpenLLMetry-style attribute names -------------------------------


def test_parse_legacy_attribute_names():
    span = _span(
        {
            "gen_ai.request.model": "gpt-4o",
            "gen_ai.system": "openai",
            "gen_ai.usage.prompt_tokens": 200,
            "gen_ai.usage.completion_tokens": 80,
        }
    )
    records = parse_otel_spans(json.dumps(_export([span])), pricing=SAMPLE_PRICING)

    assert len(records) == 1
    assert records[0]["input_tokens"] == 200
    assert records[0]["output_tokens"] == 80


def test_parse_prefers_current_names_over_legacy_when_both_present():
    span = _span(
        {
            "gen_ai.request.model": "gpt-4o",
            "gen_ai.usage.input_tokens": 10,
            "gen_ai.usage.prompt_tokens": 999,
            "gen_ai.usage.output_tokens": 5,
            "gen_ai.usage.completion_tokens": 999,
        }
    )
    records = parse_otel_spans(json.dumps(_export([span])), pricing=SAMPLE_PRICING)

    assert records[0]["input_tokens"] == 10
    assert records[0]["output_tokens"] == 5


# --- tolerant skipping -------------------------------------------------------


def test_parse_skips_span_without_model():
    span = _span({"gen_ai.usage.input_tokens": 10, "gen_ai.usage.output_tokens": 5})
    records = parse_otel_spans(json.dumps(_export([span])), pricing=SAMPLE_PRICING)
    assert records == []


def test_parse_skips_span_without_any_token_counts():
    span = _span({"gen_ai.request.model": "gpt-4o"})
    records = parse_otel_spans(json.dumps(_export([span])), pricing=SAMPLE_PRICING)
    assert records == []


def test_parse_mixed_file_only_imports_recognizable_spans():
    genai_span = _span(
        {
            "gen_ai.request.model": "gpt-4o",
            "gen_ai.usage.input_tokens": 10,
            "gen_ai.usage.output_tokens": 5,
        }
    )
    http_span = _span({"http.method": "GET", "http.status_code": 200}, name="GET /health")
    db_span = _span({"db.system": "postgresql"}, name="SELECT users")

    records = parse_otel_spans(
        json.dumps(_export([http_span, genai_span, db_span])), pricing=SAMPLE_PRICING
    )

    assert len(records) == 1
    assert records[0]["model"] == "gpt-4o"


def test_parse_skips_non_dict_spans_and_attributes():
    export = _export(["not-a-span-object"])
    records = parse_otel_spans(json.dumps(export), pricing=SAMPLE_PRICING)
    assert records == []


# --- cached tokens / label / trace_id ---------------------------------------


def test_parse_cached_input_tokens():
    span = _span(
        {
            "gen_ai.request.model": "gpt-4o",
            "gen_ai.usage.input_tokens": 100,
            "gen_ai.usage.output_tokens": 50,
            "gen_ai.usage.cache_read_input_tokens": 20,
        }
    )
    records = parse_otel_spans(json.dumps(_export([span])), pricing=SAMPLE_PRICING)
    assert records[0]["cached_input_tokens"] == 20
    assert records[0]["cost_micros"] == 100 * 5.0 + 50 * 15.0 + 20 * 2.5


def test_parse_label_prefers_operation_name_attribute():
    span = _span(
        {
            "gen_ai.request.model": "gpt-4o",
            "gen_ai.usage.input_tokens": 1,
            "gen_ai.usage.output_tokens": 1,
            "gen_ai.operation.name": "chat",
        },
        name="span-name-should-be-ignored",
    )
    records = parse_otel_spans(json.dumps(_export([span])), pricing=SAMPLE_PRICING)
    assert records[0]["label"] == "chat"


def test_parse_label_falls_back_to_span_name_then_model():
    span_with_name = _span(
        {"gen_ai.request.model": "gpt-4o", "gen_ai.usage.input_tokens": 1, "gen_ai.usage.output_tokens": 1},
        name="my-span-name",
    )
    records = parse_otel_spans(json.dumps(_export([span_with_name])), pricing=SAMPLE_PRICING)
    assert records[0]["label"] == "my-span-name"


def test_parse_trace_id_omitted_when_span_has_none():
    span = _span(
        {"gen_ai.request.model": "gpt-4o", "gen_ai.usage.input_tokens": 1, "gen_ai.usage.output_tokens": 1},
    )
    del span["traceId"]
    records = parse_otel_spans(json.dumps(_export([span])), pricing=SAMPLE_PRICING)
    assert "trace_id" not in records[0]


def test_parse_timestamp_derived_from_start_time_unix_nano():
    span = _span(
        {"gen_ai.request.model": "gpt-4o", "gen_ai.usage.input_tokens": 1, "gen_ai.usage.output_tokens": 1},
        start_nanos=1_700_000_000_000_000_000,
    )
    records = parse_otel_spans(json.dumps(_export([span])), pricing=SAMPLE_PRICING)
    assert records[0]["timestamp"] == "2023-11-14T22:13:20+00:00"


# --- unknown model cost handling --------------------------------------------


def test_parse_unknown_model_defaults_to_zero_cost_and_warns(capsys):
    span = _span(
        {
            "gen_ai.request.model": "some-unknown-model",
            "gen_ai.usage.input_tokens": 100,
            "gen_ai.usage.output_tokens": 50,
        }
    )
    records = parse_otel_spans(json.dumps(_export([span])), pricing=SAMPLE_PRICING)

    assert records[0]["cost_micros"] == 0
    err = capsys.readouterr().err
    assert "no pricing found for model 'some-unknown-model'" in err


def test_parse_warns_only_once_per_unknown_model(capsys):
    span = _span(
        {
            "gen_ai.request.model": "some-unknown-model",
            "gen_ai.usage.input_tokens": 1,
            "gen_ai.usage.output_tokens": 1,
        }
    )
    parse_otel_spans(json.dumps(_export([span, span])), pricing=SAMPLE_PRICING)
    err = capsys.readouterr().err
    assert err.count("no pricing found for model") == 1


# --- input formats: single object / array / JSONL ---------------------------


def test_parse_accepts_json_array_of_export_objects():
    span_a = _span(
        {"gen_ai.request.model": "gpt-4o", "gen_ai.usage.input_tokens": 1, "gen_ai.usage.output_tokens": 1}
    )
    span_b = _span(
        {"gen_ai.request.model": "gpt-4o", "gen_ai.usage.input_tokens": 2, "gen_ai.usage.output_tokens": 2}
    )
    raw = json.dumps([_export([span_a]), _export([span_b])])
    records = parse_otel_spans(raw, pricing=SAMPLE_PRICING)
    assert len(records) == 2


def test_parse_accepts_jsonl_export_objects():
    span_a = _span(
        {"gen_ai.request.model": "gpt-4o", "gen_ai.usage.input_tokens": 1, "gen_ai.usage.output_tokens": 1}
    )
    span_b = _span(
        {"gen_ai.request.model": "gpt-4o", "gen_ai.usage.input_tokens": 2, "gen_ai.usage.output_tokens": 2}
    )
    raw = "\n".join([json.dumps(_export([span_a])), json.dumps(_export([span_b]))])
    records = parse_otel_spans(raw, pricing=SAMPLE_PRICING)
    assert len(records) == 2


def test_parse_rejects_invalid_json():
    with pytest.raises(OtelImportError, match="invalid JSON line"):
        parse_otel_spans("{not valid json\nalso not valid")


def test_parse_empty_input_returns_empty_list():
    assert parse_otel_spans("") == []
    assert parse_otel_spans("   ") == []


def test_parse_warns_when_spans_present_but_none_recognized(capsys):
    http_span = _span({"http.method": "GET"}, name="GET /health")
    records = parse_otel_spans(json.dumps(_export([http_span])), pricing=SAMPLE_PRICING)

    assert records == []
    err = capsys.readouterr().err
    assert "none of the 1 span(s)" in err


# --- import_otel: end-to-end file import ------------------------------------


def test_import_otel_appends_records_to_dest(tmp_path):
    span = _span(
        {"gen_ai.request.model": "gpt-4o", "gen_ai.usage.input_tokens": 10, "gen_ai.usage.output_tokens": 5}
    )
    source = tmp_path / "export.json"
    source.write_text(json.dumps(_export([span])), encoding="utf-8")
    dest = tmp_path / "logs" / "calls.jsonl"

    records = import_otel(str(source), dest, pricing=SAMPLE_PRICING)

    assert len(records) == 1
    lines = dest.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == records[0]


def test_import_otel_creates_parent_directories(tmp_path):
    source = tmp_path / "export.json"
    source.write_text(json.dumps(_export([])), encoding="utf-8")
    dest = tmp_path / "a" / "b" / "calls.jsonl"

    assert not dest.parent.exists()
    import_otel(str(source), dest, pricing=SAMPLE_PRICING)
    assert dest.parent.is_dir()


def test_import_otel_sets_permissions_on_new_file(tmp_path):
    import os
    import stat

    span = _span(
        {"gen_ai.request.model": "gpt-4o", "gen_ai.usage.input_tokens": 1, "gen_ai.usage.output_tokens": 1}
    )
    source = tmp_path / "export.json"
    source.write_text(json.dumps(_export([span])), encoding="utf-8")
    dest = tmp_path / "calls.jsonl"

    import_otel(str(source), dest, pricing=SAMPLE_PRICING)

    mode = stat.S_IMODE(os.stat(dest).st_mode)
    assert mode == 0o600


def test_import_otel_appends_to_existing_file_without_overwriting(tmp_path):
    span = _span(
        {"gen_ai.request.model": "gpt-4o", "gen_ai.usage.input_tokens": 1, "gen_ai.usage.output_tokens": 1}
    )
    source = tmp_path / "export.json"
    source.write_text(json.dumps(_export([span])), encoding="utf-8")
    dest = tmp_path / "calls.jsonl"
    dest.write_text('{"pre-existing": "record"}\n', encoding="utf-8")

    import_otel(str(source), dest, pricing=SAMPLE_PRICING)

    lines = dest.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert lines[0] == '{"pre-existing": "record"}'


def test_import_otel_raises_on_missing_source_file(tmp_path):
    with pytest.raises(OtelImportError, match="cannot read"):
        import_otel(str(tmp_path / "does-not-exist.json"), tmp_path / "calls.jsonl")
