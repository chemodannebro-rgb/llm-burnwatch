from __future__ import annotations

import json
import sys
from collections import deque
from datetime import datetime, timedelta, timezone

import pytest

from llm_burnwatch import cli as cli_module
from llm_burnwatch.cli import _build_sinks, _detect_follow_poll, _run_detect_follow, build_parser
from llm_burnwatch.detectors.protocol import Alert
from llm_burnwatch.follow_state import load_follow_state, save_follow_state, state_path_for
from llm_burnwatch.sinks.exec_sink import ExecSink
from llm_burnwatch.sinks.slack_sink import SlackSink
from llm_burnwatch.sinks.telegram_sink import TelegramSink
from llm_burnwatch.sinks.webhook_sink import WebhookSink


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


_EMPTY_STATE = {
    "offsets": {},
    "window": [],
    "poll_seq": 0,
    "alert_cooldowns": {},
    "rules_aggregation": {},
}


def test_load_follow_state_missing_file_returns_empty_state_without_warning(tmp_path, capsys):
    state_path = state_path_for(tmp_path / "calls.jsonl")
    state = load_follow_state(state_path)
    assert state == _EMPTY_STATE
    assert capsys.readouterr().err == ""


def test_save_then_load_follow_state_roundtrips(tmp_path):
    state_path = state_path_for(tmp_path / "calls.jsonl")
    original = {
        "offsets": {"a.jsonl": 42},
        "window": [{"seq": 1}, {"seq": 2}],
        "poll_seq": 7,
        "alert_cooldowns": {"k": {"last_sent_at": 1.0}},
        "rules_aggregation": {},
    }
    save_follow_state(state_path, original)
    assert load_follow_state(state_path) == original


def test_load_follow_state_defaults_poll_seq_and_cooldown_keys_for_pre_1_1_state_file(tmp_path):
    # A state file written before 1.1 added these keys -- must not be
    # treated as corrupt just for predating the feature.
    state_path = state_path_for(tmp_path / "calls.jsonl")
    state_path.write_text(
        json.dumps({"offsets": {"a.jsonl": 5}, "window": [{"seq": 1}]}), encoding="utf-8"
    )

    state = load_follow_state(state_path)
    assert state == {
        "offsets": {"a.jsonl": 5},
        "window": [{"seq": 1}],
        "poll_seq": 0,
        "alert_cooldowns": {},
        "rules_aggregation": {},
    }


def test_save_follow_state_leaves_no_leftover_tmp_files(tmp_path):
    state_path = state_path_for(tmp_path / "calls.jsonl")
    save_follow_state(state_path, _EMPTY_STATE)
    leftover = [p for p in tmp_path.iterdir() if p.name != state_path.name]
    assert leftover == []


def test_load_follow_state_corrupt_json_warns_and_returns_empty_state(tmp_path, capsys):
    state_path = state_path_for(tmp_path / "calls.jsonl")
    state_path.write_text("{not valid json", encoding="utf-8")

    state = load_follow_state(state_path)
    assert state == _EMPTY_STATE
    assert "could not read follow-state file" in capsys.readouterr().err


def test_load_follow_state_malformed_shape_warns_and_returns_empty_state(tmp_path, capsys):
    state_path = state_path_for(tmp_path / "calls.jsonl")
    state_path.write_text(json.dumps({"offsets": "not-a-dict", "window": []}), encoding="utf-8")

    state = load_follow_state(state_path)
    assert state == _EMPTY_STATE
    assert "could not read follow-state file" in capsys.readouterr().err


def test_load_follow_state_missing_keys_warns_and_returns_empty_state(tmp_path, capsys):
    state_path = state_path_for(tmp_path / "calls.jsonl")
    state_path.write_text(json.dumps({"offsets": {}}), encoding="utf-8")

    state = load_follow_state(state_path)
    assert state == _EMPTY_STATE
    assert "could not read follow-state file" in capsys.readouterr().err


# --- cli._detect_follow_poll ------------------------------------------------


def test_detect_follow_poll_first_poll_reads_existing_lines_and_reports_them_as_new(tmp_path):
    log_path = tmp_path / "calls.jsonl"
    _write_lines(log_path, [{"seq": 1, "cost_micros": 100}])

    args = _detect_args(log_path, max_call_cost=0.00005)
    window: deque = deque(maxlen=5000)
    alerts, all_alerts, offsets, had_new = _detect_follow_poll(log_path, {}, window, args)

    assert had_new is True
    assert len(window) == 1
    assert any(a.kind == "call_cost_exceeded" for a in alerts)
    assert all_alerts == alerts


def test_detect_follow_poll_no_new_data_returns_no_alerts_and_no_state_change(tmp_path):
    log_path = tmp_path / "calls.jsonl"
    _write_lines(log_path, [{"seq": 1, "cost_micros": 100}])

    args = _detect_args(log_path)
    window: deque = deque(maxlen=5000)
    _, _, offsets, _ = _detect_follow_poll(log_path, {}, window, args)

    alerts, all_alerts, offsets, had_new = _detect_follow_poll(log_path, offsets, window, args)
    assert alerts == []
    assert all_alerts == []
    assert had_new is False


def test_detect_follow_poll_only_reports_alerts_triggered_by_newly_arrived_records(tmp_path):
    log_path = tmp_path / "calls.jsonl"
    _write_lines(log_path, [{"seq": 1, "cost_micros": 100}])

    args = _detect_args(log_path, max_call_cost=0.00005)
    window: deque = deque(maxlen=5000)
    first_alerts, _, offsets, _ = _detect_follow_poll(log_path, {}, window, args)
    assert any(a.kind == "call_cost_exceeded" and a.record_ref == 0 for a in first_alerts)

    # A second poll with no new violating records shouldn't re-report the
    # same old violation just because the window is re-analyzed.
    _append_lines(log_path, [{"seq": 2, "cost_micros": 100}])
    second_alerts, all_alerts, offsets, had_new = _detect_follow_poll(
        log_path, offsets, window, args
    )
    assert had_new is True
    assert all(a.record_ref != 0 for a in second_alerts)
    # The still-violating first record shows up in all_alerts (used only for
    # cooldown continuity tracking) even though it's filtered from new_alerts.
    assert any(a.record_ref == 0 for a in all_alerts)


def test_detect_follow_poll_flags_a_new_violation_appended_after_first_poll(tmp_path):
    log_path = tmp_path / "calls.jsonl"
    _write_lines(log_path, [{"seq": 1, "cost_micros": 100}])

    args = _detect_args(log_path, max_call_cost=0.00005)
    window: deque = deque(maxlen=5000)
    _, _, offsets, _ = _detect_follow_poll(log_path, {}, window, args)

    _append_lines(log_path, [{"seq": 2, "cost_micros": 5_000_000}])
    alerts, all_alerts, offsets, had_new = _detect_follow_poll(log_path, offsets, window, args)

    assert had_new is True
    assert any(
        a.kind == "call_cost_exceeded" and a.record_ref == 1 for a in alerts
    )


def test_detect_follow_poll_reports_frequency_spike_confirmed_by_new_records(tmp_path):
    # Regression test: FrequencyDetector used to report a spike window's
    # *first* record as `record_ref`. If that first record already existed
    # from an earlier poll, this function's own new-vs-already-seen filter
    # (record_ref >= new_start_index) silently dropped the alert -- even
    # though the spike was only confirmed by records that arrived *this*
    # poll. Fixed by having FrequencyDetector report the window's *last*
    # record instead (see frequency_detector.py).
    log_path = tmp_path / "calls.jsonl"
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)

    # 60 calls, all within the same 60s frequency window -- not yet a spike
    # (FREQUENCY_ABS_CALLS_PER_WINDOW is 100, and there's no window history
    # yet for a z-score comparison).
    first_batch = [
        {
            "label": "chat",
            "model": "gpt-4o",
            "timestamp": (base + timedelta(seconds=i * 0.3)).isoformat(),
        }
        for i in range(60)
    ]
    _write_lines(log_path, first_batch)

    args = _detect_args(log_path, frequency_detector="on")
    window: deque = deque(maxlen=5000)
    first_alerts, _, offsets, _ = _detect_follow_poll(log_path, {}, window, args)
    assert not any(a.kind == "frequency_spike" for a in first_alerts)

    # 45 more calls in the *same* window, appended in a later poll, push the
    # window's total to 105 -- past the absolute fail-safe.
    second_batch = [
        {
            "label": "chat",
            "model": "gpt-4o",
            "timestamp": (base + timedelta(seconds=(60 + i) * 0.3)).isoformat(),
        }
        for i in range(45)
    ]
    _append_lines(log_path, second_batch)
    second_alerts, _, offsets, had_new = _detect_follow_poll(log_path, offsets, window, args)

    assert had_new is True
    group_spikes = [
        a
        for a in second_alerts
        if a.kind == "frequency_spike" and a.group_key == ("chat", "gpt-4o")
    ]
    assert len(group_spikes) == 1
    assert group_spikes[0].evidence["window_calls"] == 105
    # record_ref must point at a newly-arrived record (index >= 60) for the
    # alert to have survived this function's own new-vs-already-seen filter.
    assert group_spikes[0].record_ref == 104


def test_detect_follow_poll_evicts_oldest_records_past_window_size(tmp_path):
    log_path = tmp_path / "calls.jsonl"
    _write_lines(log_path, [{"seq": 1}])

    args = _detect_args(log_path)
    window: deque = deque(maxlen=2)
    _, _, offsets, _ = _detect_follow_poll(log_path, {}, window, args)

    _append_lines(log_path, [{"seq": 2}])
    _, _, offsets, _ = _detect_follow_poll(log_path, offsets, window, args)
    assert [r["seq"] for r in window] == [1, 2]

    _append_lines(log_path, [{"seq": 3}])
    _, _, offsets, _ = _detect_follow_poll(log_path, offsets, window, args)
    assert [r["seq"] for r in window] == [2, 3]


# --- cli._build_sinks --------------------------------------------------------


def test_build_sinks_returns_empty_list_by_default(tmp_path):
    args = _detect_args(tmp_path / "calls.jsonl")
    assert _build_sinks(args) == []


def test_build_sinks_builds_webhook_and_slack_and_telegram_and_exec_from_flags(tmp_path):
    args = build_parser().parse_args(
        [
            "detect",
            "--log-file",
            str(tmp_path / "calls.jsonl"),
            "--follow",
            "--webhook-url",
            "https://example.com/hook",
            "--slack-webhook-url",
            "https://hooks.slack.com/services/T/B/X",
            "--telegram-bot-token",
            "123:ABC-TOKEN",
            "--telegram-chat-id",
            "-100987654321",
            "--exec-sink",
            "some-command",
            "some-arg",
        ]
    )
    sinks = _build_sinks(args)
    assert [type(s) for s in sinks] == [WebhookSink, SlackSink, TelegramSink, ExecSink]
    assert sinks[0].url == "https://example.com/hook"
    assert sinks[3].command == ["some-command", "some-arg"]


def test_build_sinks_falls_back_to_env_vars_when_flags_not_given(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_BURNWATCH_WEBHOOK_URL", "https://example.com/env-hook")
    monkeypatch.setenv("LLM_BURNWATCH_SLACK_WEBHOOK_URL", "https://hooks.slack.com/env")
    monkeypatch.setenv("LLM_BURNWATCH_TELEGRAM_BOT_TOKEN", "123:ENV-TOKEN")
    monkeypatch.setenv("LLM_BURNWATCH_TELEGRAM_CHAT_ID", "-100000000000")
    args = _detect_args(tmp_path / "calls.jsonl")

    sinks = _build_sinks(args)
    assert [type(s) for s in sinks] == [WebhookSink, SlackSink, TelegramSink]
    assert sinks[0].url == "https://example.com/env-hook"


def test_build_sinks_explicit_flag_takes_priority_over_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_BURNWATCH_WEBHOOK_URL", "https://example.com/env-hook")
    args = build_parser().parse_args(
        [
            "detect",
            "--log-file",
            str(tmp_path / "calls.jsonl"),
            "--follow",
            "--webhook-url",
            "https://example.com/flag-hook",
        ]
    )

    sinks = _build_sinks(args)
    assert sinks[0].url == "https://example.com/flag-hook"


def test_build_sinks_rejects_telegram_bot_token_without_chat_id(tmp_path):
    args = build_parser().parse_args(
        [
            "detect",
            "--log-file",
            str(tmp_path / "calls.jsonl"),
            "--follow",
            "--telegram-bot-token",
            "123:ABC-TOKEN",
        ]
    )
    with pytest.raises(ValueError, match="bot token and a chat id"):
        _build_sinks(args)


def test_build_sinks_rejects_telegram_chat_id_without_bot_token(tmp_path):
    args = build_parser().parse_args(
        [
            "detect",
            "--log-file",
            str(tmp_path / "calls.jsonl"),
            "--follow",
            "--telegram-chat-id",
            "-100987654321",
        ]
    )
    with pytest.raises(ValueError, match="bot token and a chat id"):
        _build_sinks(args)


# --- cli._run_detect_follow: sink wiring ------------------------------------


def test_run_detect_follow_pushes_new_alerts_to_configured_exec_sink(tmp_path, monkeypatch):
    log_path = tmp_path / "calls.jsonl"
    _write_lines(log_path, [{"seq": 1, "cost_micros": 5_000_000}])

    script = tmp_path / "capture.py"
    # Appends (not overwrites) each invocation's alert JSON (read from
    # stdin, not argv) as its own line -- this poll triggers two alerts for
    # the same record (rules + baseline insufficient_data), so both are
    # delivered to the sink.
    script.write_text(
        "import sys, pathlib\n"
        "payload = sys.stdin.read()\n"
        "with pathlib.Path(sys.argv[1]).open('a') as fh:\n"
        "    fh.write(payload + chr(10))\n",
        encoding="utf-8",
    )
    out_file = tmp_path / "out.txt"

    argv = [
        "detect",
        "--log-file",
        str(log_path),
        "--max-call-cost",
        "0.00005",
        "--follow",
        "--exec-sink",
        sys.executable,
        str(script),
        str(out_file),
    ]
    args = build_parser().parse_args(argv)

    monkeypatch.setattr(cli_module.time, "sleep", lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt))

    exit_code = _run_detect_follow(args)

    assert exit_code == 0
    payloads = [json.loads(line) for line in out_file.read_text().splitlines()]
    assert any(p["kind"] == "call_cost_exceeded" for p in payloads)


def test_run_detect_follow_sink_failure_does_not_crash_the_poll_loop(tmp_path, monkeypatch, capsys):
    log_path = tmp_path / "calls.jsonl"
    _write_lines(log_path, [{"seq": 1, "cost_micros": 5_000_000}])

    argv = [
        "detect",
        "--log-file",
        str(log_path),
        "--max-call-cost",
        "0.00005",
        "--follow",
        "--exec-sink",
        "/no/such/command-llm-burnwatch-test",
    ]
    args = build_parser().parse_args(argv)

    # `cli_module.time` and `sinks.protocol.time` are the same `time` module
    # object -- one patch has to serve both purposes: no-op through the sink
    # retry's own backoff delays, but still break out of the poll loop after
    # one iteration by raising on the (much larger) `--poll-interval` sleep.
    def _fake_sleep(seconds):
        if seconds == args.poll_interval:
            raise KeyboardInterrupt
        # else: a sink-retry backoff delay -- let it pass through instantly.

    monkeypatch.setattr(cli_module.time, "sleep", _fake_sleep)

    exit_code = _run_detect_follow(args)

    assert exit_code == 0
    assert "sink 'exec' failed to deliver alert" in capsys.readouterr().err


def test_run_detect_follow_with_no_sinks_configured_behaves_as_before(tmp_path, monkeypatch):
    log_path = tmp_path / "calls.jsonl"
    _write_lines(log_path, [{"seq": 1, "cost_micros": 5_000_000}])

    argv = ["detect", "--log-file", str(log_path), "--max-call-cost", "0.00005", "--follow"]
    args = build_parser().parse_args(argv)

    monkeypatch.setattr(cli_module.time, "sleep", lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt))

    exit_code = _run_detect_follow(args)
    assert exit_code == 0


def test_run_detect_follow_with_no_sinks_opens_no_sockets_and_spawns_no_processes(
    tmp_path, monkeypatch
):
    # Proof that the sinks feature is actually opt-in: with none of
    # --webhook-url/--slack-webhook-url/--exec-sink configured, --follow must
    # not open a single socket or spawn a single subprocess, even though a
    # triggering alert is present. socket.socket/subprocess.Popen are the
    # primitives urllib.request and subprocess.run both build on, so patching
    # these two catches every sink implementation, not just today's three.
    import socket
    import subprocess

    log_path = tmp_path / "calls.jsonl"
    _write_lines(log_path, [{"seq": 1, "cost_micros": 5_000_000}])

    def _no_sockets(*args, **kwargs):
        raise AssertionError("a socket was opened with no sinks configured")

    def _no_subprocess(*args, **kwargs):
        raise AssertionError("a subprocess was spawned with no sinks configured")

    monkeypatch.setattr(socket, "socket", _no_sockets)
    monkeypatch.setattr(subprocess, "Popen", _no_subprocess)

    argv = ["detect", "--log-file", str(log_path), "--max-call-cost", "0.00005", "--follow"]
    args = build_parser().parse_args(argv)

    monkeypatch.setattr(cli_module.time, "sleep", lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt))

    exit_code = _run_detect_follow(args)
    assert exit_code == 0


# --- cli._run_detect_follow: alert cooldown (1.1) ----------------------------


def _make_exec_capture_sink(tmp_path):
    script = tmp_path / "capture.py"
    script.write_text(
        "import sys, pathlib\n"
        "payload = sys.stdin.read()\n"
        "with pathlib.Path(sys.argv[1]).open('a') as fh:\n"
        "    fh.write(payload + chr(10))\n",
        encoding="utf-8",
    )
    out_file = tmp_path / "out.txt"
    return script, out_file


def test_run_detect_follow_cooldown_suppresses_repeated_sink_deliveries_for_a_sustained_alert(
    tmp_path, monkeypatch
):
    # A non-rules alert that keeps triggering, identically, on three
    # consecutive polls (no gap) must reach the sink once, not three times --
    # the whole point of 1.1. The detector engine itself is faked out so
    # this test is about cooldown wiring, not any particular detector's
    # trigger conditions (those are covered elsewhere).
    log_path = tmp_path / "calls.jsonl"
    _write_lines(log_path, [{"seq": 1}])

    def _fake_run_detectors(records, **kwargs):
        return [
            Alert(
                detector="baseline",
                severity="warning",
                kind="zscore_outlier",
                group_key=("chat", "gpt-4o"),
                record_ref=len(records) - 1,
                evidence={"scores": [{"feature": "output_tokens", "z_score": 3.0}]},
                message="anomalous call",
            )
        ]

    monkeypatch.setattr(cli_module, "run_detectors", _fake_run_detectors)

    script, out_file = _make_exec_capture_sink(tmp_path)
    argv = [
        "detect",
        "--log-file",
        str(log_path),
        "--follow",
        "--exec-sink",
        sys.executable,
        str(script),
        str(out_file),
    ]
    args = build_parser().parse_args(argv)

    poll_count = {"n": 0}

    def _fake_sleep(seconds):
        poll_count["n"] += 1
        if poll_count["n"] >= 3:
            raise KeyboardInterrupt
        # Append one more record so the next poll sees "new data" and the
        # (faked) detector registry runs again over the growing window.
        _append_lines(log_path, [{"seq": poll_count["n"] + 1}])

    monkeypatch.setattr(cli_module.time, "sleep", _fake_sleep)

    exit_code = _run_detect_follow(args)

    assert exit_code == 0
    payloads = [json.loads(line) for line in out_file.read_text().splitlines()]
    assert len(payloads) == 1


def test_run_detect_follow_prints_every_alert_to_stdout_even_while_cooldown_suppresses_the_sink(
    tmp_path, monkeypatch, capsys
):
    log_path = tmp_path / "calls.jsonl"
    _write_lines(log_path, [{"seq": 1}])

    def _fake_run_detectors(records, **kwargs):
        return [
            Alert(
                detector="baseline",
                severity="warning",
                kind="zscore_outlier",
                group_key=("chat", "gpt-4o"),
                record_ref=len(records) - 1,
                evidence={"scores": [{"feature": "output_tokens", "z_score": 3.0}]},
                message="anomalous call",
            )
        ]

    monkeypatch.setattr(cli_module, "run_detectors", _fake_run_detectors)

    argv = ["detect", "--log-file", str(log_path), "--follow"]
    args = build_parser().parse_args(argv)

    poll_count = {"n": 0}

    def _fake_sleep(seconds):
        poll_count["n"] += 1
        if poll_count["n"] >= 3:
            raise KeyboardInterrupt
        _append_lines(log_path, [{"seq": poll_count["n"] + 1}])

    monkeypatch.setattr(cli_module.time, "sleep", _fake_sleep)

    exit_code = _run_detect_follow(args)

    assert exit_code == 0
    printed = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip().startswith("{")
    ]
    assert len(printed) == 3


def test_run_detect_follow_rules_alerts_are_aggregated_not_cooldown_suppressed(
    tmp_path, monkeypatch
):
    # rules violations (real money/policy, always severity=critical) must
    # never go silent for a full cooldown window -- the first violation of
    # a --max-call-cost breach is delivered right away, same as before 1.1.
    log_path = tmp_path / "calls.jsonl"
    _write_lines(log_path, [{"seq": 1, "cost_micros": 5_000_000}])

    script, out_file = _make_exec_capture_sink(tmp_path)
    argv = [
        "detect",
        "--log-file",
        str(log_path),
        "--max-call-cost",
        "0.00005",
        "--follow",
        "--exec-sink",
        sys.executable,
        str(script),
        str(out_file),
    ]
    args = build_parser().parse_args(argv)

    monkeypatch.setattr(cli_module.time, "sleep", lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt))

    exit_code = _run_detect_follow(args)

    assert exit_code == 0
    payloads = [json.loads(line) for line in out_file.read_text().splitlines()]
    assert any(p["kind"] == "call_cost_exceeded" for p in payloads)
