"""SM Knowledge Base: past incidents, resolutions, outcomes (R3a, A2.7).

Stores action outcomes, tracks success rates per action type, and
provides history lookup for the SM reasoning engine.  This is what
makes the SM an "intelligence hub" (Platform-Design) rather than just
a correlator.

R3a: store outcomes + history lookup + error budget.
R3b: add KnowledgeBaseReasoner that queries this for "has this been seen before?"
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("harkeniq.sm.knowledge")


@dataclass
class StoredOutcome:
    """An action outcome stored in the knowledge base."""

    action_id: str
    action_type: str
    device_id: str
    outcome: str          # SUCCESS, PARTIAL, FAILURE, UNKNOWN, ROLLBACK_TRIGGERED
    fault_resolved: Optional[bool] = None
    pre_state: dict[str, Any] = field(default_factory=dict)
    post_state: dict[str, Any] = field(default_factory=dict)
    side_effects: list[str] = field(default_factory=list)
    operator_override: bool = False
    override_reason: str = ""
    recorded_at: float = field(default_factory=time.time)


@dataclass
class ErrorBudgetState:
    """Error budget for one action type (A2.2 drop-back model)."""

    action_type: str
    success_count: int = 0
    failure_count: int = 0
    total_count: int = 0
    min_success_rate: float = 0.95  # below this -> drop back to Approve
    dropped_back: bool = False
    dropped_back_at: Optional[float] = None

    @property
    def success_rate(self) -> float:
        if self.total_count == 0:
            return 1.0  # no data yet = assume good
        return self.success_count / self.total_count

    @property
    def should_drop_back(self) -> bool:
        """True if success rate is below threshold and we have enough data."""
        if self.total_count < 5:
            return False  # not enough data to judge
        return self.success_rate < self.min_success_rate


class KnowledgeBase:
    """SM knowledge base: outcomes, error budgets, history lookup.

    In-memory for R3a.  R3b migrates to PostgreSQL tables
    (sm_action_outcomes, sm_knowledge_incidents, sm_error_budget).
    """

    def __init__(self, min_success_rate: float = 0.95) -> None:
        self._outcomes: list[StoredOutcome] = []
        self._error_budgets: dict[str, ErrorBudgetState] = {}
        self._min_success_rate = min_success_rate
        # Index: device_id -> list of outcomes (for history lookup)
        self._device_outcomes: dict[str, list[StoredOutcome]] = defaultdict(list)

    def record_outcome(self, outcome: StoredOutcome) -> None:
        """Record an action outcome and update error budget."""
        self._outcomes.append(outcome)
        self._device_outcomes[outcome.device_id].append(outcome)

        # Update error budget
        budget = self._error_budgets.get(outcome.action_type)
        if budget is None:
            budget = ErrorBudgetState(
                action_type=outcome.action_type,
                min_success_rate=self._min_success_rate,
            )
            self._error_budgets[outcome.action_type] = budget

        budget.total_count += 1
        if outcome.outcome == "SUCCESS":
            budget.success_count += 1
        elif outcome.outcome in ("FAILURE", "ROLLBACK_TRIGGERED"):
            budget.failure_count += 1
            if budget.should_drop_back and not budget.dropped_back:
                budget.dropped_back = True
                budget.dropped_back_at = time.time()
                logger.warning(
                    "Error budget drop-back: %s success rate %.1f%% < %.1f%% "
                    "threshold (%d/%d). Autonomy revoked for this action type.",
                    outcome.action_type,
                    budget.success_rate * 100,
                    budget.min_success_rate * 100,
                    budget.success_count,
                    budget.total_count,
                )

        # Trim old outcomes (keep last 1000 per device)
        device_list = self._device_outcomes[outcome.device_id]
        if len(device_list) > 1000:
            self._device_outcomes[outcome.device_id] = device_list[-500:]

    def is_action_type_dropped_back(self, action_type: str) -> bool:
        """Check if an action type has been revoked due to error budget."""
        budget = self._error_budgets.get(action_type)
        if budget is None:
            return False
        return budget.dropped_back

    def get_error_budget(self, action_type: str) -> Optional[ErrorBudgetState]:
        """Get error budget state for an action type."""
        return self._error_budgets.get(action_type)

    def get_device_history(
        self, device_id: str, action_type: Optional[str] = None, limit: int = 10
    ) -> list[StoredOutcome]:
        """Look up past outcomes for a device.

        This is the "has this been seen before?" capability that makes
        the SM a reasoning engine, not just a correlator.
        """
        outcomes = self._device_outcomes.get(device_id, [])
        if action_type:
            outcomes = [o for o in outcomes if o.action_type == action_type]
        return outcomes[-limit:]

    def recover_error_budget(self, action_type: str) -> bool:
        """Manually recover an action type from error budget drop-back.

        Called when an operator reviews the failures and re-enables autonomy.
        Returns True if recovery was applied.
        """
        budget = self._error_budgets.get(action_type)
        if budget is None or not budget.dropped_back:
            return False
        budget.dropped_back = False
        budget.dropped_back_at = None
        # Reset counters for a fresh evaluation period
        budget.success_count = 0
        budget.failure_count = 0
        budget.total_count = 0
        logger.info(
            "Error budget recovered for %s: counters reset, autonomy re-enabled",
            action_type,
        )
        return True

    def get_all_budgets(self) -> dict[str, dict]:
        """Return all error budget states for reporting."""
        return {
            at: {
                "success_rate": f"{b.success_rate:.1%}",
                "total": b.total_count,
                "success": b.success_count,
                "failure": b.failure_count,
                "dropped_back": b.dropped_back,
            }
            for at, b in self._error_budgets.items()
        }

    @property
    def total_outcomes(self) -> int:
        return len(self._outcomes)
