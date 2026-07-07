from __future__ import annotations

import json

from llm_burnwatch.cli import main


def _attr(key, value):
    if isinstance(value, int) and not isinstance(value, bool):
        return {"key": key, "value": {"intValue": str(value)}}
    return {"key": key, "value": {"stringValue": value}}


def _span(attrs, *, name="chat"):
    return {
        "traceId": "trace-abc",
        "spanId": "span-1",
        "name": name,
        "startTimeUnixNano": "1700000000000000000",
        "attributes": [_attr(k, v) for k, v in attrs.items()],
    }


def _export(spans):
    return {"resourceSpans": [{"resource": {}, "scopeSpans": [{"scope": {}, "spans": spans}]}]}


def test_import_otel_writes_records_and_prints_confirmation(tmp_path, capsys):
    span = _span(
        {"gen_ai.request.model": "gpt-4o", "gen_ai.usage.input_tokens": 10, "gen_ai.usage.output_tokens": 5}
    )
    source = tmp_path / "export.json"
    source.write_text(json.dumps(_export([span])), encoding="utf-8")
    dest = tmp_path / "calls.jsonl"

    exit_code = main(["import", "otel", str(source), "--log-file", str(dest)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert f"imported 1 call(s) to {dest}" in captured.out
    lines = dest.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["model"] == "gpt-4o"
    assert record["input_tokens"] == 10
    assert record["output_tokens"] == 5


def test_import_otel_appends_to_an_existing_log(tmp_path, capsys):
    span = _span(
        {"gen_ai.request.model": "gpt-4o", "gen_ai.usage.input_tokens": 1, "gen_ai.usage.output_tokens": 1}
    )
    source = tmp_path / "export.json"
    source.write_text(json.dumps(_export([span])), encoding="utf-8")
    dest = tmp_path / "calls.jsonl"
    dest.write_text('{"pre-existing": "record"}\n', encoding="utf-8")

    exit_code = main(["import", "otel", str(source), "--log-file", str(dest)])
    capsys.readouterr()

    assert exit_code == 0
    lines = dest.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2


def test_import_otel_with_no_recognizable_spans_imports_zero(tmp_path, capsys):
    http_span = _span({"http.method": "GET"}, name="GET /health")
    source = tmp_path / "export.json"
    source.write_text(json.dumps(_export([http_span])), encoding="utf-8")
    dest = tmp_path / "calls.jsonl"

    exit_code = main(["import", "otel", str(source), "--log-file", str(dest)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "imported 0 call(s)" in captured.out
    assert "none of the 1 span(s)" in captured.err


def test_import_otel_reports_error_on_missing_source(tmp_path, capsys):
    dest = tmp_path / "calls.jsonl"

    exit_code = main(["import", "otel", str(tmp_path / "missing.json"), "--log-file", str(dest)])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "cannot read" in captured.err
    assert not dest.exists()


def test_import_otel_reports_error_on_invalid_json(tmp_path, capsys):
    source = tmp_path / "export.json"
    source.write_text("{not valid json\nstill not valid", encoding="utf-8")
    dest = tmp_path / "calls.jsonl"

    exit_code = main(["import", "otel", str(source), "--log-file", str(dest)])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "invalid JSON" in captured.err


def test_import_otel_uses_local_pricing_json(tmp_path, capsys, monkeypatch):
    # Verifies the CLI path resolves pricing the normal way (packaged
    # default here, since no --pricing-file flag exists on this subcommand
    # and no user pricing.json is configured) rather than hardcoding a
    # pricing dict internally.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    span = _span(
        {"gen_ai.request.model": "gpt-4o", "gen_ai.usage.input_tokens": 1_000_000, "gen_ai.usage.output_tokens": 0}
    )
    source = tmp_path / "export.json"
    source.write_text(json.dumps(_export([span])), encoding="utf-8")
    dest = tmp_path / "calls.jsonl"

    exit_code = main(["import", "otel", str(source), "--log-file", str(dest)])
    capsys.readouterr()

    assert exit_code == 0
    record = json.loads(dest.read_text(encoding="utf-8").splitlines()[0])
    # Packaged pricing.json's gpt-4o input rate; just assert something
    # nonzero was actually resolved rather than every model defaulting to 0.
    assert record["cost_micros"] > 0
