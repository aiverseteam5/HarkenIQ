"""SM-side autonomy budget enforcement and stop switch (spec A2.2, A2.7).

SM is the middle tier: CC sets fleet-wide policy, SM enforces per-site,
agent enforces per-device.  SM maintains budget counters per action type
per fault domain and propagates stop switch state to agents via leases.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("harkeniq.sm.autonomy")


@dataclass
class SiteBudgetCounter:
    """Budget counter for one action type at one site."""

    action_type: str
    max_per_window: int
    window_seconds: float
    executions: list[float] = field(default_factory=list)  # timestamps

    @property
    def remaining(self) -> int:
        if self.max_per_window < 0:
            return -1  # unlimited
        now = time.time()
        recent = [t for t in self.executions if (now - t) < self.window_seconds]
        self.executions = recent  # trim old entries
        return max(0, self.max_per_window - len(recent))

    def record_execution(self) -> None:
        self.executions.append(time.time())


class SMAutonomyEnforcer:
    """Site Manager autonomy enforcement.

    Tracks per-action-type budget usage across the site.  Provides budget
    state for lease issuance and enforces site-wide limits.
    """

    def __init__(self) -> None:
        self._counters: dict[str, SiteBudgetCounter] = {}
        self._stop_switch: bool = False
        self._stop_switch_activated_at: Optional[float] = None
        self._stop_switch_activated_by: str = ""
        # Policy from CC (updated via PushPolicy RPC)
        self._policies: dict[str, dict] = {}

    @property
    def stop_switch_active(self) -> bool:
        return self._stop_switch

    def update_policy(self, policies: list[dict]) -> None:
        """Apply autonomy budget policies from CC.

        Each policy dict: {action_type, max_per_window, window_seconds, risk_level}
        """
        for policy in policies:
            action_type = policy.get("action_type", "")
            self._policies[action_type] = policy
            if action_type not in self._counters:
                self._counters[action_type] = SiteBudgetCounter(
                    action_type=action_type,
                    max_per_window=policy.get("max_per_window", -1),
                    window_seconds=policy.get("window_seconds", 3600),
                )
            else:
                counter = self._counters[action_type]
                counter.max_per_window = policy.get("max_per_window", -1)
                counter.window_seconds = policy.get("window_seconds", 3600)

    def policy_actions(self) -> dict[str, str]:
        """CC-granted action classes: {action_type: risk_level}.

        Used at lease issuance (QA-021): a CC budget policy for an action
        type both grants the class and bounds it.
        """
        return {
            action_type: policy.get("risk_level", "low")
            for action_type, policy in self._policies.items()
            if action_type and action_type != "*"
        }

    def get_budget_for_agent(self, agent_id: str) -> dict[str, int]:
        """Compute budget remaining for an agent's lease.

        Returns {action_type: remaining_count} for all tracked action types.
        Currently site-wide counters (not per-agent), so all agents at the
        site share the budget.  Per-agent budgets deferred to R3b.
        """
        result = {}
        for action_type, counter in self._counters.items():
            result[action_type] = counter.remaining
        return result

    def record_execution(self, action_type: str) -> None:
        """Record that an action was executed at this site."""
        counter = self._counters.get(action_type)
        if counter:
            counter.record_execution()

    def allows_site_wide(self, action_type: str) -> bool:
        """Check whether site-wide budget allows this action."""
        if self._stop_switch:
            return False
        counter = self._counters.get(action_type)
        if counter is None:
            return True
        return counter.remaining != 0

    def activate_stop_switch(self, activated_by: str = "operator") -> None:
        """Fleet-wide halt: deny all autonomous actions at this site."""
        self._stop_switch = True
        self._stop_switch_activated_at = time.time()
        self._stop_switch_activated_by = activated_by
        logger.warning(
            "Stop switch ACTIVATED by %s at site level", activated_by
        )

    def deactivate_stop_switch(self, deactivated_by: str = "operator") -> None:
        """Resume normal operation."""
        self._stop_switch = False
        logger.info(
            "Stop switch deactivated by %s at site level", deactivated_by
        )

    def get_state(self) -> dict:
        """Return current enforcement state for reporting."""
        return {
            "stop_switch": self._stop_switch,
            "stop_switch_activated_at": self._stop_switch_activated_at,
            "stop_switch_activated_by": self._stop_switch_activated_by,
            "budgets": {
                at: {"remaining": c.remaining, "executions": len(c.executions)}
                for at, c in self._counters.items()
            },
        }
