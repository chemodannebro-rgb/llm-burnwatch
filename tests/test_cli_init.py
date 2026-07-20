from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone

import pytest

from llm_burnwatch.cli import main
from llm_burnwatch.init_command import (
    INIT_SUGGESTION_MIN_CALLS,
    INIT_SUGGESTION_MIN_DAYS,
    KNOWN_SDKS,
    SDK_SNIPPETS,
    compute_init_suggestions,
    detect_available_sdks,
)


def _record(*, timestamp: str, model: str, cost_micros: int) -> dict:
    return {
        "schema_version": "1.0",
        "timestamp": timestamp,
        "label": "chat",
        "model": model,
        "input_tokens": 100,
        "output_tokens": 50,
        "cached_input_tokens": 0,
        "cost_micros": cost_micros,
    }


def _spread_records(n: int, days: int, cost_micros_fn=lambda i: 1000) -> list[dict]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    records = []
    for i in range(n):
        # Spread evenly across [0, days] so max-min timestamp span == days.
        offset = timedelta(days=days) * (i / max(n - 1, 1))
        records.append(
            _record(
                timestamp=(start + offset).isoformat(),
                model="gpt-4o-mini",
                cost_micros=cost_micros_fn(i),
            )
        )
    return records


def _write_log(tmp_path, records, name="calls.jsonl"):
    log_path = tmp_path / name
    with log_path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")
    return log_path


# --- detect_available_sdks -------------------------------------------------


def test_detect_available_sdks_returns_only_found_modules(monkeypatch):
    def fake_find_spec(name):
        if name == "openai":
            return object()
        if name == "google.genai":
            raise ModuleNotFoundError("No module named 'google'")
        return None

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)

    assert detect_available_sdks() == ["openai"]


def test_detect_available_sdks_returns_empty_when_none_found(monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)

    assert detect_available_sdks() == []


def test_detect_available_sdks_returns_all_when_all_found(monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())

    assert detect_available_sdks() == list(KNOWN_SDKS.values())


def test_known_sdks_all_have_a_snippet():
    assert set(KNOWN_SDKS.values()) == set(SDK_SNIPPETS.keys())


# --- compute_init_suggestions -----------------------------------------------


def test_compute_init_suggestions_none_when_too_few_calls():
    records = _spread_records(INIT_SUGGESTION_MIN_CALLS - 1, INIT_SUGGESTION_MIN_DAYS + 1)
    assert compute_init_suggestions(records) is None


def test_compute_init_suggestions_none_when_span_too_short():
    records = _spread_records(INIT_SUGGESTION_MIN_CALLS + 5, INIT_SUGGESTION_MIN_DAYS - 1)
    assert compute_init_suggestions(records) is None


def test_compute_init_suggestions_none_when_no_parseable_timestamps():
    records = [
        {**_record(timestamp="not-a-timestamp", model="gpt-4o-mini", cost_micros=100)}
        for _ in range(INIT_SUGGESTION_MIN_CALLS + 5)
    ]
    assert compute_init_suggestions(records) is None


def test_compute_init_suggestions_computes_expected_numbers():
    # 100 calls spread evenly over 10 days, cost_micros 1..100 (in dollars,
    # 1_000_000 micros = $1) so the arithmetic is easy to check by hand.
    n = 100
    days = 10
    records = _spread_records(n, days, cost_micros_fn=lambda i: (i + 1) * 1_000_000)

    result = compute_init_suggestions(records)

    assert result is not None
    assert result["days_spanned"] == days
    assert result["call_count"] == n
    assert result["models_seen"] == ["gpt-4o-mini"]

    # p99 nearest-rank of costs 1..100 (sorted) -> index ceil(0.99*100)-1 = 98
    # -> cost_micros[98] == 99_000_000 micros == $99, suggested = $99 * 10.
    assert result["suggested_max_call_cost_usd"] == pytest.approx(990.0)

    # total = sum(1..100) = 5050 dollars, over 10 days -> $505/day * 30.
    assert result["suggested_monthly_budget_usd"] == pytest.approx(5050 / 10 * 30)


# --- cmd_init via main() ----------------------------------------------------


def test_init_command_without_a_log_prints_onboarding_and_exits_0(tmp_path, capsys):
    log_path = tmp_path / "does-not-exist.jsonl"

    exit_code = main(["init", "--log-file", str(log_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "does not exist yet" in captured.out
    assert "suggest" not in captured.out.lower()


def test_init_command_with_sparse_log_shows_call_count_no_suggestions(tmp_path, capsys):
    records = _spread_records(5, 1)
    log_path = _write_log(tmp_path, records)

    exit_code = main(["init", "--log-file", str(log_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "5 call(s) logged so far" in captured.out
    assert "you could try" not in captured.out.lower()


def test_init_command_with_rich_log_shows_suggestions(tmp_path, capsys):
    records = _spread_records(
        INIT_SUGGESTION_MIN_CALLS + 10,
        INIT_SUGGESTION_MIN_DAYS + 3,
        cost_micros_fn=lambda i: 1_000_000,
    )
    log_path = _write_log(tmp_path, records)

    exit_code = main(["init", "--log-file", str(log_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "you could try" in captured.out.lower()
    assert "llm-burnwatch detect --max-call-cost" in captured.out
    assert "llm-burnwatch budget set --monthly" in captured.out


def test_init_command_json_output_is_valid_and_matches_text_mode(tmp_path, capsys):
    records = _spread_records(
        INIT_SUGGESTION_MIN_CALLS + 10,
        INIT_SUGGESTION_MIN_DAYS + 3,
        cost_micros_fn=lambda i: 1_000_000,
    )
    log_path = _write_log(tmp_path, records)

    exit_code = main(["init", "--log-file", str(log_path), "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["log_exists"] is True
    assert payload["call_count"] == len(records)
    assert payload["suggestions"] is not None
    assert payload["suggestions"]["call_count"] == len(records)


def test_init_command_json_output_without_log(tmp_path, capsys):
    log_path = tmp_path / "does-not-exist.jsonl"

    exit_code = main(["init", "--log-file", str(log_path), "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == 0
    assert payload["log_exists"] is False
    assert payload["call_count"] == 0
    assert payload["suggestions"] is None
