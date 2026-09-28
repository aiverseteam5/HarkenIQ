"""SM-side autonomy budget enforcement and stop switch (spec A2.2, A2.7).

SM is the middle tier: CC sets fleet-wide policy, SM enforces per-site,
agent enforces per-device.  SM maintains budget counters per action type
per SITE (S3-E1-0, A30.36) and propagates stop switch state to agents via
leases.
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

    Tracks per-action-type budget usage PER SITE. Provides budget state for
    lease issuance and for the site's reported safety state.

    S3-E1-0 (A30.36, F-3): the windows were keyed by action type alone, so
    on a Site Manager serving several sites (E1.3) an execution at site A
    drew down the window reported for site B and carried in every lease at
    B -- although this class, `SiteBudgetCounter` and the module docstring
    all describe per-site windows. They are keyed ``(site_id, action_type)``
    now. The POLICY stays per action type: Central Command pushes the
    tenant's `max_per_window` and window to every Site Manager, and each
    site gets its own window under it -- the unit the enforcer was written
    for before one Site Manager could serve several sites. A site-less
    execution cannot be recorded: attributing it to an arbitrary site would
    change that site's autonomy.
    """

    def __init__(self) -> None:
        # (site_id, action_type) -> the window for that class at that site.
        self._counters: dict[tuple[str, str], SiteBudgetCounter] = {}
        self._stop_switch: bool = False
        self._stop_switch_activated_at: Optional[float] = None
        self._stop_switch_activated_by: str = ""
        # Policy from CC (updated via PushPolicy RPC), per action type.
        self._policies: dict[str, dict] = {}

    @property
    def stop_switch_active(self) -> bool:
        return self._stop_switch

    def update_policy(self, policies: list[dict]) -> None:
        """Apply autonomy budget policies from CC.

        Each policy dict: {action_type, max_per_window, window_seconds, risk_level}

        A changed policy re-limits every site's existing window for that
        class; a site with no window yet gets one from the policy the first
        time it is read or drawn down.
        """
        for policy in policies:
            action_type = policy.get("action_type", "")
            self._policies[action_type] = policy
            for (_, counted), counter in self._counters.items():
                if counted == action_type:
                    counter.max_per_window = policy.get("max_per_window", -1)
                    counter.window_seconds = policy.get("window_seconds", 3600)

    def _counter(self, site_id: str, action_type: str) -> Optional[SiteBudgetCounter]:
        """The window for one class at one site; None when no policy bounds it."""
        if not site_id:
            raise ValueError(
                "a budget window belongs to a site; a site-less read or "
                "execution cannot be attributed (A30.36)"
            )
        policy = self._policies.get(action_type)
        if policy is None:
            return None
        key = (site_id, action_type)
        counter = self._counters.get(key)
        if counter is None:
            counter = SiteBudgetCounter(
                action_type=action_type,
                max_per_window=policy.get("max_per_window", -1),
                window_seconds=policy.get("window_seconds", 3600),
            )
            self._counters[key] = counter
        return counter

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

    def budget_for_site(self, site_id: str) -> dict[str, int]:
        """Remaining budget at ONE site: {action_type: remaining}.

        Every policy class is present (-1 = unlimited). This is what the
        site's reported safety state and every lease issued to a device at
        that site carry; nothing any other site did can move it. All agents
        at a site share its windows (per-agent budgets deferred to R3b).
        """
        return {
            action_type: self._counter(site_id, action_type).remaining
            for action_type in sorted(self._policies)
        }

    def record_execution(self, site_id: str, action_type: str) -> None:
        """Record that an action was executed at THIS site."""
        counter = self._counter(site_id, action_type)
        if counter:
            counter.record_execution()

    def allows(self, site_id: str, action_type: str) -> bool:
        """Check whether this site's budget allows this action."""
        if self._stop_switch:
            return False
        counter = self._counter(site_id, action_type)
        if counter is None:
            return True
        return counter.remaining != 0

    def site_state(self, site_id: str) -> dict[str, dict]:
        """One site's windows, for reporting: {action_type: {remaining, executions}}."""
        state = {}
        for action_type in sorted(self._policies):
            counter = self._counter(site_id, action_type)
            state[action_type] = {
                "remaining": counter.remaining,
                "executions": len(counter.executions),
            }
        return state

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
        """Return current enforcement state for reporting.

        Budget windows are per site (`budgets_by_site`), for every site
        that has a window; there is no Site Manager-wide window to report.
        """
        return {
            "stop_switch": self._stop_switch,
            "stop_switch_activated_at": self._stop_switch_activated_at,
            "stop_switch_activated_by": self._stop_switch_activated_by,
            "budgets_by_site": {
                site_id: self.site_state(site_id)
                for site_id in sorted({site for site, _ in self._counters})
            },
        }
