"""Knowledge distribution CC→SM→Agent (R3b-3 Phase 6, R-C1).

Distributes fleet-learned patterns back to Site Managers for skill
generation. Reuses the existing PushPolicy RPC channel by adding
learned_patterns_json to the PolicyUpdate message.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from harkeniq_cc.pattern_detector import FleetPattern

logger = logging.getLogger("harkeniq.cc.knowledge_distributor")


@dataclass
class DistributionEvent:
    """Tracks a pattern distribution to a site."""

    pattern_id: str
    site_id: str
    distributed_at: float = field(default_factory=time.time)
    delivered: bool = False


class KnowledgeDistributor:
    """Distributes fleet-learned patterns to Site Managers.

    Patterns detected by PatternDetector are routed to SMs whose device
    inventory matches the affected scope (vendor/model). Uses the
    existing PushPolicy RPC channel.
    """

    def __init__(self) -> None:
        self._distributions: list[DistributionEvent] = []
        self._distributed_patterns: set[str] = set()  # pattern_ids already sent

    def select_targets(
        self,
        pattern: FleetPattern,
        sites: list[dict],
    ) -> list[dict]:
        """Select sites that should receive this pattern.

        A site is a target if its fleet cache contains devices matching
        the pattern's affected_scope (vendor, model).
        """
        targets = []
        scope = pattern.affected_scope
        vendor = scope.get("vendor", "")
        model = scope.get("model", "")

        for site in sites:
            devices = site.get("devices", [])
            for dev in devices:
                if vendor and dev.get("vendor", "") != vendor:
                    continue
                if model and dev.get("model", "") != model:
                    continue
                targets.append(site)
                break  # one match is enough for this site
        return targets

    def prepare_payload(self, patterns: list[FleetPattern]) -> str:
        """Serialize patterns for PushPolicy.learned_patterns_json."""
        payload = []
        for p in patterns:
            if p.pattern_id in self._distributed_patterns:
                continue
            payload.append({
                "pattern_id": p.pattern_id,
                "pattern_type": p.pattern_type,
                "description": p.description,
                "affected_scope": p.affected_scope,
                "confidence": p.confidence,
                "evidence": p.evidence,
                "detected_at": p.detected_at,
            })
        return json.dumps(payload)

    def record_distribution(
        self,
        pattern: FleetPattern,
        site_id: str,
        delivered: bool = True,
    ) -> None:
        """Record that a pattern was distributed to a site."""
        self._distributed_patterns.add(pattern.pattern_id)
        self._distributions.append(DistributionEvent(
            pattern_id=pattern.pattern_id,
            site_id=site_id,
            delivered=delivered,
        ))

    def undistributed_patterns(
        self, patterns: list[FleetPattern],
    ) -> list[FleetPattern]:
        """Return patterns not yet distributed."""
        return [
            p for p in patterns
            if p.pattern_id not in self._distributed_patterns
        ]

    @property
    def distribution_count(self) -> int:
        return len(self._distributions)

    @property
    def distribution_history(self) -> list[DistributionEvent]:
        return list(self._distributions)
