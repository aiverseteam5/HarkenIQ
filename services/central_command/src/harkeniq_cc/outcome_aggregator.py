"""Outcome aggregation for fleet-wide learning (R3b-3 Phase 4, R-C1).

Groups action outcomes by (action_type, vendor, model) and computes
success rates, failure rates, and trends. Source data comes from
CCOutcomeHistory populated by the fleet poller.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("harkeniq.cc.aggregator")


@dataclass
class AggregateMetrics:
    """Aggregated metrics for a (action_type, vendor, model) group."""

    action_type: str
    vendor: str
    model: str
    total_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    partial_count: int = 0
    fault_resolved_count: int = 0

    @property
    def success_rate(self) -> float:
        if self.total_count == 0:
            return 1.0
        return self.success_count / self.total_count

    @property
    def failure_rate(self) -> float:
        if self.total_count == 0:
            return 0.0
        return self.failure_count / self.total_count

    @property
    def resolution_rate(self) -> float:
        if self.total_count == 0:
            return 0.0
        return self.fault_resolved_count / self.total_count


class OutcomeAggregator:
    """Aggregates action outcomes by scope for fleet intelligence."""

    def __init__(self) -> None:
        # Keyed by (action_type, vendor, model)
        self._metrics: dict[tuple[str, str, str], AggregateMetrics] = {}
        # Historical snapshots for trend detection
        self._history: list[dict[tuple[str, str, str], AggregateMetrics]] = []

    def ingest(self, outcomes: list[dict]) -> int:
        """Ingest a batch of outcome dicts and update aggregates.

        Each outcome dict has: action_type, vendor, model, outcome,
        fault_resolved (optional).

        Returns number of outcomes ingested.
        """
        count = 0
        for oc in outcomes:
            action_type = oc.get("action_type", "")
            vendor = oc.get("vendor", "")
            model = oc.get("model", "")
            outcome = oc.get("outcome", "UNKNOWN")
            fault_resolved = oc.get("fault_resolved", False)

            key = (action_type, vendor, model)
            if key not in self._metrics:
                self._metrics[key] = AggregateMetrics(
                    action_type=action_type, vendor=vendor, model=model,
                )
            m = self._metrics[key]
            m.total_count += 1
            if outcome == "SUCCESS":
                m.success_count += 1
            elif outcome in ("FAILURE", "ROLLBACK"):
                m.failure_count += 1
            elif outcome == "PARTIAL":
                m.partial_count += 1
            if fault_resolved:
                m.fault_resolved_count += 1
            count += 1
        return count

    def get_metrics(
        self,
        action_type: Optional[str] = None,
        vendor: Optional[str] = None,
    ) -> list[AggregateMetrics]:
        """Get aggregate metrics, optionally filtered."""
        result = []
        for key, m in self._metrics.items():
            if action_type and m.action_type != action_type:
                continue
            if vendor and m.vendor != vendor:
                continue
            result.append(m)
        return sorted(result, key=lambda m: m.total_count, reverse=True)

    def get_fleet_success_rate(self, action_type: str) -> float:
        """Compute fleet-wide success rate for an action type."""
        total = 0
        success = 0
        for key, m in self._metrics.items():
            if m.action_type == action_type:
                total += m.total_count
                success += m.success_count
        if total == 0:
            return 1.0
        return success / total

    def snapshot(self) -> None:
        """Save current state as a historical snapshot for trend detection."""
        import copy
        self._history.append(copy.deepcopy(self._metrics))

    def get_trend(self, key: tuple[str, str, str], window: int = 3) -> Optional[float]:
        """Get failure rate trend over last N snapshots.

        Returns the change in failure rate (positive = increasing failures).
        Returns None if insufficient history.
        """
        if len(self._history) < window:
            return None
        recent = self._history[-window:]
        rates = []
        for snap in recent:
            m = snap.get(key)
            if m:
                rates.append(m.failure_rate)
            else:
                rates.append(0.0)
        if len(rates) < 2:
            return None
        return rates[-1] - rates[0]

    @property
    def group_count(self) -> int:
        return len(self._metrics)
