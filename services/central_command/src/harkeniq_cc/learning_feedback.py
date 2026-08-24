"""Learning feedback loop tracker (R3b-3 Phase 7, R-C1 complete).

Tracks the full R-C1 feedback loop:
  outcome gathered → pattern detected → skill generated →
  distributed → applied → new outcomes tracked

Metrics: "skill X reduced failure rate of action Y by Z% across N devices"
Promotion criteria: success_rate > 95% across 50+ devices → auto-promote.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("harkeniq.cc.learning_feedback")

# Promotion thresholds
PROMOTION_SUCCESS_RATE = 0.95
PROMOTION_MIN_DEVICES = 50


@dataclass
class LearningCycleEntry:
    """Tracks one iteration of the R-C1 learning loop."""

    cycle_id: str
    pattern_id: str
    pattern_type: str
    skill_id: Optional[str] = None
    sites_distributed: int = 0
    devices_applied: int = 0
    outcomes_before: dict[str, Any] = field(default_factory=dict)
    outcomes_after: dict[str, Any] = field(default_factory=dict)
    improvement_pct: Optional[float] = None
    promoted: bool = False
    started_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None


class LearningFeedbackTracker:
    """Tracks the R-C1 fleet learning feedback loop.

    The loop:
    1. Outcomes gathered from agents via SM → CC
    2. Patterns detected by PatternDetector
    3. Skills generated (or suggested) from patterns
    4. Skills distributed via KnowledgeDistributor → SM → Agent
    5. New outcomes tracked to measure improvement
    """

    def __init__(
        self,
        promotion_success_rate: float = PROMOTION_SUCCESS_RATE,
        promotion_min_devices: int = PROMOTION_MIN_DEVICES,
    ) -> None:
        self._promotion_rate = promotion_success_rate
        self._promotion_min = promotion_min_devices
        self._cycles: dict[str, LearningCycleEntry] = {}
        self._promotions: list[str] = []  # skill_ids auto-promoted

    def start_cycle(
        self,
        cycle_id: str,
        pattern_id: str,
        pattern_type: str,
        baseline_metrics: dict[str, Any],
    ) -> LearningCycleEntry:
        """Start tracking a learning cycle from a detected pattern."""
        entry = LearningCycleEntry(
            cycle_id=cycle_id,
            pattern_id=pattern_id,
            pattern_type=pattern_type,
            outcomes_before=baseline_metrics,
        )
        self._cycles[cycle_id] = entry
        logger.info("Learning cycle started: %s (pattern=%s)", cycle_id, pattern_id)
        return entry

    def record_skill_generated(self, cycle_id: str, skill_id: str) -> None:
        """Record that a skill was generated from this pattern."""
        entry = self._cycles.get(cycle_id)
        if entry:
            entry.skill_id = skill_id

    def record_distribution(
        self, cycle_id: str, sites: int, devices: int,
    ) -> None:
        """Record that the skill was distributed to sites/devices."""
        entry = self._cycles.get(cycle_id)
        if entry:
            entry.sites_distributed = sites
            entry.devices_applied = devices

    def record_outcomes(
        self, cycle_id: str, new_metrics: dict[str, Any],
    ) -> Optional[float]:
        """Record post-distribution outcomes and compute improvement.

        Returns improvement percentage (positive = better), or None
        if insufficient data.
        """
        entry = self._cycles.get(cycle_id)
        if entry is None:
            return None

        entry.outcomes_after = new_metrics
        before_rate = entry.outcomes_before.get("failure_rate", 0.0)
        after_rate = new_metrics.get("failure_rate", 0.0)

        if before_rate > 0:
            entry.improvement_pct = ((before_rate - after_rate) / before_rate) * 100
        else:
            entry.improvement_pct = 0.0

        entry.completed_at = time.time()
        logger.info(
            "Learning cycle %s: improvement=%.1f%% (%.1f%% → %.1f%%)",
            cycle_id, entry.improvement_pct, before_rate * 100, after_rate * 100,
        )
        return entry.improvement_pct

    def check_promotion(self, cycle_id: str) -> bool:
        """Check if the skill from this cycle should be auto-promoted.

        Promotion criteria: success_rate > threshold across min_devices.
        """
        entry = self._cycles.get(cycle_id)
        if entry is None or entry.skill_id is None:
            return False

        success_rate = entry.outcomes_after.get("success_rate", 0.0)
        devices = entry.devices_applied

        if success_rate >= self._promotion_rate and devices >= self._promotion_min:
            entry.promoted = True
            self._promotions.append(entry.skill_id)
            logger.info(
                "Skill %s auto-promoted: success_rate=%.1f%% devices=%d",
                entry.skill_id, success_rate * 100, devices,
            )
            return True
        return False

    def get_cycle(self, cycle_id: str) -> Optional[LearningCycleEntry]:
        return self._cycles.get(cycle_id)

    def get_active_cycles(self) -> list[LearningCycleEntry]:
        return [c for c in self._cycles.values() if c.completed_at is None]

    def get_completed_cycles(self) -> list[LearningCycleEntry]:
        return [c for c in self._cycles.values() if c.completed_at is not None]

    @property
    def total_cycles(self) -> int:
        return len(self._cycles)

    @property
    def promotions(self) -> list[str]:
        return list(self._promotions)
