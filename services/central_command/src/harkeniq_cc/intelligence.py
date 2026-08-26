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

from harkeniq_cc.db.repos import (
    CandidateSkillRepo,
    FleetPatternRepo,
    OutcomeHistoryRepo,
)
from harkeniq_cc.learning_feedback import LearningFeedbackTracker
from harkeniq_cc.outcome_aggregator import OutcomeAggregator
from harkeniq_cc.pattern_detector import FleetPattern, PatternDetector

logger = logging.getLogger("harkeniq.cc.intelligence")


class IntelligenceEngine:
    """Cumulative aggregation + pattern detection over outcome history."""

    def __init__(self) -> None:
        self.aggregator = OutcomeAggregator()
        self.detector = PatternDetector()
        # QA-033 feedback half: the R-C1 cycle tracker finally runs inside
        # the loop instead of only in tests.
        self.feedback = LearningFeedbackTracker()
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
                # R-C1: every detected pattern opens a learning cycle
                self.feedback.start_cycle(
                    cycle_id=pattern.pattern_id,
                    pattern_id=pattern.pattern_id,
                    pattern_type=pattern.pattern_type,
                    baseline_metrics=self._scope_metrics(pattern.affected_scope),
                )
            logger.info(
                "Intelligence cycle: %d outcomes ingested, %d new patterns",
                len(outcomes), len(new_patterns),
            )
        await self._link_candidates(session, tenant_id)
        await self._track_outcomes(session, tenant_id)
        return new_patterns

    # -- R-C1 learning feedback (QA-033) --------------------------------

    def _scope_metrics(self, scope: dict) -> dict:
        """Current failure/success rates for a pattern's scope."""
        action_type = scope.get("action_type", "")
        vendor = scope.get("vendor", "") or None
        metrics = self.aggregator.get_metrics(
            action_type=action_type or None, vendor=vendor,
        )
        model = scope.get("model", "")
        if model:
            narrowed = [m for m in metrics if m.model == model]
            metrics = narrowed or metrics
        if not metrics:
            return {"failure_rate": 0.0, "success_rate": 1.0, "total": 0}
        total = sum(m.total_count for m in metrics)
        success = sum(m.success_count for m in metrics)
        return {
            "failure_rate": 1.0 - (success / total) if total else 0.0,
            "success_rate": (success / total) if total else 1.0,
            "total": total,
        }

    async def _link_candidates(self, session, tenant_id: str) -> None:
        """Match received candidate skills to open learning cycles.

        Heuristic: the candidate's source subsystem (component prefix)
        appears in the cycle's pattern scope action_type or description.
        Unmatched candidates stay `received` — visible in the API, never
        silently dropped.
        """
        repo = CandidateSkillRepo(session)
        received = await repo.list_candidates(tenant_id, status="received")
        if not received:
            return
        open_cycles = [
            c for c in self.feedback.get_active_cycles() if c.skill_id is None
        ]
        for cand in received:
            subsystem = cand.source_component.split(":", 1)[0].lower()
            if not subsystem:
                continue
            for cycle in open_cycles:
                pattern_key = cycle.pattern_id.lower()
                entry_scope = f"{cycle.pattern_type} {pattern_key}"
                if subsystem in entry_scope or self._subsystem_in_cycle(
                    subsystem, cycle,
                ):
                    self.feedback.record_skill_generated(
                        cycle.cycle_id, cand.skill_id
                    )
                    await repo.link_cycle(
                        tenant_id, cand.skill_id, cycle.cycle_id
                    )
                    open_cycles.remove(cycle)
                    logger.info(
                        "Candidate skill %s linked to learning cycle %s",
                        cand.skill_id, cycle.cycle_id,
                    )
                    break

    def _subsystem_in_cycle(self, subsystem: str, cycle) -> bool:
        """True when the cycle's pattern scope mentions the subsystem
        (e.g. candidate 'fan:Fan1A' vs pattern action_type FAN_RESET)."""
        scope = self._pattern_scope(cycle.pattern_id)
        action_type = (scope.get("action_type") or "").lower()
        description = (scope.get("description") or "").lower()
        return subsystem in action_type or subsystem in description

    def _pattern_scope(self, pattern_id: str) -> dict:
        """Scope of a detected pattern from the detector's live state."""
        for pattern in self.detector.all_patterns:
            if pattern.pattern_id == pattern_id:
                return {**pattern.affected_scope,
                        "description": pattern.description}
        return {}

    async def _track_outcomes(self, session, tenant_id: str) -> None:
        """Advance skill-linked cycles: record post-distribution outcomes,
        then evaluate the promotion criteria (≥95% success, ≥50 devices).

        Promotion marks the candidate row `promoted` — a recommendation
        for the marketplace review path, never a distribution bypass.
        """
        repo = CandidateSkillRepo(session)
        for cycle in list(self.feedback.get_active_cycles()):
            if cycle.skill_id is None or cycle.sites_distributed == 0:
                continue
            metrics = self._scope_metrics(self._pattern_scope(cycle.pattern_id))
            if not metrics.get("total"):
                continue
            self.feedback.record_outcomes(cycle.cycle_id, metrics)
            if self.feedback.check_promotion(cycle.cycle_id):
                await repo.mark_promoted(tenant_id, cycle.skill_id)
                logger.info(
                    "Learning cycle %s met promotion criteria: skill %s "
                    "recommended for marketplace review",
                    cycle.cycle_id, cycle.skill_id,
                )


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
                # QA-033 (R-C1): distribution advances skill-linked cycles
                async with state.sessionmaker() as session:
                    await _record_distribution(
                        session, engine, delivered
                    )
                    await session.commit()
        except Exception as exc:
            logger.error("Intelligence cycle error: %s", exc)


async def _record_distribution(session, engine, delivered: int) -> None:
    """Record distribution reach on skill-linked cycles (QA-033).

    Device reach = fleet-cache devices matching the pattern's vendor/model
    scope (empty scope fields are wildcards).
    """
    from sqlalchemy import func, select

    from harkeniq_cc.db.models import CCFleetCache

    for cycle in engine.feedback.get_active_cycles():
        if cycle.skill_id is None or cycle.sites_distributed:
            continue
        scope = engine._pattern_scope(cycle.pattern_id)
        stmt = select(func.count()).select_from(CCFleetCache)
        if scope.get("vendor"):
            stmt = stmt.where(CCFleetCache.vendor == scope["vendor"])
        if scope.get("model"):
            stmt = stmt.where(CCFleetCache.model == scope["model"])
        devices = (await session.execute(stmt)).scalar() or 0
        engine.feedback.record_distribution(
            cycle.cycle_id, sites=delivered, devices=devices,
        )
