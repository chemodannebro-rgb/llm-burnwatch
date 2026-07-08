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
    def __init__(self, status: int = 200, url: str = "https://example.com/hook"):
        self.status = status
        self._url = url

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def geturl(self):
        return self._url


def test_send_posts_json_serialized_alert(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["body"] = json.loads(request.data)
        captured["headers"] = dict(request.header_items())
        captured["timeout"] = timeout
        return _FakeResponse(200, url=request.full_url)

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
        webhook_sink.urllib.request,
        "urlopen",
        lambda request, timeout: _FakeResponse(500, url=request.full_url),
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


# --- 1.0.0-a: secret redaction in SinkError messages -------------------------

_SECRET_URL = "https://hooks.slack.com/services/T000/B000/XXXXXXXXXXXXXXXXXXXXXXXX"
_SECRET_PATH = "/services/T000/B000/XXXXXXXXXXXXXXXXXXXXXXXX"


def test_non_2xx_error_message_omits_secret_path(monkeypatch):
    monkeypatch.setattr(
        webhook_sink.urllib.request,
        "urlopen",
        lambda request, timeout: _FakeResponse(500, url=request.full_url),
    )

    with pytest.raises(SinkError) as exc_info:
        WebhookSink(_SECRET_URL).send(_ALERT)

    assert _SECRET_PATH not in str(exc_info.value)
    assert "hooks.slack.com" in str(exc_info.value)


def test_http_error_message_omits_secret_path(monkeypatch):
    from urllib.error import HTTPError

    def fake_urlopen(request, timeout):
        raise HTTPError(request.full_url, 404, "Not Found", hdrs=None, fp=None)

    monkeypatch.setattr(webhook_sink.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(SinkError) as exc_info:
        WebhookSink(_SECRET_URL).send(_ALERT)

    assert _SECRET_PATH not in str(exc_info.value)


def test_network_error_message_omits_secret_path(monkeypatch):
    import socket
    from urllib.error import URLError

    def fake_urlopen(request, timeout):
        raise URLError(socket.gaierror("name resolution failed"))

    monkeypatch.setattr(webhook_sink.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(SinkError) as exc_info:
        WebhookSink(_SECRET_URL).send(_ALERT)

    assert _SECRET_PATH not in str(exc_info.value)


def test_network_error_reason_itself_does_not_carry_secret_url(monkeypatch):
    """`URLError.reason` for realistic underlying failures (DNS resolution,
    connection refused) is a plain OS-level exception describing the failure
    class, not the request URL -- confirm that assumption holds so that using
    `exc.reason` (instead of `str(exc)`, which wraps the request) in the
    `SinkError` message is actually safe.
    """
    import socket
    from urllib.error import URLError

    def fake_urlopen(request, timeout):
        raise URLError(socket.gaierror("name resolution failed"))

    monkeypatch.setattr(webhook_sink.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(SinkError) as exc_info:
        WebhookSink(_SECRET_URL).send(_ALERT)

    assert _SECRET_PATH not in str(exc_info.value.__cause__.reason)


# --- 1.0.0-b: redirect / scheme-downgrade protection --------------------------


def test_redirect_to_different_host_is_rejected(monkeypatch):
    def fake_urlopen(request, timeout):
        return _FakeResponse(200, url="https://attacker.example/hook")

    monkeypatch.setattr(webhook_sink.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(SinkError, match="redirect"):
        WebhookSink(_SECRET_URL).send(_ALERT)


def test_redirect_downgrading_https_to_http_is_rejected(monkeypatch):
    def fake_urlopen(request, timeout):
        return _FakeResponse(200, url="http://example.com/hook")

    monkeypatch.setattr(webhook_sink.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(SinkError, match="redirect"):
        WebhookSink("https://example.com/hook").send(_ALERT)


def test_redirect_error_message_omits_secret_path(monkeypatch):
    def fake_urlopen(request, timeout):
        return _FakeResponse(200, url="https://attacker.example/hook")

    monkeypatch.setattr(webhook_sink.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(SinkError) as exc_info:
        WebhookSink(_SECRET_URL).send(_ALERT)

    assert _SECRET_PATH not in str(exc_info.value)


def test_redirect_to_same_host_and_scheme_is_allowed(monkeypatch):
    """A redirect that lands back on the same host/port and scheme (e.g.
    http -> https on the *same* host, or a path-only redirect) is not a
    host/scheme hijack and must not be rejected.
    """

    def fake_urlopen(request, timeout):
        return _FakeResponse(200, url="https://example.com/hook/v2")

    monkeypatch.setattr(webhook_sink.urllib.request, "urlopen", fake_urlopen)

    WebhookSink("https://example.com/hook").send(_ALERT)
