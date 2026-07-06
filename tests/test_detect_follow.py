from __future__ import annotations

import json
from collections import deque

import pytest

from llm_burnwatch.cli import _detect_follow_poll, build_parser
from llm_burnwatch.follow_state import load_follow_state, save_follow_state, state_path_for


def _write_lines(path, records):
    with path.open("w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def _append_lines(path, records):
    with path.open("a", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")


def _detect_args(log_file, **overrides):
    argv = ["detect", "--log-file", str(log_file)]
    for flag, value in overrides.items():
        argv += [f"--{flag.replace('_', '-')}", str(value)]
    return build_parser().parse_args(argv)


# --- follow_state.py -------------------------------------------------------


def test_state_path_for_is_sibling_of_log_with_suffix(tmp_path):
    log_path = tmp_path / "calls.jsonl"
    state_path = state_path_for(log_path)
    assert state_path.parent == tmp_path
    assert state_path.name == "calls.jsonl.llm-burnwatch-follow-state.json"


def test_load_follow_state_missing_file_returns_empty_state_without_warning(tmp_path, capsys):
    state_path = state_path_for(tmp_path / "calls.jsonl")
    state = load_follow_state(state_path)
    assert state == {"offsets": {}, "window": []}
    assert capsys.readouterr().err == ""


def test_save_then_load_follow_state_roundtrips(tmp_path):
    state_path = state_path_for(tmp_path / "calls.jsonl")
    original = {"offsets": {"a.jsonl": 42}, "window": [{"seq": 1}, {"seq": 2}]}
    save_follow_state(state_path, original)
    assert load_follow_state(state_path) == original


def test_save_follow_state_leaves_no_leftover_tmp_files(tmp_path):
    state_path = state_path_for(tmp_path / "calls.jsonl")
    save_follow_state(state_path, {"offsets": {}, "window": []})
    leftover = [p for p in tmp_path.iterdir() if p.name != state_path.name]
    assert leftover == []


def test_load_follow_state_corrupt_json_warns_and_returns_empty_state(tmp_path, capsys):
    state_path = state_path_for(tmp_path / "calls.jsonl")
    state_path.write_text("{not valid json", encoding="utf-8")

    state = load_follow_state(state_path)
    assert state == {"offsets": {}, "window": []}
    assert "could not read follow-state file" in capsys.readouterr().err


def test_load_follow_state_malformed_shape_warns_and_returns_empty_state(tmp_path, capsys):
    state_path = state_path_for(tmp_path / "calls.jsonl")
    state_path.write_text(json.dumps({"offsets": "not-a-dict", "window": []}), encoding="utf-8")

    state = load_follow_state(state_path)
    assert state == {"offsets": {}, "window": []}
    assert "could not read follow-state file" in capsys.readouterr().err


def test_load_follow_state_missing_keys_warns_and_returns_empty_state(tmp_path, capsys):
    state_path = state_path_for(tmp_path / "calls.jsonl")
    state_path.write_text(json.dumps({"offsets": {}}), encoding="utf-8")

    state = load_follow_state(state_path)
    assert state == {"offsets": {}, "window": []}
    assert "could not read follow-state file" in capsys.readouterr().err


# --- cli._detect_follow_poll ------------------------------------------------


def test_detect_follow_poll_first_poll_reads_existing_lines_and_reports_them_as_new(tmp_path):
    log_path = tmp_path / "calls.jsonl"
    _write_lines(log_path, [{"seq": 1, "cost_micros": 100}])

    args = _detect_args(log_path, max_call_cost=0.00005)
    window: deque = deque(maxlen=5000)
    alerts, offsets, had_new = _detect_follow_poll(log_path, {}, window, args)

    assert had_new is True
    assert len(window) == 1
    assert any(a.kind == "call_cost_exceeded" for a in alerts)


def test_detect_follow_poll_no_new_data_returns_no_alerts_and_no_state_change(tmp_path):
    log_path = tmp_path / "calls.jsonl"
    _write_lines(log_path, [{"seq": 1, "cost_micros": 100}])

    args = _detect_args(log_path)
    window: deque = deque(maxlen=5000)
    _, offsets, _ = _detect_follow_poll(log_path, {}, window, args)

    alerts, offsets, had_new = _detect_follow_poll(log_path, offsets, window, args)
    assert alerts == []
    assert had_new is False


def test_detect_follow_poll_only_reports_alerts_triggered_by_newly_arrived_records(tmp_path):
    log_path = tmp_path / "calls.jsonl"
    _write_lines(log_path, [{"seq": 1, "cost_micros": 100}])

    args = _detect_args(log_path, max_call_cost=0.00005)
    window: deque = deque(maxlen=5000)
    first_alerts, offsets, _ = _detect_follow_poll(log_path, {}, window, args)
    assert any(a.kind == "call_cost_exceeded" and a.record_ref == 0 for a in first_alerts)

    # A second poll with no new violating records shouldn't re-report the
    # same old violation just because the window is re-analyzed.
    _append_lines(log_path, [{"seq": 2, "cost_micros": 100}])
    second_alerts, offsets, had_new = _detect_follow_poll(log_path, offsets, window, args)
    assert had_new is True
    assert all(a.record_ref != 0 for a in second_alerts)


def test_detect_follow_poll_flags_a_new_violation_appended_after_first_poll(tmp_path):
    log_path = tmp_path / "calls.jsonl"
    _write_lines(log_path, [{"seq": 1, "cost_micros": 100}])

    args = _detect_args(log_path, max_call_cost=0.00005)
    window: deque = deque(maxlen=5000)
    _, offsets, _ = _detect_follow_poll(log_path, {}, window, args)

    _append_lines(log_path, [{"seq": 2, "cost_micros": 5_000_000}])
    alerts, offsets, had_new = _detect_follow_poll(log_path, offsets, window, args)

    assert had_new is True
    assert any(
        a.kind == "call_cost_exceeded" and a.record_ref == 1 for a in alerts
    )


def test_detect_follow_poll_evicts_oldest_records_past_window_size(tmp_path):
    log_path = tmp_path / "calls.jsonl"
    _write_lines(log_path, [{"seq": 1}])

    args = _detect_args(log_path)
    window: deque = deque(maxlen=2)
    _, offsets, _ = _detect_follow_poll(log_path, {}, window, args)

    _append_lines(log_path, [{"seq": 2}])
    _, offsets, _ = _detect_follow_poll(log_path, offsets, window, args)
    assert [r["seq"] for r in window] == [1, 2]

    _append_lines(log_path, [{"seq": 3}])
    _, offsets, _ = _detect_follow_poll(log_path, offsets, window, args)
    assert [r["seq"] for r in window] == [2, 3]
