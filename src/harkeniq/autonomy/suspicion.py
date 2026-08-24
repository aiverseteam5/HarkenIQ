"""Suspicion state tracking and threshold-triggered claims (R3b-2 Phase 5).

R-M20: Nodes maintain and exchange continuous suspicion state per component
and per path.  A claim is raised when accumulated cross-node evidence
crosses a confidence threshold.

R-M21: Where a fault is inferred, the platform must identify the smallest
set of components that explains every degraded path while remaining
consistent with every healthy path.

R-M22: Synthetic measurement must cover every member of a load-balanced bundle.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("harkeniq.autonomy.suspicion")

# Default threshold for raising a claim from accumulated suspicion
DEFAULT_CLAIM_THRESHOLD = 0.8
# Suspicion decay rate per second (suspicion fades if not reinforced)
DEFAULT_DECAY_RATE = 0.01
# Maximum suspicion score
MAX_SCORE = 1.0


@dataclass
class SuspicionScore:
    """Per-component suspicion score from one observer."""

    component: str
    score: float
    observer_id: str
    updated_at: float = 0.0

    def decay(self, now: float, rate: float = DEFAULT_DECAY_RATE) -> None:
        """Apply time-based decay."""
        if self.updated_at > 0:
            elapsed = now - self.updated_at
            if elapsed > 0:
                self.score = max(0.0, self.score - rate * elapsed)
        self.updated_at = now


class SuspicionTracker:
    """Tracks per-component suspicion from local observations and peers.

    When cross-node evidence crosses the claim threshold, the tracker
    signals that a claim should be raised.
    """

    def __init__(
        self,
        my_agent_id: str,
        claim_threshold: float = DEFAULT_CLAIM_THRESHOLD,
        decay_rate: float = DEFAULT_DECAY_RATE,
    ) -> None:
        self._my_id = my_agent_id
        self._threshold = claim_threshold
        self._decay_rate = decay_rate

        # {component: {observer_id: SuspicionScore}}
        self._scores: dict[str, dict[str, SuspicionScore]] = {}

        # Components that have already triggered claims (avoid re-triggering)
        self._claimed: set[str] = set()

        # Bundle coverage tracking (R-M22)
        self._bundles: dict[str, set[str]] = {}  # bundle_name -> measured members
        self._bundle_total: dict[str, int] = {}  # bundle_name -> total members

    def update_local(self, component: str, score: float, now: Optional[float] = None) -> None:
        """Update local suspicion for a component."""
        now = time.time() if now is None else now
        score = min(max(score, 0.0), MAX_SCORE)
        if component not in self._scores:
            self._scores[component] = {}
        self._scores[component][self._my_id] = SuspicionScore(
            component=component,
            score=score,
            observer_id=self._my_id,
            updated_at=now,
        )

    def receive_peer(
        self, component: str, score: float, peer_id: str,
        now: Optional[float] = None,
    ) -> None:
        """Receive suspicion state from a peer (R-M20)."""
        now = time.time() if now is None else now
        score = min(max(score, 0.0), MAX_SCORE)
        if component not in self._scores:
            self._scores[component] = {}
        self._scores[component][peer_id] = SuspicionScore(
            component=component,
            score=score,
            observer_id=peer_id,
            updated_at=now,
        )

    def get_combined_score(self, component: str) -> float:
        """Get the combined suspicion score for a component.

        Combines scores from all observers using max (any single
        observer at threshold triggers).
        """
        observers = self._scores.get(component, {})
        if not observers:
            return 0.0
        return max(s.score for s in observers.values())

    def get_observer_count(self, component: str) -> int:
        """Number of independent observers for a component."""
        return len(self._scores.get(component, {}))

    def tick(self, now: Optional[float] = None) -> list[str]:
        """Apply decay and check thresholds.

        Returns list of components that crossed the claim threshold
        (should trigger claim broadcast).
        """
        now = time.time() if now is None else now
        triggered: list[str] = []

        for component, observers in self._scores.items():
            # Apply decay
            for ss in observers.values():
                ss.decay(now, self._decay_rate)

            # Check threshold
            if component in self._claimed:
                continue
            combined = self.get_combined_score(component)
            if combined >= self._threshold and len(observers) >= 2:
                # Need evidence from at least 2 observers (R-M13)
                triggered.append(component)
                self._claimed.add(component)
                logger.info(
                    "Suspicion threshold crossed for %s: %.2f (observers=%d)",
                    component, combined, len(observers),
                )

        return triggered

    def clear_claimed(self, component: str) -> None:
        """Clear the claimed flag (e.g., after incident resolved)."""
        self._claimed.discard(component)
        self._scores.pop(component, None)

    def get_all_scores(self) -> dict[str, float]:
        """Return combined scores for all tracked components."""
        return {
            comp: self.get_combined_score(comp)
            for comp in self._scores
        }

    def get_exchange_data(self) -> list[tuple[str, float]]:
        """Get local suspicion data to exchange with peers.

        Returns [(component, score), ...] for components where we have
        local observations.
        """
        result: list[tuple[str, float]] = []
        for component, observers in self._scores.items():
            local = observers.get(self._my_id)
            if local and local.score > 0:
                result.append((component, local.score))
        return result

    # -- R-M21: Smallest explaining set (greedy set cover) --------------------

    @staticmethod
    def smallest_explaining_set(
        degraded_paths: list[set[str]],
        healthy_paths: list[set[str]],
    ) -> set[str]:
        """Find the smallest set of components explaining all degraded paths
        while remaining consistent with all healthy paths (R-M21).

        Args:
            degraded_paths: Each set is the components on a degraded path.
            healthy_paths: Each set is the components on a healthy path.

        Returns:
            Smallest set of suspect components. Empty set if no consistent
            explanation exists.
        """
        if not degraded_paths:
            return set()

        # Components that CANNOT be faulty (they're on a healthy path)
        exonerated: set[str] = set()
        for path in healthy_paths:
            exonerated.update(path)

        # Candidate components (appear in degraded paths, not exonerated)
        candidates: set[str] = set()
        for path in degraded_paths:
            candidates.update(path - exonerated)

        if not candidates:
            return set()  # no consistent explanation

        # Greedy set cover: pick the candidate that covers the most
        # uncovered degraded paths
        uncovered = list(range(len(degraded_paths)))
        result: set[str] = set()

        while uncovered:
            best_candidate = None
            best_coverage = 0
            for c in candidates - result:
                coverage = sum(
                    1 for i in uncovered
                    if c in degraded_paths[i]
                )
                if coverage > best_coverage:
                    best_coverage = coverage
                    best_candidate = c
            if best_candidate is None or best_coverage == 0:
                break
            result.add(best_candidate)
            uncovered = [
                i for i in uncovered
                if best_candidate not in degraded_paths[i]
            ]

        return result

    # -- R-M22: Bundle coverage ------------------------------------------------

    def register_bundle(self, bundle_name: str, total_members: int) -> None:
        """Register a load-balanced bundle for coverage tracking."""
        self._bundles[bundle_name] = set()
        self._bundle_total[bundle_name] = total_members

    def record_bundle_measurement(self, bundle_name: str, member_id: str) -> None:
        """Record that a bundle member was measured."""
        if bundle_name in self._bundles:
            self._bundles[bundle_name].add(member_id)

    def check_bundle_coverage(self, bundle_name: str) -> Optional[set[str]]:
        """Check if all members of a bundle have been measured (R-M22).

        Returns set of unmeasured members, or None if fully covered.
        """
        if bundle_name not in self._bundles:
            return None
        measured = self._bundles[bundle_name]
        total = self._bundle_total.get(bundle_name, 0)
        if len(measured) >= total:
            return None
        # Return placeholder IDs for unmeasured members
        all_members = {f"member-{i}" for i in range(total)}
        return all_members - measured
