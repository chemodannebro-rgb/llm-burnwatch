from __future__ import annotations

import pytest

import llm_burnwatch.sinks.protocol as sinks_protocol
from llm_burnwatch.detectors.protocol import Alert
from llm_burnwatch.sinks.protocol import SINK_RETRY_ATTEMPTS, SinkError, send_to_all

_ALERT = Alert(
    detector="rules",
    severity="critical",
    kind="call_cost_exceeded",
    group_key=("chat", "gpt-4o"),
    record_ref=0,
    evidence={},
    message="call cost exceeded",
)


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Avoid real backoff delays -- retries in these tests should be instant."""
    monkeypatch.setattr(sinks_protocol.time, "sleep", lambda _seconds: None)


class _RecordingSink:
    name = "recording"

    def __init__(self):
        self.received = []

    def send(self, alert):
        self.received.append(alert)


class _FailingSink:
    name = "failing"

    def __init__(self, exc):
        self._exc = exc
        self.calls = 0

    def send(self, alert):
        self.calls += 1
        raise self._exc


class _FlakySink:
    """Fails `fail_times` times, then succeeds -- for exercising the retry
    path's success case (no `warn()` should be triggered)."""

    name = "flaky"

    def __init__(self, exc, fail_times):
        self._exc = exc
        self._fail_times = fail_times
        self.calls = 0
        self.received = []

    def send(self, alert):
        self.calls += 1
        if self.calls <= self._fail_times:
            raise self._exc
        self.received.append(alert)


def test_send_to_all_delivers_to_every_sink():
    sinks = [_RecordingSink(), _RecordingSink()]
    send_to_all(sinks, _ALERT)
    assert sinks[0].received == [_ALERT]
    assert sinks[1].received == [_ALERT]


def test_send_to_all_one_sink_failing_does_not_stop_the_others(capsys):
    failing = _FailingSink(SinkError("boom"))
    recording = _RecordingSink()
    send_to_all([failing, recording], _ALERT)

    assert failing.calls == SINK_RETRY_ATTEMPTS
    assert recording.received == [_ALERT]
    assert "sink 'failing' failed to deliver alert: boom" in capsys.readouterr().err


def test_send_to_all_never_raises_for_an_unexpected_exception_type():
    # Contract test (not tied to any single Sink implementation): a bug in a
    # sink that raises something other than SinkError must still not crash
    # the caller -- `detect --follow`'s poll loop can never be brought down
    # by one misbehaving sink.
    failing = _FailingSink(ValueError("not a SinkError"))
    send_to_all([failing], _ALERT)
    assert failing.calls == SINK_RETRY_ATTEMPTS


def test_send_to_all_retries_and_succeeds_before_exhausting_attempts(capsys):
    flaky = _FlakySink(SinkError("transient"), fail_times=SINK_RETRY_ATTEMPTS - 1)
    send_to_all([flaky], _ALERT)

    assert flaky.calls == SINK_RETRY_ATTEMPTS
    assert flaky.received == [_ALERT]
    assert capsys.readouterr().err == ""
