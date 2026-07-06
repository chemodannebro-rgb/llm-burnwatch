"""Wraps `anomaly.baseline.analyze()` as a `Detector` for the engine.

Does not change any baseline detection logic -- only converts its existing
`CallAnalysis`/`FeatureScore` results into the common `Alert` shape. Emits an
alert for every non-"ok" call, not just "anomaly": an `insufficient_data`
alert (severity "info") lets callers reconstruct the "N call(s) had
insufficient history" count from the returned `Alert`s alone, without
calling `anomaly.baseline.analyze()` a second time.
"""

from __future__ import annotations

from typing import Sequence

from ..anomaly.baseline import analyze, format_score
from ..anomaly.constants import Z_SCORE_THRESHOLD
from .protocol import Alert


class BaselineDetector:
    name = "baseline"
    enabled_by_default = True

    def __init__(self, threshold: float = Z_SCORE_THRESHOLD) -> None:
        self.threshold = threshold

    def analyze(self, records: Sequence[dict]) -> list[Alert]:
        analyses = analyze(records, threshold=self.threshold)
        alerts: list[Alert] = []
        for i, a in enumerate(analyses):
            if a.status == "insufficient_data":
                alerts.append(
                    Alert(
                        detector=self.name,
                        severity="info",
                        kind="insufficient_data",
                        group_key=a.group_key,
                        record_ref=i,
                        message="insufficient history to score this call",
                    )
                )
            elif a.status == "anomaly":
                anomalous_scores = [s for s in a.scores if s.is_anomalous]
                alerts.append(
                    Alert(
                        detector=self.name,
                        severity="warning",
                        kind="zscore_outlier",
                        group_key=a.group_key,
                        record_ref=i,
                        evidence={
                            "scores": [
                                {
                                    "feature": s.feature,
                                    "value": s.value,
                                    "median": s.median,
                                    "mad": s.mad,
                                    "z_score": s.z_score,
                                    "is_extreme": s.is_extreme,
                                    "reason": format_score(s),
                                }
                                for s in anomalous_scores
                            ]
                        },
                        message="; ".join(format_score(s) for s in anomalous_scores),
                    )
                )
        return alerts
