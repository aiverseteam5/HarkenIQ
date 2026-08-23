"""Authorization lease: SM-signed, agent-verified (spec A2.2).

Wire format: canonical_json_payload || ed25519_signature
(same pattern as licensing.py token format, without the base64url dot
separator — the proto field carries raw bytes).

Lease carries: permitted action classes, risk ceiling, budget state,
suppression domains, stop switch, and expiry timestamps.  The agent
verifies the SM signature and enforces expiry + risk-degradation locally.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from harkeniq.autonomy.identity import AgentIdentity, _canonical_json

logger = logging.getLogger("harkeniq.autonomy.lease")

# Ed25519 signature is always 64 bytes.
_SIG_LEN = 64


class InvalidLease(Exception):
    """Raised when a lease fails signature verification or is malformed."""


@dataclass
class AuthorizationLease:
    """Parsed and verified authorization lease from SM."""

    agent_id: str
    action_classes: list[str]
    risk_ceiling: str  # "none" | "low" | "medium" | "high"
    budget_remaining: dict[str, int]  # action_type -> remaining (-1 = unlimited)
    lease_expiry: float  # unix timestamp
    grace_expiry: float  # lease_expiry + grace_period
    suppression_domains: list[str]
    stop_switch: bool
    issued_at: float

    @classmethod
    def parse(cls, raw: bytes, identity: AgentIdentity) -> AuthorizationLease:
        """Parse and verify an SM-signed lease.

        ``raw`` is ``canonical_json_bytes + ed25519_signature(64 bytes)``.
        Raises InvalidLease on any failure.
        """
        if len(raw) <= _SIG_LEN:
            raise InvalidLease("Lease too short")

        payload_bytes = raw[:-_SIG_LEN]
        signature = raw[-_SIG_LEN:]

        if not identity.verify_sm_signature(payload_bytes, signature):
            raise InvalidLease("Lease signature verification failed")

        try:
            data = json.loads(payload_bytes)
        except json.JSONDecodeError as e:
            raise InvalidLease(f"Lease payload not valid JSON: {e}") from e

        if data.get("agent_id") != identity.agent_id:
            raise InvalidLease(
                f"Lease agent_id mismatch: {data.get('agent_id')} != {identity.agent_id}"
            )

        return cls(
            agent_id=data["agent_id"],
            action_classes=data.get("action_classes", []),
            risk_ceiling=data.get("risk_ceiling", "none"),
            budget_remaining=data.get("budget_remaining", {}),
            lease_expiry=float(data.get("lease_expiry", 0)),
            grace_expiry=float(data.get("grace_expiry", 0)),
            suppression_domains=data.get("suppression_domains", []),
            stop_switch=bool(data.get("stop_switch", False)),
            issued_at=float(data.get("issued_at", 0)),
        )

    def is_valid(self, now: Optional[float] = None) -> bool:
        """Lease has not expired."""
        now = now or time.time()
        return now < self.lease_expiry

    def is_in_grace(self, now: Optional[float] = None) -> bool:
        """Past expiry but within grace period."""
        now = now or time.time()
        return self.lease_expiry <= now < self.grace_expiry

    def is_fully_expired(self, now: Optional[float] = None) -> bool:
        """Past both expiry and grace period."""
        now = now or time.time()
        return now >= self.grace_expiry

    def allows_action(
        self,
        action_type: str,
        risk: str,
        sm_connected: bool,
        now: Optional[float] = None,
    ) -> str:
        """Check whether this action may execute.

        Returns:
            "execute" — proceed with the action
            "propose" — queue for SM/human review
            "deny"    — do not execute or propose
        """
        now = now or time.time()

        # Stop switch overrides everything
        if self.stop_switch:
            return "deny"

        # Fully expired -> observe-only
        if self.is_fully_expired(now):
            return "deny"

        # Grace period -> propose everything
        if self.is_in_grace(now):
            return "propose"

        # Expired (shouldn't reach here, but safety)
        if not self.is_valid(now):
            return "propose"

        # Action class not in lease
        if action_type not in self.action_classes:
            return "deny"

        # Budget exhausted for this action type
        remaining = self.budget_remaining.get(action_type, 0)
        if remaining == 0:
            return "propose"
        # -1 means unlimited

        # Risk-degraded behavior (A2.2): medium/high-risk actions
        # drop to propose-only when SM is disconnected, even with
        # a valid lease.
        _RISK_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3}
        if not sm_connected and _RISK_RANK.get(risk, 0) >= 2:
            return "propose"

        # Suppression check: if action's fault domain is suppressed
        # (suppression domain matching is done at a higher level,
        # since the lease carries domain IDs, not per-action domains)

        return "execute"


def build_lease_payload(
    agent_id: str,
    action_classes: list[str],
    risk_ceiling: str,
    budget_remaining: dict[str, int],
    lease_duration: float = 300.0,
    grace_duration: float = 60.0,
    suppression_domains: Optional[list[str]] = None,
    stop_switch: bool = False,
) -> bytes:
    """Build the canonical JSON payload for an authorization lease.

    This is called on the SM side.  The SM signs the returned bytes
    with its private key and sends payload + signature to the agent.
    """
    now = time.time()
    payload = {
        "v": 1,
        "agent_id": agent_id,
        "action_classes": sorted(action_classes),
        "risk_ceiling": risk_ceiling,
        "budget_remaining": budget_remaining,
        "lease_expiry": now + lease_duration,
        "grace_expiry": now + lease_duration + grace_duration,
        "suppression_domains": sorted(suppression_domains or []),
        "stop_switch": stop_switch,
        "issued_at": now,
    }
    return _canonical_json(payload)
