from __future__ import annotations

import json
import sys

import pytest

from llm_burnwatch.detectors.protocol import Alert
from llm_burnwatch.sinks.exec_sink import ExecSink
from llm_burnwatch.sinks.protocol import SinkError

_ALERT = Alert(
    detector="rules",
    severity="critical",
    kind="call_cost_exceeded",
    group_key=("chat", "gpt-4o"),
    record_ref=3,
    evidence={"call_cost_usd": 1.5},
    message="call cost exceeded",
)


def _write_capture_script(path):
    # A tiny Python script that writes its stdin to `path`, so the test can
    # assert on exactly what ExecSink sent without depending on any real
    # external command being installed.
    script = path / "capture.py"
    script.write_text(
        "import sys, pathlib\n"
        "pathlib.Path(sys.argv[1]).write_text(sys.stdin.read())\n",
        encoding="utf-8",
    )
    return script


def test_send_runs_command_with_alert_json_via_stdin(tmp_path):
    script = _write_capture_script(tmp_path)
    out_file = tmp_path / "out.txt"

    sink = ExecSink([sys.executable, str(script), str(out_file)])
    sink.send(_ALERT)

    payload = json.loads(out_file.read_text())
    assert payload["kind"] == "call_cost_exceeded"
    assert payload["record_ref"] == 3


def test_send_does_not_pass_alert_via_argv(tmp_path):
    # Regression test: process argv is visible to other local users via
    # `ps`/`/proc/<pid>/cmdline`, stdin is not -- the alert must never appear
    # as a literal argv entry.
    script = tmp_path / "check_argv.py"
    script.write_text(
        "import sys, pathlib\n"
        "pathlib.Path(sys.argv[1]).write_text(repr(sys.argv))\n"
        "sys.stdin.read()\n",
        encoding="utf-8",
    )
    out_file = tmp_path / "argv.txt"

    sink = ExecSink([sys.executable, str(script), str(out_file)])
    sink.send(_ALERT)

    captured_argv = out_file.read_text()
    assert "call_cost_exceeded" not in captured_argv
    assert "call cost exceeded" not in captured_argv


def test_send_never_invokes_a_shell():
    # A malicious/unusual message containing shell metacharacters must be
    # passed through as inert stdin content, not interpreted.
    alert = Alert(
        detector="rules",
        severity="critical",
        kind="call_cost_exceeded",
        group_key=("chat; rm -rf /", "gpt-4o"),
        record_ref=0,
        evidence={},
        message="`$(echo pwned)`",
    )
    sink = ExecSink([sys.executable, "-c", "import sys; print(len(sys.argv))"])
    # Should not raise and should not execute the embedded shell syntax --
    # if it did, this call itself would behave unpredictably (or raise from
    # an actually-broken shell command). No exception is the expected pass.
    sink.send(alert)


def test_send_raises_sink_error_on_non_zero_exit():
    sink = ExecSink([sys.executable, "-c", "import sys; sys.exit(1)"])
    with pytest.raises(SinkError, match="exited 1"):
        sink.send(_ALERT)


def test_send_raises_sink_error_when_command_does_not_exist():
    sink = ExecSink(["/no/such/command-llm-burnwatch-test"])
    with pytest.raises(SinkError):
        sink.send(_ALERT)


def test_constructor_rejects_empty_command():
    with pytest.raises(ValueError):
        ExecSink([])
