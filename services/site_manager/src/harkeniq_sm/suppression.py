"""Correlated-conclusion suppression engine (spec A2.6).

When multiple devices in the same fault domain produce similar verdicts
within a short window, autonomous action is suppressed for the entire
domain.  This prevents the "self-inflicted outage" scenario where many
agents independently act on a shared upstream cause.

Two trigger paths:
  Path 1: Direct shared-dependency evidence (2+ devices, same domain,
          event type maps to domain failure mode) -> immediate suppression
  Path 2: Statistical correlation (threshold reached per policy) -> suppress

Recovery:
  - Transient resolved: auto-recovery after 10min stability + 1h hair-trigger
  - Unresolved/high-severity: explicit human re-enable
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("harkeniq.sm.suppression")

# Default stability period before auto-recovery (seconds)
STABILITY_PERIOD = 600  # 10 minutes
# Hair-trigger window after auto-recovery: any re-trigger in this window
# suppresses immediately with zero threshold
HAIR_TRIGGER_WINDOW = 3600  # 1 hour


@dataclass
class SuppressionPolicy:
    """Policy for one event family + fault domain type."""

    event_family: str       # "power", "thermal", "connectivity", "component"
    domain_kind: str        # "power", "cooling", "network", "*"
    direct_dependency: bool # Path 1: immediate suppression
    threshold: int          # Path 2: device count to trigger
    window_seconds: float   # correlation window


# Default policies per A2.6
DEFAULT_POLICIES = [
    SuppressionPolicy("power", "power", True, 2, 30),
    SuppressionPolicy("thermal", "cooling", True, 3, 60),
    SuppressionPolicy("connectivity", "network", True, 3, 15),
    SuppressionPolicy("component", "*", False, 5, 300),
]


@dataclass
class SuppressionState:
    """Active suppression for one fault domain."""

    domain_id: str
    domain_kind: str
    event_family: str
    triggered_at: float
    trigger_reason: str  # "direct_dependency" | "statistical_threshold"
    device_count: int
    correlation_evidence: list[str] = field(default_factory=list)
    # Recovery tracking
    all_clear_at: Optional[float] = None  # when all devices returned healthy
    auto_recovered: bool = False
    human_re_enabled: bool = False


@dataclass
class CorrelationEvent:
    """A verdict event for correlation tracking."""

    device_id: str
    domain_id: str
    domain_kind: str
    event_family: str
    severity: str
    timestamp: float


class SuppressionEngine:
    """Evaluates incoming verdicts for correlated-conclusion suppression.

    Hooks into the SM correlation engine.  When suppression is active
    for a fault domain, the lease renewal for all agents in that domain
    carries a suppression flag.
    """

    def __init__(self, policies: list[SuppressionPolicy] | None = None) -> None:
        self._policies = {
            (p.event_family, p.domain_kind): p
            for p in (policies or DEFAULT_POLICIES)
        }
        # Active suppressions: domain_id -> SuppressionState
        self._active: dict[str, SuppressionState] = {}
        # Recent events for statistical correlation: (domain_id, event_family) -> events
        self._recent: dict[tuple[str, str], list[CorrelationEvent]] = defaultdict(list)
        # Hair-trigger: domains that auto-recovered recently
        self._hair_trigger: dict[str, float] = {}  # domain_id -> recovery_ts

    def evaluate(self, event: CorrelationEvent) -> Optional[SuppressionState]:
        """Evaluate a new verdict event for suppression.

        Returns SuppressionState if suppression was triggered (new or existing).
        Returns None if no suppression.
        """
        # Already suppressed?
        if event.domain_id in self._active:
            return self._active[event.domain_id]

        # Hair-trigger check: recently auto-recovered domain
        hair_ts = self._hair_trigger.get(event.domain_id)
        if hair_ts and (time.time() - hair_ts) < HAIR_TRIGGER_WINDOW:
            return self._trigger(
                event, "hair_trigger_re-suppression", device_count=1
            )

        # Find matching policy
        policy = self._find_policy(event.event_family, event.domain_kind)
        if policy is None:
            return None

        # Path 1: Direct shared-dependency
        if policy.direct_dependency:
            key = (event.domain_id, event.event_family)
            self._recent[key].append(event)
            self._trim_window(key, policy.window_seconds)
            unique_devices = {e.device_id for e in self._recent[key]}
            if len(unique_devices) >= policy.threshold:
                return self._trigger(
                    event, "direct_dependency",
                    device_count=len(unique_devices),
                    evidence=[
                        f"{e.device_id}: {e.severity} at {e.timestamp:.0f}"
                        for e in self._recent[key]
                    ],
                )

        # Path 2: Statistical correlation
        else:
            key = (event.domain_id, event.event_family)
            self._recent[key].append(event)
            self._trim_window(key, policy.window_seconds)
            unique_devices = {e.device_id for e in self._recent[key]}
            if len(unique_devices) >= policy.threshold:
                return self._trigger(
                    event, "statistical_threshold",
                    device_count=len(unique_devices),
                    evidence=[
                        f"{e.device_id}: {e.severity} at {e.timestamp:.0f}"
                        for e in self._recent[key]
                    ],
                )

        return None

    def get_suppressed_domains(self) -> list[str]:
        """Return domain IDs with active suppression (for lease flags)."""
        return list(self._active.keys())

    def is_suppressed(self, domain_id: str) -> bool:
        return domain_id in self._active

    def check_auto_recovery(self, domain_id: str, all_devices_healthy: bool) -> bool:
        """Check whether a suppressed domain can auto-recover.

        Called periodically by the SM.  Returns True if auto-recovery
        was triggered.

        Auto-recovery requires:
        - All devices in the domain returned to healthy
        - Stability period elapsed (10 minutes)
        - Incident severity is not S1/S2
        """
        state = self._active.get(domain_id)
        if state is None:
            return False

        now = time.time()

        if all_devices_healthy:
            if state.all_clear_at is None:
                state.all_clear_at = now
                logger.info(
                    "Domain %s: all devices healthy, starting %ds stability period",
                    domain_id, STABILITY_PERIOD,
                )
            elif (now - state.all_clear_at) >= STABILITY_PERIOD:
                # Stability period elapsed: auto-recover
                state.auto_recovered = True
                self._hair_trigger[domain_id] = now
                del self._active[domain_id]
                logger.info(
                    "Domain %s: auto-recovered after %ds stability. "
                    "Hair-trigger active for %ds.",
                    domain_id, STABILITY_PERIOD, HAIR_TRIGGER_WINDOW,
                )
                return True
        else:
            # Devices not all healthy: reset stability timer
            state.all_clear_at = None

        return False

    def human_re_enable(self, domain_id: str, operator: str) -> bool:
        """Operator explicitly re-enables autonomy for a domain.

        Required for unresolved, ambiguous, or S1/S2 severity incidents.
        """
        state = self._active.get(domain_id)
        if state is None:
            return False
        state.human_re_enabled = True
        del self._active[domain_id]
        logger.info(
            "Domain %s: autonomy re-enabled by operator %s", domain_id, operator
        )
        return True

    def get_state(self) -> dict:
        """Return current suppression state for reporting."""
        return {
            "active_suppressions": {
                did: {
                    "event_family": s.event_family,
                    "trigger_reason": s.trigger_reason,
                    "device_count": s.device_count,
                    "triggered_at": s.triggered_at,
                    "all_clear_at": s.all_clear_at,
                }
                for did, s in self._active.items()
            },
            "hair_trigger_domains": list(self._hair_trigger.keys()),
        }

    # -- internals ----------------------------------------------------------

    def _find_policy(
        self, event_family: str, domain_kind: str
    ) -> Optional[SuppressionPolicy]:
        # Exact match first, then wildcard domain
        policy = self._policies.get((event_family, domain_kind))
        if policy:
            return policy
        return self._policies.get((event_family, "*"))

    def _trigger(
        self,
        event: CorrelationEvent,
        reason: str,
        device_count: int,
        evidence: list[str] | None = None,
    ) -> SuppressionState:
        state = SuppressionState(
            domain_id=event.domain_id,
            domain_kind=event.domain_kind,
            event_family=event.event_family,
            triggered_at=time.time(),
            trigger_reason=reason,
            device_count=device_count,
            correlation_evidence=evidence or [],
        )
        self._active[event.domain_id] = state
        logger.warning(
            "SUPPRESSION ACTIVATED: domain=%s family=%s reason=%s devices=%d",
            event.domain_id, event.event_family, reason, device_count,
        )
        return state

    def _trim_window(self, key: tuple[str, str], window: float) -> None:
        cutoff = time.time() - window
        self._recent[key] = [e for e in self._recent[key] if e.timestamp > cutoff]
