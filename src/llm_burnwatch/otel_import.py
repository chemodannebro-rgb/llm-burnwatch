"""Import OpenTelemetry GenAI semantic-convention trace exports into
llm-burnwatch's own JSONL log format.

Accepts the raw OTLP JSON export shape (`resourceSpans` -> `scopeSpans` ->
`spans`), as either a single JSON object, a JSON array of such objects, or
JSONL (one such object per line -- what an OTel Collector's file exporter
typically writes, one `ExportTraceServiceRequest` per line).

`source` must be a local file path. Unlike `pricing import <url>`, this
deliberately does NOT accept an http(s):// URL: that would be a second,
unrelated network boundary that nothing asked for -- trivial to add later as
an explicit opt-in flag if it's ever actually needed. See "Network
boundaries" in ARCHITECTURE.md.

Tolerant of both attribute-naming generations the OTel GenAI semantic
conventions have had in the wild -- the current (v1.36+) stable names and the
older/OpenLLMetry-style names many instrumentations still emit by default --
see the `_..._ATTRS` tuples below. Also tolerant of spans that carry no
recognizable `gen_ai.*` usage attributes at all (most real traces are mostly
non-GenAI spans -- HTTP handlers, DB calls, ...): those are silently skipped,
the same tolerant-parsing precedent as `pricing_import.parse_litellm_pricing`.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ._messages import warn
from .tracker import SCHEMA_VERSION, resolve_pricing

# Current (v1.36+) stable name first, tried in order -- the first attribute
# present on a span wins. See CHANGELOG.md [0.9.4] for the sources behind
# this list.
_MODEL_ATTRS = ("gen_ai.request.model",)
_INPUT_TOKEN_ATTRS = ("gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens")
_OUTPUT_TOKEN_ATTRS = ("gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens")
_CACHED_INPUT_TOKEN_ATTRS = ("gen_ai.usage.cache_read_input_tokens",)
_OPERATION_NAME_ATTR = "gen_ai.operation.name"


class OtelImportError(Exception):
    """Raised for any failure reading or parsing an OTel export file."""


def _read_local_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        raise OtelImportError(f"cannot read {path}: {exc}") from exc


def _iter_export_objects(raw_text: str) -> Iterator[dict]:
    """Yield each top-level OTLP export object in `raw_text`: the object
    itself, each element of a JSON array, or (if neither parses as one JSON
    document) each non-empty line of a JSONL file.
    """
    stripped = raw_text.strip()
    if not stripped:
        return
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        for line in stripped.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise OtelImportError(f"invalid JSON line: {exc}") from exc
        return
    if isinstance(parsed, list):
        yield from parsed
    else:
        yield parsed


def _attr_value(value: dict) -> Any:
    """Decode one OTLP JSON `AnyValue` object into a plain Python value."""
    if "stringValue" in value:
        return value["stringValue"]
    if "intValue" in value:
        return int(value["intValue"])
    if "doubleValue" in value:
        return value["doubleValue"]
    if "boolValue" in value:
        return value["boolValue"]
    return None


def _span_attributes(span: dict) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    for attr in span.get("attributes") or []:
        if not isinstance(attr, dict):
            continue
        key = attr.get("key")
        value = attr.get("value")
        if key is None or not isinstance(value, dict):
            continue
        attrs[key] = _attr_value(value)
    return attrs


def _first_present(attrs: dict[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if name in attrs and attrs[name] is not None:
            return attrs[name]
    return None


def _span_timestamp(span: dict) -> str:
    nanos = span.get("startTimeUnixNano")
    try:
        # nanos may be None/non-numeric for a malformed span -- int() raising
        # TypeError/ValueError is exactly the fallback path below, so a
        # missing/bad field is handled deliberately, not a type bug.
        return datetime.fromtimestamp(
            int(nanos) / 1_000_000_000,  # type: ignore[arg-type]
            tz=timezone.utc,
        ).isoformat(timespec="seconds")
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _cost_micros(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int,
    pricing: dict,
    warned_models: set[str],
) -> int:
    rates = pricing.get("models", {}).get(model)
    if rates is None:
        if model not in warned_models:
            warn(f"no pricing found for model {model!r}; importing at cost_micros=0")
            warned_models.add(model)
        return 0
    input_rate = rates.get("input_per_1m", 0.0)
    output_rate = rates.get("output_per_1m", 0.0)
    cached_rate = rates.get("cached_input_per_1m", input_rate)
    micros = (
        input_tokens * input_rate
        + cached_input_tokens * cached_rate
        + output_tokens * output_rate
    )
    return round(micros)


def parse_otel_spans(raw_text: str, *, pricing: dict | None = None) -> list[dict]:
    """Parse an OTLP JSON/JSONL export into llm-burnwatch JSONL records.

    Spans lacking a `gen_ai.request.model` attribute, or lacking both an
    input- and output-token count, are silently skipped -- a real trace
    export is expected to contain plenty of non-GenAI spans that were never
    meant to become cost records. If the export contains at least one span
    but none of them are recognizable as GenAI calls, a single warning is
    printed (nothing to import is more likely a caller mistake -- wrong
    file, wrong exporter config -- than an intentionally empty result).

    Raises `OtelImportError` if `raw_text` isn't valid JSON/JSONL at all.
    """
    resolved_pricing = pricing if pricing is not None else resolve_pricing()
    warned_models: set[str] = set()
    records: list[dict] = []
    span_count = 0

    for export_obj in _iter_export_objects(raw_text):
        if not isinstance(export_obj, dict):
            continue
        for resource_span in export_obj.get("resourceSpans") or []:
            for scope_span in resource_span.get("scopeSpans") or []:
                for span in scope_span.get("spans") or []:
                    if not isinstance(span, dict):
                        continue
                    span_count += 1
                    attrs = _span_attributes(span)
                    model = _first_present(attrs, _MODEL_ATTRS)
                    raw_input_tokens = _first_present(attrs, _INPUT_TOKEN_ATTRS)
                    raw_output_tokens = _first_present(attrs, _OUTPUT_TOKEN_ATTRS)

                    if model is None or (raw_input_tokens is None and raw_output_tokens is None):
                        continue

                    input_tokens = int(raw_input_tokens or 0)
                    output_tokens = int(raw_output_tokens or 0)
                    cached_input_tokens = int(_first_present(attrs, _CACHED_INPUT_TOKEN_ATTRS) or 0)
                    label = attrs.get(_OPERATION_NAME_ATTR) or span.get("name") or model

                    record: dict[str, Any] = {
                        "schema_version": SCHEMA_VERSION,
                        "timestamp": _span_timestamp(span),
                        "label": label,
                        "model": model,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "cached_input_tokens": cached_input_tokens,
                        "cost_micros": _cost_micros(
                            model,
                            input_tokens,
                            output_tokens,
                            cached_input_tokens,
                            resolved_pricing,
                            warned_models,
                        ),
                    }
                    trace_id = span.get("traceId")
                    if trace_id:
                        record["trace_id"] = trace_id
                    records.append(record)

    if span_count and not records:
        warn(
            f"none of the {span_count} span(s) in the OTel export had recognizable "
            "gen_ai.* usage attributes -- nothing imported"
        )

    return records


def import_otel(source: str, dest: Path, *, pricing: dict | None = None) -> list[dict]:
    """Read+parse `source` (a local OTLP JSON/JSONL export file) and append
    the resulting records to `dest` (a llm-burnwatch JSONL log). Creates
    `dest`'s parent directory if needed, and -- only if `dest` didn't already
    exist -- locks it down to 0600, matching `CostTracker`'s own log file
    permissions. Appends rather than atomically replacing: `dest` is a log
    file other processes may already be writing to concurrently, unlike
    `pricing.json`. Returns the list of records appended.
    """
    raw = _read_local_file(source)
    records = parse_otel_spans(raw, pricing=pricing)

    dest.parent.mkdir(parents=True, exist_ok=True)
    file_is_new = not dest.exists()
    with dest.open("a", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, separators=(",", ":")))
            fh.write("\n")
    if file_is_new:
        try:
            dest.chmod(0o600)
        except OSError:
            pass

    return records
