from __future__ import annotations

import json

import pytest

from llm_burnwatch.detectors.protocol import Alert
from llm_burnwatch.sinks import webhook_sink
from llm_burnwatch.sinks.protocol import SinkError
from llm_burnwatch.sinks.webhook_sink import WebhookSink

_ALERT = Alert(
    detector="rules",
    severity="critical",
    kind="call_cost_exceeded",
    group_key=("chat", "gpt-4o"),
    record_ref=3,
    evidence={"call_cost_usd": 1.5},
    message="call cost exceeded",
)


class _FakeResponse:
    def __init__(self, status: int = 200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_send_posts_json_serialized_alert(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["body"] = json.loads(request.data)
        captured["headers"] = dict(request.header_items())
        captured["timeout"] = timeout
        return _FakeResponse(200)

    monkeypatch.setattr(webhook_sink.urllib.request, "urlopen", fake_urlopen)

    WebhookSink("https://example.com/hook").send(_ALERT)

    assert captured["url"] == "https://example.com/hook"
    assert captured["method"] == "POST"
    assert captured["body"]["kind"] == "call_cost_exceeded"
    assert captured["body"]["record_ref"] == 3
    assert captured["body"]["group_key"] == ["chat", "gpt-4o"]
    assert captured["headers"]["Content-type"] == "application/json"
    assert captured["timeout"] == webhook_sink.TIMEOUT_SECONDS


def test_send_raises_sink_error_on_non_2xx_status(monkeypatch):
    monkeypatch.setattr(
        webhook_sink.urllib.request, "urlopen", lambda request, timeout: _FakeResponse(500)
    )

    with pytest.raises(SinkError, match="HTTP 500"):
        WebhookSink("https://example.com/hook").send(_ALERT)


def test_send_wraps_http_error(monkeypatch):
    from urllib.error import HTTPError

    def fake_urlopen(request, timeout):
        raise HTTPError(request.full_url, 404, "Not Found", hdrs=None, fp=None)

    monkeypatch.setattr(webhook_sink.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(SinkError, match="HTTP 404"):
        WebhookSink("https://example.com/hook").send(_ALERT)


def test_send_wraps_network_error(monkeypatch):
    import socket
    from urllib.error import URLError

    def fake_urlopen(request, timeout):
        raise URLError(socket.gaierror("name resolution failed"))

    monkeypatch.setattr(webhook_sink.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(SinkError, match="network error"):
        WebhookSink("https://example.com/hook").send(_ALERT)


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "ftp://example.com/hook", "javascript:alert(1)", "example.com/hook"],
)
def test_constructor_rejects_non_http_schemes(url):
    with pytest.raises(ValueError, match="http"):
        WebhookSink(url)


def test_constructor_accepts_http_and_https():
    WebhookSink("http://example.com/hook")
    WebhookSink("https://example.com/hook")
