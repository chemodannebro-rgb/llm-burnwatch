"""Slack sink: posts an alert to a Slack incoming-webhook URL.

Slack's incoming webhooks accept a plain HTTP POST of `{"text": ...}` JSON --
the same transport `WebhookSink` already implements, just a different
payload shape -- so this composes `WebhookSink.post_json` instead of
reimplementing the HTTP POST/error handling.
"""

from __future__ import annotations

from ..detectors.protocol import Alert
from .webhook_sink import TIMEOUT_SECONDS, WebhookSink


class SlackSink:
    name = "slack"

    def __init__(self, webhook_url: str, timeout: float = TIMEOUT_SECONDS) -> None:
        self._webhook = WebhookSink(webhook_url, timeout=timeout)

    def send(self, alert: Alert) -> None:
        text = f"[{alert.severity}] {alert.detector}/{alert.kind}: {alert.message}"
        self._webhook.post_json({"text": text})
