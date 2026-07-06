"""Common protocol for anomaly detectors and the `Alert` they emit.

Every detector (the existing baseline z-score, and the frequency/CUSUM/rules
detectors planned for the rest of the v0.8 milestone) implements a single
batch method, `analyze(records) -> list[Alert]`, rather than a `feed`/
`finalize` streaming split: even `detect --follow` re-runs detectors over a
small, fixed-size window on each poll rather than accumulating incremental
state across polls, so a streaming API would add complexity with no matching
benefit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

# Bumped whenever the shape of `Alert` (or its JSON rendering) changes in a
# way existing consumers should know about.
ALERT_SCHEMA_VERSION = 1


@dataclass
class Alert:
    """One anomaly finding from a single detector.

    `group_key` identifies the statistical population the finding was scored
    against (e.g. a `(label, model)` pair); `record_ref` is the index of the
    triggering record in the input sequence, or `None` for alerts that are
    not tied to a single record. `evidence` carries whatever
    detector-specific numbers back a JSON consumer needs to render `message`
    itself (e.g. baseline's per-feature z-scores).
    """

    detector: str
    severity: str  # "info" | "warning" | "critical"
    kind: str
    group_key: tuple
    record_ref: int | None
    evidence: dict[str, Any] = field(default_factory=dict)
    message: str = ""


class Detector(Protocol):
    name: str
    enabled_by_default: bool

    def analyze(self, records: Sequence[dict]) -> list[Alert]: ...
