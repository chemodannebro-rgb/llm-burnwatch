"""Webhook sink: POSTs a JSON-serialized `Alert` to a fixed URL.

Reuses the same HTTP discipline already established for llm-burnwatch's one
other network call, `pricing_import.py`'s `_fetch_url`: a fixed timeout,
rejection of any non-`http(s)://` URL scheme, and `urllib.request` from the
standard library -- no new dependency, so unlike `[anomaly]` this isn't
gated behind a pip extra, it's a new row in `ARCHITECTURE.md`'s "Network
boundaries" table, the same way `pricing import <url>` already is (see
SECURITY.md for the full trust-boundary writeup). Unlike `pricing_import`,
the response body is never read here -- only `response.status` is
inspected -- so there's no equivalent to `pricing_import`'s response-size
cap to enforce; nothing is buffered in memory either way.
"""

from __future__ import annotations

import dataclasses
import json
import urllib.request
from urllib.error import HTTPError, URLError

from ..detectors.protocol import Alert
from .protocol import SinkError

TIMEOUT_SECONDS = 10


class WebhookSink:
    name = "webhook"

    def __init__(self, url: str, timeout: float = TIMEOUT_SECONDS) -> None:
        if not url.startswith("http://") and not url.startswith("https://"):
            raise ValueError(
                f"webhook URL {url!r} must use http:// or https:// "
                "-- other schemes (file://, etc.) are not supported"
            )
        self.url = url
        self.timeout = timeout

    def send(self, alert: Alert) -> None:
        self.post_json(dataclasses.asdict(alert))

    def post_json(self, payload: dict) -> None:
        """POST `payload` as JSON to `self.url`. Split out from `send` so
        `SlackSink` can reuse this HTTP-POST logic for its own,
        differently-shaped payload instead of duplicating it.
        """
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "llm-burnwatch-webhook-sink",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                if response.status >= 300:
                    raise SinkError(f"webhook {self.url} returned HTTP {response.status}")
        except HTTPError as exc:
            raise SinkError(f"webhook {self.url} returned HTTP {exc.code}") from exc
        except URLError as exc:
            raise SinkError(f"network error POSTing to webhook {self.url}: {exc}") from exc
