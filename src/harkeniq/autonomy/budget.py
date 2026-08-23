"""Agent-local autonomy budget enforcement (spec A2.2, A2.7 contract 4).

The distributed enforcement model: CC sets policy, SM enforces site-wide,
agent enforces device-local.  This module handles the agent tier.

Budget state arrives in the authorization lease from SM.  The agent tracks
local execution counts and refuses actions when the budget is exhausted.
Budget counters reset on each new lease (SM is the source of truth for
remaining budget).
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from harkeniq.models import ActionType

logger = logging.getLogger("harkeniq.autonomy.budget")


@dataclass
class BudgetEntry:
    """Budget state for one action type on this device."""

    action_type: ActionType
    remaining: int         # -1 = unlimited, 0 = exhausted
    used_this_period: int = 0
    last_reset: float = 0.0


class AgentBudgetEnforcer:
    """Agent-local autonomy budget enforcement.

    Budget limits arrive from the SM via the authorization lease.
    The agent tracks local usage and refuses actions when exhausted.
    On each lease renewal, budget_remaining is refreshed from SM.
    """

    def __init__(self) -> None:
        self._budgets: dict[str, BudgetEntry] = {}
        self._stop_switch: bool = False

    @property
    def stop_switch_active(self) -> bool:
        return self._stop_switch

    def update_from_lease(
        self, budget_remaining: dict[str, int], stop_switch: bool
    ) -> None:
        """Refresh budget state from a new lease.

        Called on every heartbeat ack when a valid lease is received.
        SM is the authoritative source for remaining budget.
        """
        self._stop_switch = stop_switch
        for action_str, remaining in budget_remaining.items():
            try:
                action_type = ActionType(action_str)
            except ValueError:
                continue
            entry = self._budgets.get(action_str)
            if entry is None:
                self._budgets[action_str] = BudgetEntry(
                    action_type=action_type,
                    remaining=remaining,
                    last_reset=time.time(),
                )
            else:
                entry.remaining = remaining

    def allows(self, action_type: ActionType) -> bool:
        """Check whether budget permits this action.

        Returns False if:
        - Stop switch is active (denies everything)
        - Budget for this action type is exhausted (remaining == 0)

        Returns True if:
        - No budget entry exists (action not budget-gated)
        - Budget remaining is -1 (unlimited)
        - Budget remaining > 0
        """
        if self._stop_switch:
            return False

        entry = self._budgets.get(action_type.value)
        if entry is None:
            return True  # not budget-gated
        if entry.remaining == -1:
            return True  # unlimited
        return entry.remaining > 0

    def consume(self, action_type: ActionType) -> None:
        """Record that an action was executed, decrementing the budget.

        Call this AFTER successful action execution.
        """
        entry = self._budgets.get(action_type.value)
        if entry is None or entry.remaining == -1:
            return
        entry.remaining = max(0, entry.remaining - 1)
        entry.used_this_period += 1
        if entry.remaining == 0:
            logger.info(
                "Budget exhausted for %s (used %d this period)",
                action_type.value, entry.used_this_period,
            )

    def get_state(self) -> dict[str, dict]:
        """Return current budget state for reporting."""
        return {
            action_str: {
                "remaining": entry.remaining,
                "used": entry.used_this_period,
            }
            for action_str, entry in self._budgets.items()
        }

    def activate_stop_switch(self) -> None:
        """Emergency halt: deny all autonomous actions."""
        self._stop_switch = True
        logger.warning("Stop switch ACTIVATED: all autonomous actions denied")

    def deactivate_stop_switch(self) -> None:
        """Resume normal operation after stop switch."""
        self._stop_switch = False
        logger.info("Stop switch deactivated: normal operation resumed")
