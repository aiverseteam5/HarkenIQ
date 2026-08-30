"""QA-033 feedback half, SM side: candidate skill generation + CC upflow.

The R3b-1 C2 SkillGenerator and the validate_and_promote pipeline finally
have production callers: the ingest enrichment path generates + validates
+ persists candidates, and GetFleetSnapshot reports each one to CC once.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from harkeniq.proto import harkeniq_pb2
from harkeniq_sm.approvals import ApprovalService
from harkeniq_sm.config import SMConfig
from harkeniq_sm.db.models import CandidateSkillRow, Site
from harkeniq_sm.grpc_server import SiteManagerServiceServicer
from harkeniq_sm.ingest import IngestService
from harkeniq_sm.reasoning import ReasoningContext, ReasoningResult
from harkeniq_sm.skill_generator import SkillGenerator

GOOD_YAML = (
    "```yaml\n"
    "name: auto-fan-bearing-wear\n"
    "version: 1\n"
    "target: fan\n"
    "description: Detect fan bearing wear from RPM decline\n"
    "rules:\n"
    "  - condition: \"speed_rpm < 3000\"\n"
    "    verdict: WARNING\n"
    "    message: \"Fan {name} RPM below threshold\"\n"
    "```"
)


def _result(action="Replace fan"):
    return ReasoningResult(
        provider="llm", diagnosis="Bearing wear", confidence=0.9,
        suggested_action=action,
    )


def _context():
    return ReasoningContext(
        device_id="agent-1", component="fan:Fan1A", severity="CRITICAL",
        evidence=[{"skill": "fan-health", "data": {"speed_rpm": 2500}}],
    )


@pytest.fixture
def ingest(db):
    svc = IngestService(db, SMConfig(insecure=True, site_name="site-test"))
    provider = AsyncMock()
    provider.complete = AsyncMock(return_value=GOOD_YAML)
    svc.skill_generator = SkillGenerator(provider)
    return svc


async def _rows(db):
    async with db() as session:
        return (
            (await session.execute(select(CandidateSkillRow))).scalars().all()
        )


class TestCandidateGeneration:
    async def test_validated_candidate_persisted(self, db, ingest):
        await ingest._generate_candidate_skill(
            "agent-1", "fan:Fan1A", "CRITICAL", _result(), _context(),
        )
        rows = await _rows(db)
        assert len(rows) == 1
        row = rows[0]
        assert row.source_device == "agent-1"
        assert row.source_component == "fan:Fan1A"
        # Static passed -> TESTED; dry-run ran against the trigger evidence
        assert row.validation_state == "tested"
        assert row.dry_run_matches == 1  # speed_rpm 2500 < 3000 matched
        assert not row.reported_to_cc

    async def test_dedup_per_device_component(self, db, ingest):
        for _ in range(3):
            await ingest._generate_candidate_skill(
                "agent-1", "fan:Fan1A", "CRITICAL", _result(), _context(),
            )
        assert len(await _rows(db)) == 1

    async def test_invalid_yaml_dropped(self, db, ingest):
        ingest.skill_generator._provider.complete = AsyncMock(
            return_value="```yaml\nname: bad\nversion: 1\n```"  # no rules
        )
        await ingest._generate_candidate_skill(
            "agent-1", "fan:Fan1A", "CRITICAL", _result(), _context(),
        )
        assert await _rows(db) == []

    async def test_no_generator_is_noop(self, db):
        svc = IngestService(db, SMConfig(insecure=True, site_name="s"))
        await svc._generate_candidate_skill(
            "agent-1", "fan:Fan1A", "CRITICAL", _result(), _context(),
        )
        assert await _rows(db) == []


class TestSnapshotUpflow:
    async def test_candidates_ride_snapshot_once(self, db, ingest):
        await ingest._generate_candidate_skill(
            "agent-1", "fan:Fan1A", "CRITICAL", _result(), _context(),
        )
        # E0.2: a candidate belongs to the site of the device that
        # produced it, and the snapshot is addressed by Central Command's
        # site identity, so both the binding and the device must exist.
        async with db() as session:
            from harkeniq_sm.db.models import Device

            site = Site(name="site-1", cc_site_id="cc-site-1")
            session.add(site)
            await session.flush()
            session.add(Device(site_id=site.id, agent_id="agent-1"))
            await session.commit()

        config = SMConfig(insecure=True, site_name="site-1")
        servicer = SiteManagerServiceServicer(
            db, ApprovalService(db, config), config,
        )
        request = harkeniq_pb2.FleetSnapshotRequest(
            tenant_id="t1", site_id="cc-site-1",
        )
        snap = await servicer.GetFleetSnapshot(request, None)
        assert len(snap.candidate_skills) == 1
        cand = snap.candidate_skills[0]
        assert cand.source_component == "fan:Fan1A"
        assert cand.validation_state == "tested"
        assert "name:" in cand.yaml_text

        # Reported exactly once: the next snapshot is empty.
        snap2 = await servicer.GetFleetSnapshot(request, None)
        assert len(snap2.candidate_skills) == 0
