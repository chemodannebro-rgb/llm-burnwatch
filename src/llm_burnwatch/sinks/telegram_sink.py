"""Telegram sink: posts an alert to a Telegram chat via the Bot API.

Same shape as `SlackSink`: composes `WebhookSink.post_json` instead of
reimplementing the HTTP POST/error handling, and formats the alert as the
same plain-text line Slack uses (`[severity] detector/kind: message`) rather
than Telegram's Markdown/HTML `parse_mode`, so there's no message-formatting
escaping to get right (Telegram's MarkdownV2 requires escaping a long list of
characters in arbitrary alert text -- not worth the added failure mode for a
notification line).

Unlike webhook/Slack, the caller doesn't supply an arbitrary destination
URL: `bot_token` and `chat_id` are supplied separately, and this sink builds
the fixed `https://api.telegram.org/bot<token>/sendMessage` endpoint itself,
so there's no non-http(s) scheme to reject here (the host is hard-coded, not
caller-supplied). `bot_token` is embedded in that URL, exactly like a Slack
incoming-webhook URL embeds its own secret -- so, like Slack, a delivery
failure's `SinkError` message (which includes the URL) is only ever logged
locally via `warn()`, never sent anywhere else.
"""

from __future__ import annotations

from ..detectors.protocol import Alert
from .webhook_sink import TIMEOUT_SECONDS, WebhookSink


class TelegramSink:
    name = "telegram"

    def __init__(self, bot_token: str, chat_id: str, timeout: float = TIMEOUT_SECONDS) -> None:
        self._chat_id = chat_id
        self._webhook = WebhookSink(
            f"https://api.telegram.org/bot{bot_token}/sendMessage", timeout=timeout
        )

    def send(self, alert: Alert) -> None:
        text = f"[{alert.severity}] {alert.detector}/{alert.kind}: {alert.message}"
        self._webhook.post_json({"chat_id": self._chat_id, "text": text})
