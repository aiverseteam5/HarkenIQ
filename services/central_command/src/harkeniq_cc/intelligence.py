"""CC intelligence loop (R4-1, R-C2).

Wires the R3b-3 learning components into the runtime: periodically reads
new rows from cc_outcome_history, feeds the OutcomeAggregator (site-aware
since R4-1), snapshots for trend detection, runs the PatternDetector
(including cross-site correlation), and persists new patterns to
cc_fleet_patterns.

The aggregator and detector live across cycles; only rows ingested after
the cursor are fed in, so aggregates are cumulative and the detector's
dedup keys prevent re-emitting known patterns. On process restart the
cursor resets and history is re-ingested from scratch -- FleetPatternRepo
.save() is idempotent on pattern id and pattern dedup keys re-arm, which
can re-emit a still-true pattern under a new id; acceptable, patterns
describe current fleet state.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

from harkeniq_cc.db.repos import FleetPatternRepo, OutcomeHistoryRepo
from harkeniq_cc.outcome_aggregator import OutcomeAggregator
from harkeniq_cc.pattern_detector import FleetPattern, PatternDetector

logger = logging.getLogger("harkeniq.cc.intelligence")


class IntelligenceEngine:
    """Cumulative aggregation + pattern detection over outcome history."""

    def __init__(self) -> None:
        self.aggregator = OutcomeAggregator()
        self.detector = PatternDetector()
        self._cursor: Optional[datetime] = None

    async def run_cycle(self, session, tenant_id: str) -> list[FleetPattern]:
        """One detection cycle. Returns newly detected (and persisted) patterns.

        Caller owns the commit.
        """
        repo = OutcomeHistoryRepo(session)
        outcomes = await repo.list_outcome_dicts(tenant_id, since=self._cursor)
        if outcomes:
            self.aggregator.ingest(outcomes)
            self._cursor = max(oc["ingested_at"] for oc in outcomes)
        # Snapshot every cycle (even empty ones) so get_trend() windows
        # reflect elapsed cycles, not just data arrival.
        self.aggregator.snapshot()
        new_patterns = self.detector.detect(self.aggregator)
        if new_patterns:
            patterns_repo = FleetPatternRepo(session)
            for pattern in new_patterns:
                await patterns_repo.save(pattern, tenant_id=tenant_id)
            logger.info(
                "Intelligence cycle: %d outcomes ingested, %d new patterns",
                len(outcomes), len(new_patterns),
            )
        return new_patterns


async def intelligence_loop(state) -> None:
    """Background task: run detection cycles at the configured interval.

    QA-033: each cycle also distributes fleet patterns to scope-matched
    Site Managers (R-C1) — the KnowledgeDistributor finally runs inside
    a loop instead of only in tests.
    """
    from harkeniq_cc.knowledge_distributor import (
        KnowledgeDistributor, distribute_patterns,
    )

    interval = state.config.pattern_detect_interval_s
    engine = IntelligenceEngine()
    state.intelligence = engine
    distributor = KnowledgeDistributor()
    logger.info("Intelligence loop started (interval=%.0fs)", interval)
    while True:
        await asyncio.sleep(interval)
        try:
            async with state.sessionmaker() as session:
                await engine.run_cycle(session, state.config.tenant_id)
                await session.commit()
            delivered = await distribute_patterns(
                state.config, state.sessionmaker, distributor=distributor
            )
            if delivered:
                logger.info(
                    "Distributed %d pattern delivery(ies) to SMs", delivered
                )
        except Exception as exc:
            logger.error("Intelligence cycle error: %s", exc)
