"""S3: the product test — does yesterday's learning reach tomorrow's answer?

    "Can something learned from yesterday materially improve what HarkenIQ
     tells a human or agent to pay attention to tomorrow?"

This file proves the full vertical on one database:

    real outcome  ->  pattern  ->  durable learned signal  ->  cycle ledger
                  ->  SIMULATED PROCESS RESTART (new engine, fresh memory)
                  ->  the next Attention evaluation consumes the knowledge

The restart is the point. Before S3 the learning record lived in the
intelligence engine's memory, so a restart erased what the fleet had
learned and cc_candidate_skills.cycle_id pointed at nothing. A substrate
that forgets cannot "improve future decisions".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from harkeniq_cc.attention import build_attention
from harkeniq_cc.db.models import (
    CCFleetCache,
    CCLearnedSignal,
    CCLearningCycle,
    CCOutcomeHistory,
    CCSite,
)
from harkeniq_cc.db.repos import LearnedSignalRepo, LearningCycleRepo
from harkeniq_cc.intelligence import IntelligenceEngine
from harkeniq_cc.predictive import DeviceRisk

TENANT = "t1"


async def _seed_failing_fleet(session, *, devices=6, failures=5):
    """Real outcome rows: one cohort repeatedly failing the same action.

    This is what the fleet actually experienced — the ground truth every
    later concept is derived from.
    """
    site = CCSite(
        tenant_id=TENANT, site_name="BLR-1",
        sm_endpoint="sm:50051", sm_token="tok",
    )
    session.add(site)
    await session.flush()

    now = datetime.now(timezone.utc)
    for d in range(devices):
        agent_id = f"agent-{d}"
        session.add(CCFleetCache(
            site_id=site.id, agent_id=agent_id, agent_name=f"srv-{d}",
            vendor="Dell", model="R750", health="ok", observation="observed",
            service_tag=f"TAG{d}",
        ))
        for n in range(failures):
            session.add(CCOutcomeHistory(
                site_id=site.id, action_id=f"act-{d}-{n}",
                action_type="SEL_CLEAR", device_agent_id=agent_id,
                vendor="Dell", model="R750", outcome="FAILURE",
                fault_resolved=False,
                recorded_at=now - timedelta(hours=n + 1),
            ))
    await session.commit()
    return site.id


class TestTheLearningVerticalSurvivesRestart:
    async def test_outcome_to_pattern_to_durable_signal_to_next_attention(self, db):
        # ---- Day one: the loop runs in a live process -------------------
        async with db() as session:
            site_id = await _seed_failing_fleet(session)

        engine = IntelligenceEngine()
        async with db() as session:
            patterns = await engine.run_cycle(session, TENANT)
            await session.commit()

        assert patterns, "repeated real failures must produce a pattern"
        assert any(p.pattern_type in ("batch_failure", "reliability")
                   for p in patterns)

        # Knowledge and the process record are both on disk now.
        async with db() as session:
            signals = (await session.execute(
                select(CCLearnedSignal).where(CCLearnedSignal.tenant_id == TENANT)
            )).scalars().all()
            cycles = (await session.execute(
                select(CCLearningCycle).where(CCLearningCycle.tenant_id == TENANT)
            )).scalars().all()

        assert signals, "the pattern must yield durable learned knowledge"
        assert cycles, "the learning process must be recorded durably"
        cohort = [s for s in signals if s.scope_type == "cohort"]
        assert cohort, "cohort scope is always evidence-supported"
        assert "SEL_CLEAR" in cohort[0].statement
        assert cohort[0].confidence > 0

        # ---- RESTART: everything in memory is gone ----------------------
        del engine
        fresh_engine = IntelligenceEngine()
        assert fresh_engine.feedback.get_active_cycles() == [], (
            "a fresh process starts with no in-memory learning"
        )

        # ---- Day two: the next attention evaluation --------------------
        async with db() as session:
            devices = (await session.execute(
                select(CCFleetCache).where(CCFleetCache.site_id == site_id)
            )).scalars().all()
            sites = (await session.execute(
                select(CCSite).where(CCSite.tenant_id == TENANT)
            )).scalars().all()
            learned = await LearnedSignalRepo(session).list_active(TENANT)
            durable_cycles = await LearningCycleRepo(session).list_cycles(TENANT)

        assert learned, "knowledge must survive the restart"
        assert durable_cycles, "the cycle ledger must survive the restart"

        risks = [
            DeviceRisk(
                agent_id=d.agent_id, vendor=d.vendor, model=d.model,
                risk_score=0.2, band="low", sample_count=5,
                factors={"basis": "device_history", "weighted_failure_rate": 0.2},
                site_id=d.site_id, agent_name=d.agent_name,
            )
            for d in devices
        ]
        answer = build_attention(
            devices=devices, risks=risks, exposures=[], warranty_map={},
            pending_routes=[], patterns=[], sites=sites, tenant_id=TENANT,
            learned_signals=learned,
        )

        item = answer["items"][0]
        # THE PRODUCT TEST: yesterday's learning is in today's answer.
        assert item["evidence"]["learned_signals"], (
            "attention must consume what the fleet learned"
        )
        assert any("Learned for" in r for r in item["reasons"]), (
            "and must say so to the human in plain language"
        )
        signal = item["evidence"]["learned_signals"][0]
        assert signal["action_type"] == "SEL_CLEAR"
        assert signal["source_pattern_id"], "knowledge traces to its pattern"

    async def test_re_detection_refreshes_knowledge_rather_than_duplicating(self, db):
        async with db() as session:
            await _seed_failing_fleet(session)

        engine = IntelligenceEngine()
        for _ in range(3):  # three detection passes over the same truth
            async with db() as session:
                await engine.run_cycle(session, TENANT)
                await session.commit()

        async with db() as session:
            rows = (await session.execute(
                select(CCLearnedSignal)
                .where(CCLearnedSignal.tenant_id == TENANT)
                .where(CCLearnedSignal.scope_type == "cohort")
            )).scalars().all()

        keys = [r.signal_key for r in rows]
        assert len(keys) == len(set(keys)), "one key, one row — no duplicates"

    async def test_knowledge_is_tenant_scoped(self, db):
        """A tenant never inherits another tenant's learning."""
        async with db() as session:
            await _seed_failing_fleet(session)
        engine = IntelligenceEngine()
        async with db() as session:
            await engine.run_cycle(session, TENANT)
            await session.commit()

        async with db() as session:
            mine = await LearnedSignalRepo(session).list_active(TENANT)
            theirs = await LearnedSignalRepo(session).list_active("other-tenant")
        assert mine
        assert theirs == []


class TestLedgerSemantics:
    async def test_recommended_is_not_promoted(self, db):
        """Promotion stays governed: the ledger records that the evidence
        bar was met, never that the capability was distributed."""
        async with db() as session:
            await _seed_failing_fleet(session)
        engine = IntelligenceEngine()
        async with db() as session:
            await engine.run_cycle(session, TENANT)
            await session.commit()
            cycles = await LearningCycleRepo(session).list_cycles(TENANT)
        for c in cycles:
            assert hasattr(c, "promotion_recommended")
            # Nothing here can mark a capability as distributed.
            assert not hasattr(c, "distributed_to_agents")

    async def test_cycle_rows_carry_their_pattern(self, db):
        async with db() as session:
            await _seed_failing_fleet(session)
        engine = IntelligenceEngine()
        async with db() as session:
            patterns = await engine.run_cycle(session, TENANT)
            await session.commit()
            cycles = await LearningCycleRepo(session).list_cycles(TENANT)
        pattern_ids = {p.pattern_id for p in patterns}
        assert {c.pattern_id for c in cycles} & pattern_ids
