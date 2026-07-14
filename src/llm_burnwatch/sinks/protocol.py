"""Common protocol for alert sinks: something `detect --follow` can hand a
freshly-triggered `Alert` to, e.g. a webhook, Slack, or a local command.

Sinks are wired in only from `_run_detect_follow` (see cli.py) -- the
one-shot `detect` already has `--json`/text output meant to be piped into a
script, so there's nothing for a sink to add there; `--follow` is the only
mode where an alert "happens once" and a push notification makes sense.

A sink failure (network error, non-2xx response, non-zero exit code, ...)
must never crash `--follow` or stop the remaining sinks from being tried for
the same alert -- the same "one failure doesn't take down the whole poll"
discipline already used for the ML cross-check and the follow-state file.
`send_to_all` is the single place that enforces this, so every call site
(just `_run_detect_follow` today) gets it for free instead of each having to
remember to wrap `sink.send()` in its own try/except.
"""

from __future__ import annotations

import time
from typing import Protocol, Sequence

from .._messages import warn
from ..detectors.protocol import Alert

# Simple, uniform retry policy (no distinction by exception type or HTTP
# status) -- a transient network error/5xx shouldn't lose an alert forever,
# but this deliberately doesn't try to be smarter than "retry a few times
# with exponential backoff", per the scope this was asked for.
SINK_RETRY_ATTEMPTS = 3
SINK_RETRY_BASE_DELAY_SECONDS = 1.0


class SinkError(Exception):
    """Raised by a `Sink.send()` implementation for any failure to deliver
    `alert` -- network error, non-2xx response, non-zero exit code, etc.
    Not required (any exception is caught by `send_to_all`), but the sinks
    in this package all raise it specifically so their own tests can assert
    on a stable, sink-specific exception type rather than a bare `Exception`.
    """


class Sink(Protocol):
    name: str

    def send(self, alert: Alert) -> None: ...


def send_to_all(sinks: Sequence[Sink], alert: Alert) -> None:
    """Send `alert` to every sink in `sinks`, in order.

    Each sink gets up to `SINK_RETRY_ATTEMPTS` attempts with exponential
    backoff (`SINK_RETRY_BASE_DELAY_SECONDS * 2 ** attempt_index` between
    attempts) before being given up on. Any exception still raised after the
    last attempt is caught, reported via `warn()`, and does not stop the
    remaining sinks from being tried for the same alert.
    """
    for sink in sinks:
        for attempt_index in range(SINK_RETRY_ATTEMPTS):
            try:
                sink.send(alert)
                break
            except Exception as exc:  # noqa: BLE001 - a sink must never crash --follow
                if attempt_index == SINK_RETRY_ATTEMPTS - 1:
                    warn(f"sink {sink.name!r} failed to deliver alert: {exc}")
                else:
                    time.sleep(SINK_RETRY_BASE_DELAY_SECONDS * (2**attempt_index))
