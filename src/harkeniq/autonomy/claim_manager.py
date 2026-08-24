"""Claim ownership manager (R3b-2 Phase 4, spec R-M15 through R-M19).

ClaimManager is the stateful arbiter for incident ownership in the mesh.
It processes inbound claims, applies the deterministic tiebreak, manages
claim leases, and handles lapse/inheritance.

Key invariants:
  - R-M15: First-claim wins.  Ties broken by lower agent_id (deterministic).
  - R-M16: One active claim per subject_device_id.
  - R-M17: Lapsed lease returns incident to claimable with inherited evidence.
  - R-M18: Owner owns investigation, not resolution (resolution is SM/CC).
  - R-M19: Isolated node cannot claim (enforced by ClaimExchange).
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from harkeniq.autonomy.claim import (
    Claim,
    ClaimLease,
    DEFAULT_CLAIM_LEASE_DURATION,
    deterministic_tiebreak,
)
from harkeniq.models import ClaimStatus

logger = logging.getLogger("harkeniq.autonomy.claim_manager")


class ClaimManager:
    """Stateful arbiter for incident claim ownership."""

    def __init__(
        self,
        my_agent_id: str,
        claim_lease_duration: float = DEFAULT_CLAIM_LEASE_DURATION,
    ) -> None:
        self._my_id = my_agent_id
        self._lease_duration = claim_lease_duration

        # Active claims by subject_device_id (one claim per device)
        self._active_claims: dict[str, Claim] = {}

        # Active leases by claim_id
        self._leases: dict[str, ClaimLease] = {}

        # Lapsed evidence by subject_device_id (for inheritance on re-claim)
        self._lapsed_evidence: dict[str, list[dict]] = {}

        # Resolved subjects (to prevent re-claiming after resolution)
        self._resolved: set[str] = set()

    def process_claim(self, claim: Claim) -> str:
        """Process an inbound claim (from peer or self).

        Returns: "accepted" | "rejected" | "superseded"
          - "accepted": claim is now the active claim for this subject
          - "rejected": an existing claim with tiebreak priority holds
          - "superseded": an existing claim was replaced by this one
        """
        subject = claim.subject_device_id
        if subject in self._resolved:
            return "rejected"

        existing = self._active_claims.get(subject)
        if existing is None:
            # No existing claim: accept (inherit any lapsed evidence)
            inherited = self._lapsed_evidence.pop(subject, None)
            self._accept_claim(claim, inherited_evidence=inherited)
            return "accepted"

        # Existing claim — check lease status
        existing_lease = self._leases.get(existing.claim_id)
        if existing_lease and existing_lease.is_lapsed():
            # Existing claim lapsed — new claim takes over with inherited evidence
            inherited = existing_lease.lapse()
            self._accept_claim(claim, inherited_evidence=inherited)
            logger.info(
                "Claim %s supersedes lapsed %s for %s (evidence inherited)",
                claim.claim_id, existing.claim_id, subject,
            )
            return "superseded"

        # Both claims active: deterministic tiebreak (R-M15)
        winner = deterministic_tiebreak(existing, claim)
        if winner is claim:
            # New claim wins — evict existing
            self._evict_claim(existing.claim_id)
            self._accept_claim(claim)
            logger.info(
                "Claim %s wins tiebreak over %s for %s",
                claim.claim_id, existing.claim_id, subject,
            )
            return "superseded"
        else:
            logger.debug(
                "Claim %s loses tiebreak to %s for %s",
                claim.claim_id, existing.claim_id, subject,
            )
            return "rejected"

    def _accept_claim(
        self,
        claim: Claim,
        inherited_evidence: Optional[list[dict]] = None,
    ) -> None:
        self._active_claims[claim.subject_device_id] = claim
        lease = ClaimLease.from_claim(claim, duration=self._lease_duration)
        if inherited_evidence:
            for ev in inherited_evidence:
                lease.add_evidence(ev)
        self._leases[claim.claim_id] = lease

    def _evict_claim(self, claim_id: str) -> None:
        lease = self._leases.pop(claim_id, None)
        # Don't remove from _active_claims here — the caller replaces it

    def renew_lease(self, claim_id: str) -> bool:
        """Renew the lease on a claim.

        Returns True if renewed, False if claim not found, not owned by
        us, or already lapsed.
        """
        lease = self._leases.get(claim_id)
        if lease is None:
            return False
        if lease.status != ClaimStatus.ACTIVE:
            return False
        lease.renew(duration=self._lease_duration)
        return True

    def tick(self, now: Optional[float] = None) -> list[ClaimLease]:
        """Expire lapsed leases, returning them to claimable state.

        Returns list of newly lapsed leases (for logging/reporting).
        Called periodically by the agent main loop.
        """
        now = time.time() if now is None else now
        lapsed: list[ClaimLease] = []

        for claim_id, lease in list(self._leases.items()):
            if lease.status == ClaimStatus.ACTIVE and lease.is_lapsed(now=now):
                evidence = lease.lapse()
                subject = lease.subject_device_id
                # Store lapsed evidence for inheritance (R-M17)
                self._lapsed_evidence[subject] = evidence
                # Remove from active claims so the subject is claimable again
                active = self._active_claims.get(subject)
                if active is not None and active.claim_id == claim_id:
                    del self._active_claims[subject]
                lapsed.append(lease)
                logger.info(
                    "Claim %s lapsed for %s (owned by %s)",
                    claim_id, subject, lease.owner_id,
                )

        return lapsed

    def resolve(self, subject_device_id: str) -> bool:
        """Mark an incident as resolved (investigation complete).

        Returns True if there was an active claim to resolve.
        """
        claim = self._active_claims.pop(subject_device_id, None)
        if claim is None:
            return False
        lease = self._leases.get(claim.claim_id)
        if lease:
            lease.status = ClaimStatus.RESOLVED
        self._resolved.add(subject_device_id)
        return True

    def get_owned_claims(self) -> list[Claim]:
        """Return claims owned by this agent."""
        return [
            c for c in self._active_claims.values()
            if c.claimant_id == self._my_id
            and self._leases.get(c.claim_id, None) is not None
            and self._leases[c.claim_id].status == ClaimStatus.ACTIVE
        ]

    def get_active_claim(self, subject_device_id: str) -> Optional[Claim]:
        """Get the active claim for a subject, if any."""
        return self._active_claims.get(subject_device_id)

    def get_lease(self, claim_id: str) -> Optional[ClaimLease]:
        """Get the lease for a claim."""
        return self._leases.get(claim_id)

    def is_claimable(self, subject_device_id: str) -> bool:
        """Check if a subject is available for claiming."""
        if subject_device_id in self._resolved:
            return False
        claim = self._active_claims.get(subject_device_id)
        if claim is None:
            return True
        lease = self._leases.get(claim.claim_id)
        if lease is None:
            return True
        return lease.is_lapsed()
