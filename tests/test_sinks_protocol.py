from __future__ import annotations

from llm_burnwatch.detectors.protocol import Alert
from llm_burnwatch.sinks.protocol import SinkError, send_to_all

_ALERT = Alert(
    detector="rules",
    severity="critical",
    kind="call_cost_exceeded",
    group_key=("chat", "gpt-4o"),
    record_ref=0,
    evidence={},
    message="call cost exceeded",
)


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


def test_send_to_all_delivers_to_every_sink():
    sinks = [_RecordingSink(), _RecordingSink()]
    send_to_all(sinks, _ALERT)
    assert sinks[0].received == [_ALERT]
    assert sinks[1].received == [_ALERT]


def test_send_to_all_one_sink_failing_does_not_stop_the_others(capsys):
    failing = _FailingSink(SinkError("boom"))
    recording = _RecordingSink()
    send_to_all([failing, recording], _ALERT)

    assert failing.calls == 1
    assert recording.received == [_ALERT]
    assert "sink 'failing' failed to deliver alert: boom" in capsys.readouterr().err


def test_send_to_all_never_raises_for_an_unexpected_exception_type():
    # Contract test (not tied to any single Sink implementation): a bug in a
    # sink that raises something other than SinkError must still not crash
    # the caller -- `detect --follow`'s poll loop can never be brought down
    # by one misbehaving sink.
    failing = _FailingSink(ValueError("not a SinkError"))
    send_to_all([failing], _ALERT)
    assert failing.calls == 1
