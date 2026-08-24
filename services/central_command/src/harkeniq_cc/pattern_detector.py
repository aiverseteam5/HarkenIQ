"""Fleet-wide pattern detection (R3b-3 Phase 4, R-C1).

Detects three pattern types from aggregated outcomes:
  - batch_failure: action_type X fails on vendor Y model Z above threshold
  - anomaly: failure rate for action_type X increased N-fold recently
  - reliability: component C on model M has failure_rate > fleet average

Runs periodically alongside the fleet poller.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from harkeniq_cc.outcome_aggregator import OutcomeAggregator

logger = logging.getLogger("harkeniq.cc.pattern_detector")

# Default thresholds
BATCH_FAILURE_THRESHOLD = 0.15    # 15% failure rate triggers batch pattern
ANOMALY_MULTIPLIER = 3.0          # 3x increase in failure rate triggers anomaly
MIN_SAMPLES = 5                   # minimum outcomes before pattern detection
CROSS_SITE_MIN_SITES = 2          # R-C2: failing sites needed for cross-site pattern


@dataclass
class FleetPattern:
    """A detected fleet-wide pattern."""

    pattern_id: str
    pattern_type: str  # "batch_failure" | "anomaly" | "reliability" | "cross_site_batch"
    description: str
    affected_scope: dict[str, str]  # {"vendor": "dell", "model": "R750", ...}
    confidence: float  # 0.0 to 1.0
    detected_at: float = field(default_factory=time.time)
    evidence: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def new_id() -> str:
        return f"pat-{uuid.uuid4().hex[:8]}"


class PatternDetector:
    """Detects fleet-wide patterns from aggregated outcomes."""

    def __init__(
        self,
        batch_threshold: float = BATCH_FAILURE_THRESHOLD,
        anomaly_multiplier: float = ANOMALY_MULTIPLIER,
        min_samples: int = MIN_SAMPLES,
        cross_site_min_sites: int = CROSS_SITE_MIN_SITES,
    ) -> None:
        self._batch_threshold = batch_threshold
        self._anomaly_multiplier = anomaly_multiplier
        self._min_samples = min_samples
        self._cross_site_min_sites = cross_site_min_sites
        self._detected: list[FleetPattern] = []
        self._seen_keys: set[tuple[str, str]] = set()  # (pattern_type, scope_key)

    def detect(self, aggregator: OutcomeAggregator) -> list[FleetPattern]:
        """Run all detection algorithms and return new patterns."""
        new_patterns: list[FleetPattern] = []
        new_patterns.extend(self._detect_batch_failures(aggregator))
        new_patterns.extend(self._detect_cross_site_batches(aggregator))
        new_patterns.extend(self._detect_anomalies(aggregator))
        new_patterns.extend(self._detect_reliability(aggregator))
        self._detected.extend(new_patterns)
        return new_patterns

    def _detect_cross_site_batches(
        self, agg: OutcomeAggregator
    ) -> list[FleetPattern]:
        """R4-1 (R-C2): batch failure spanning multiple sites.

        A failure pattern confined to one site is likely environmental
        (power, cooling, network). The same (action_type, vendor, model)
        failing at 2+ sites points at the hardware batch or firmware --
        the highest-value fleet signal for design partners.
        """
        patterns: list[FleetPattern] = []
        for m in agg.get_metrics():
            if m.total_count < self._min_samples:
                continue
            if m.failure_rate < self._batch_threshold:
                continue
            if m.failing_site_count < self._cross_site_min_sites:
                continue
            failing_sites = sorted(m.site_failure_counts)
            scope_key = f"cross_site:{m.action_type}:{m.vendor}:{m.model}"
            dedup_key = ("cross_site_batch", scope_key)
            if dedup_key in self._seen_keys:
                continue
            self._seen_keys.add(dedup_key)
            patterns.append(FleetPattern(
                pattern_id=FleetPattern.new_id(),
                pattern_type="cross_site_batch",
                description=(
                    f"{m.action_type} fails at {m.failure_rate:.0%} on "
                    f"{m.vendor} {m.model} across {m.failing_site_count} sites "
                    f"({m.failure_count}/{m.total_count})"
                ),
                affected_scope={
                    "action_type": m.action_type,
                    "vendor": m.vendor,
                    "model": m.model,
                    "sites": ",".join(failing_sites),
                },
                # Multi-site corroboration raises confidence over the
                # single-site batch signal.
                confidence=min(
                    1.0, m.total_count / 20 + 0.1 * m.failing_site_count
                ),
                evidence={
                    "total": m.total_count,
                    "failures": m.failure_count,
                    "failure_rate": round(m.failure_rate, 3),
                    "site_failure_counts": dict(m.site_failure_counts),
                    "sites_affected": m.failing_site_count,
                },
            ))
            logger.warning(
                "Cross-site batch failure detected: %s", patterns[-1].description
            )
        return patterns

    def _detect_batch_failures(self, agg: OutcomeAggregator) -> list[FleetPattern]:
        """Detect: action_type X fails on vendor Y model Z above threshold."""
        patterns: list[FleetPattern] = []
        for m in agg.get_metrics():
            if m.total_count < self._min_samples:
                continue
            if m.failure_rate < self._batch_threshold:
                continue
            scope_key = f"batch:{m.action_type}:{m.vendor}:{m.model}"
            dedup_key = ("batch_failure", scope_key)
            if dedup_key in self._seen_keys:
                continue
            self._seen_keys.add(dedup_key)
            patterns.append(FleetPattern(
                pattern_id=FleetPattern.new_id(),
                pattern_type="batch_failure",
                description=(
                    f"{m.action_type} fails at {m.failure_rate:.0%} on "
                    f"{m.vendor} {m.model} ({m.failure_count}/{m.total_count})"
                ),
                affected_scope={
                    "action_type": m.action_type,
                    "vendor": m.vendor,
                    "model": m.model,
                },
                confidence=min(1.0, m.total_count / 20),  # higher N = higher confidence
                evidence={
                    "total": m.total_count,
                    "failures": m.failure_count,
                    "failure_rate": round(m.failure_rate, 3),
                    "success_rate": round(m.success_rate, 3),
                },
            ))
            logger.warning("Batch failure detected: %s", patterns[-1].description)
        return patterns

    def _detect_anomalies(self, agg: OutcomeAggregator) -> list[FleetPattern]:
        """Detect: failure rate increased N-fold recently."""
        patterns: list[FleetPattern] = []
        for m in agg.get_metrics():
            if m.total_count < self._min_samples:
                continue
            key = (m.action_type, m.vendor, m.model)
            trend = agg.get_trend(key)
            if trend is None:
                continue
            if trend <= 0:
                continue
            # Check if trend represents a significant increase
            baseline_rate = max(0.01, m.failure_rate - trend)
            if m.failure_rate / baseline_rate >= self._anomaly_multiplier:
                scope_key = f"anomaly:{m.action_type}:{m.vendor}:{m.model}"
                dedup_key = ("anomaly", scope_key)
                if dedup_key in self._seen_keys:
                    continue
                self._seen_keys.add(dedup_key)
                patterns.append(FleetPattern(
                    pattern_id=FleetPattern.new_id(),
                    pattern_type="anomaly",
                    description=(
                        f"{m.action_type} failure rate increased {trend:+.1%} "
                        f"on {m.vendor} {m.model}"
                    ),
                    affected_scope={
                        "action_type": m.action_type,
                        "vendor": m.vendor,
                        "model": m.model,
                    },
                    confidence=0.7,
                    evidence={
                        "current_failure_rate": round(m.failure_rate, 3),
                        "trend": round(trend, 3),
                    },
                ))
        return patterns

    def _detect_reliability(self, agg: OutcomeAggregator) -> list[FleetPattern]:
        """Detect: specific model has failure rate above fleet average."""
        patterns: list[FleetPattern] = []
        # Group by action_type to compare across vendors/models
        by_action: dict[str, list] = {}
        for m in agg.get_metrics():
            if m.total_count < self._min_samples:
                continue
            by_action.setdefault(m.action_type, []).append(m)

        for action_type, metrics in by_action.items():
            if len(metrics) < 2:
                continue
            fleet_rate = agg.get_fleet_success_rate(action_type)
            for m in metrics:
                # Model-specific failure rate significantly worse than fleet average
                if m.failure_rate > (1 - fleet_rate) * 2 and m.failure_rate > 0.1:
                    scope_key = f"reliability:{m.action_type}:{m.vendor}:{m.model}"
                    dedup_key = ("reliability", scope_key)
                    if dedup_key in self._seen_keys:
                        continue
                    self._seen_keys.add(dedup_key)
                    patterns.append(FleetPattern(
                        pattern_id=FleetPattern.new_id(),
                        pattern_type="reliability",
                        description=(
                            f"{m.vendor} {m.model} has {m.failure_rate:.0%} failure rate "
                            f"for {m.action_type} vs {1 - fleet_rate:.0%} fleet average"
                        ),
                        affected_scope={
                            "action_type": m.action_type,
                            "vendor": m.vendor,
                            "model": m.model,
                        },
                        confidence=min(1.0, m.total_count / 30),
                        evidence={
                            "model_failure_rate": round(m.failure_rate, 3),
                            "fleet_failure_rate": round(1 - fleet_rate, 3),
                            "model_total": m.total_count,
                        },
                    ))
        return patterns

    @property
    def all_patterns(self) -> list[FleetPattern]:
        return list(self._detected)

    @property
    def pattern_count(self) -> int:
        return len(self._detected)
