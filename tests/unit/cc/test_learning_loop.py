"""QA-033 feedback half, CC side: candidate intake + the R-C1 loop.

The LearningFeedbackTracker finally runs inside the intelligence engine:
pattern detected → cycle opened → SM candidate skill linked →
distribution recorded → post-distribution outcomes measured → promotion
recommendation. Also covers QA-042's ingest path (_ingest_candidates).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from harkeniq_cc.db.models import CCCandidateSkill, CCFleetCache, CCOutcomeHistory, CCSite
from harkeniq_cc.db.repos import CandidateSkillRepo
from harkeniq_cc.fleet_poller import _ingest_candidates, _ingest_outcomes
from harkeniq_cc.intelligence import IntelligenceEngine, _record_distribution
from harkeniq_cc.learning_feedback import LearningFeedbackTracker

TENANT = "t1"


def _cand(skill_id="auto-fan-1", component="fan:Fan1A"):
    return {
        "skill_id": skill_id,
        "yaml_text": "name: auto-fan\nversion: 1\ntarget: fan\nrules: []\n",
        "source_device": "agent-1",
        "source_component": component,
        "validation_state": "tested",
        "generated_at_unix": 1787000000,
        "warnings_json": '["review before promotion"]',
        "dry_run_matches": 1,
    }


async def _seed_site(session) -> str:
    site = CCSite(
        tenant_id=TENANT, site_name="site-1",
        sm_endpoint="sm:50051", sm_token="tok",
    )
    session.add(site)
    await session.flush()
    return site.id


def _outcome(site_id, outcome="FAILURE", n=0):
    return {
        "action_id": f"act-{outcome}-{n}",
        "action_type": "FAN_RESET",
        "device_agent_id": f"agent-{n}",
        "outcome": outcome,
        "fault_resolved": outcome == "SUCCESS",
        "vendor": "Dell",
        "model": "R750",
        "recorded_at_unix": 1787000000,
    }


class TestCandidateIngest:
    async def test_ingest_and_idempotent_upsert(self, db):
        async with db() as session:
            site_id = await _seed_site(session)
            await _ingest_candidates(session, TENANT, site_id, [_cand()])
            await _ingest_candidates(session, TENANT, site_id, [_cand()])
            await session.commit()
        async with db() as session:
            rows = (
                await session.execute(select(CCCandidateSkill))
            ).scalars().all()
            assert len(rows) == 1
            assert rows[0].tenant_id == TENANT
            assert rows[0].status == "received"
            assert rows[0].warnings == ["review before promotion"]


class TestLearningLoop:
    async def test_full_cycle_to_promotion(self, db):
        engine = IntelligenceEngine()
        # Lab-scale thresholds; production defaults are 95% / 50 devices.
        engine.feedback = LearningFeedbackTracker(
            promotion_success_rate=0.9, promotion_min_devices=1,
        )

        async with db() as session:
            site_id = await _seed_site(session)
            # 5 FAN_RESET failures on Dell R750 -> batch_failure pattern
            await _ingest_outcomes(
                session, site_id,
                [_outcome(site_id, "FAILURE", i) for i in range(5)],
            )
            await _ingest_candidates(session, TENANT, site_id, [_cand()])
            session.add(CCFleetCache(
                site_id=site_id, agent_id="agent-1", vendor="Dell",
                model="R750",
            ))
            await session.commit()

        # Cycle 1: pattern detected -> learning cycle opened; the fan
        # candidate links to the FAN_RESET pattern.
        async with db() as session:
            patterns = await engine.run_cycle(session, TENANT)
            await session.commit()
        assert len(patterns) == 1
        assert patterns[0].pattern_type == "batch_failure"
        cycle = engine.feedback.get_cycle(patterns[0].pattern_id)
        assert cycle is not None
        assert cycle.skill_id == "auto-fan-1"
        assert cycle.outcomes_before["failure_rate"] == 1.0
        async with db() as session:
            row = await CandidateSkillRepo(session).list_candidates(TENANT)
            assert row[0].status == "cycle_linked"
            assert row[0].cycle_id == cycle.cycle_id

        # Distribution recorded (1 delivery, 1 in-scope device).
        async with db() as session:
            await _record_distribution(session, engine, delivered=1)
            await session.commit()
        assert cycle.sites_distributed == 1
        assert cycle.devices_applied == 1

        # Post-distribution: outcomes recover -> cycle completes, skill
        # meets the (lab-scaled) promotion criteria.
        async with db() as session:
            await _ingest_outcomes(
                session, site_id,
                [_outcome(site_id, "SUCCESS", 100 + i) for i in range(45)],
            )
            await session.commit()
        async with db() as session:
            await engine.run_cycle(session, TENANT)
            await session.commit()

        assert cycle.completed_at is not None
        assert cycle.improvement_pct and cycle.improvement_pct > 0
        assert cycle.promoted
        assert "auto-fan-1" in engine.feedback.promotions
        async with db() as session:
            row = await CandidateSkillRepo(session).list_candidates(TENANT)
            assert row[0].status == "promoted"

    async def test_unmatched_candidate_stays_received(self, db):
        engine = IntelligenceEngine()
        async with db() as session:
            site_id = await _seed_site(session)
            await _ingest_outcomes(
                session, site_id,
                [_outcome(site_id, "FAILURE", i) for i in range(5)],
            )
            # disk candidate cannot match a FAN_RESET pattern
            await _ingest_candidates(
                session, TENANT, site_id,
                [_cand(skill_id="auto-disk-1", component="disk:Bay0")],
            )
            await session.commit()
        async with db() as session:
            await engine.run_cycle(session, TENANT)
            await session.commit()
        async with db() as session:
            row = await CandidateSkillRepo(session).list_candidates(TENANT)
            assert row[0].status == "received"
            assert row[0].cycle_id is None
