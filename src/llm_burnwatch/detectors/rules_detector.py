"""Rules (hard-limit) detector: enforces explicit, user-configured policies
-- a model allowlist, a per-call cost cap, and a per-trace cost cap --
rather than a statistical threshold.

Unlike every other detector in this package, these aren't tuned from the
statistics literature: they're the caller's own policy, so they're exposed
as CLI flags on `detect` (`--allowed-models`, `--max-call-cost`,
`--max-trace-cost`), following the precedent already set by `--threshold`.
Every alert this detector emits is `severity="critical"`: this isn't a
"this looks statistically unusual" finding, it's "this explicitly violates
a limit you configured" -- a hard safety net, not a heuristic.

Ships enabled by default, but stays completely silent unless the caller
configures at least one rule: an unconfigured `RulesDetector` is a
deliberate no-op, not a detector with sensible built-in defaults --
there's no safe universal default for "which models are allowed" or "how
much should a call cost".
"""

from __future__ import annotations

from typing import Sequence

from .protocol import Alert

MICROS_PER_USD = 1_000_000


def _group_key(record: dict) -> tuple:
    return (record.get("label"), record.get("model"))


class RulesDetector:
    name = "rules"
    enabled_by_default = True

    def __init__(
        self,
        allowed_models: Sequence[str] | None = None,
        max_call_cost_usd: float | None = None,
        max_trace_cost_usd: float | None = None,
    ) -> None:
        self.allowed_models = set(allowed_models) if allowed_models else None
        self.max_call_cost_usd = max_call_cost_usd
        self.max_trace_cost_usd = max_trace_cost_usd

    def analyze(self, records: Sequence[dict]) -> list[Alert]:
        alerts: list[Alert] = []
        alerts.extend(self._check_allowed_models(records))
        alerts.extend(self._check_max_call_cost(records))
        alerts.extend(self._check_max_trace_cost(records))
        return alerts

    def _check_allowed_models(self, records: Sequence[dict]) -> list[Alert]:
        if self.allowed_models is None:
            return []
        alerts = []
        for i, r in enumerate(records):
            model = r.get("model")
            if model in self.allowed_models:
                continue
            alerts.append(
                Alert(
                    detector=self.name,
                    severity="critical",
                    kind="model_not_allowed",
                    group_key=_group_key(r),
                    record_ref=i,
                    evidence={
                        "model": model,
                        "allowed_models": sorted(self.allowed_models),
                    },
                    message=f"model {model!r} is not in the allowed-models list",
                )
            )
        return alerts

    def _check_max_call_cost(self, records: Sequence[dict]) -> list[Alert]:
        if self.max_call_cost_usd is None:
            return []
        alerts = []
        for i, r in enumerate(records):
            cost_micros = r.get("cost_micros")
            if cost_micros is None:
                continue
            call_cost_usd = cost_micros / MICROS_PER_USD
            if call_cost_usd <= self.max_call_cost_usd:
                continue
            alerts.append(
                Alert(
                    detector=self.name,
                    severity="critical",
                    kind="call_cost_exceeded",
                    group_key=_group_key(r),
                    record_ref=i,
                    evidence={
                        "call_cost_usd": call_cost_usd,
                        "max_call_cost_usd": self.max_call_cost_usd,
                    },
                    message=(
                        f"call cost ${call_cost_usd:.6f} exceeds "
                        f"--max-call-cost ${self.max_call_cost_usd:.6f}"
                    ),
                )
            )
        return alerts

    def _check_max_trace_cost(self, records: Sequence[dict]) -> list[Alert]:
        if self.max_trace_cost_usd is None:
            return []
        by_trace: dict[str, list[int]] = {}
        for i, r in enumerate(records):
            trace_id = r.get("trace_id")
            if trace_id is None:
                continue
            by_trace.setdefault(trace_id, []).append(i)

        alerts = []
        for trace_id, idxs in by_trace.items():
            total_micros = sum(records[i].get("cost_micros") or 0 for i in idxs)
            trace_cost_usd = total_micros / MICROS_PER_USD
            if trace_cost_usd <= self.max_trace_cost_usd:
                continue

            # The record whose cost pushed the running trace total over the
            # limit -- not necessarily the single most expensive call, but
            # the point the cap was actually crossed, so an operator can see
            # which step was the one that broke it.
            running = 0
            trigger_index = idxs[-1]
            for i in idxs:
                running += records[i].get("cost_micros") or 0
                if running / MICROS_PER_USD > self.max_trace_cost_usd:
                    trigger_index = i
                    break

            alerts.append(
                Alert(
                    detector=self.name,
                    severity="critical",
                    kind="trace_cost_exceeded",
                    group_key=(trace_id,),
                    record_ref=trigger_index,
                    evidence={
                        "trace_id": trace_id,
                        "trace_cost_usd": trace_cost_usd,
                        "max_trace_cost_usd": self.max_trace_cost_usd,
                    },
                    message=(
                        f"trace {trace_id!r} total cost ${trace_cost_usd:.6f} "
                        f"exceeds --max-trace-cost ${self.max_trace_cost_usd:.6f}"
                    ),
                )
            )
        return alerts
