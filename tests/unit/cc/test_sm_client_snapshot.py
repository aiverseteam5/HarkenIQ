"""QA-042: SMClient.get_fleet_snapshot carries outcomes + candidates.

Over the wire against a REAL SM servicer — the bug lived exactly in the
client's proto→dict layer, which direct-servicer tests never touch:
snap.outcomes was never dictified, so CC's fleet-learning intake
(_ingest_outcomes) ran on an empty feed from R3b-3 until QA-042.
"""

from __future__ import annotations

from datetime import datetime, timezone

import grpc
import pytest

from harkeniq.proto import harkeniq_pb2_grpc
from harkeniq_cc.sm_client import SMClient
from harkeniq_sm.approvals import ApprovalService
from harkeniq_sm.config import SMConfig
from harkeniq_sm.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_sm.db.models import ActionOutcomeRow, CandidateSkillRow, Site
from harkeniq_sm.grpc_server import SiteManagerServiceServicer


@pytest.fixture
async def sm_wire():
    """Real SM servicer on a real insecure port, with seeded rows."""
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    db = make_sessionmaker(engine)
    async with db() as session:
        session.add(Site(name="site-1"))
        session.add(ActionOutcomeRow(
            action_id="act-1", action_type="FAN_RESET", device_id="dev-x",
            outcome="SUCCESS", fault_resolved=True,
            recorded_at=datetime.now(timezone.utc),
        ))
        session.add(CandidateSkillRow(
            skill_id="auto-fan-9",
            yaml_text="name: auto-fan\nversion: 1\n",
            source_device="agent-1", source_component="fan:Fan1A",
            validation_state="tested", dry_run_matches=2,
        ))
        await session.commit()

    config = SMConfig(insecure=True, site_name="site-1")
    servicer = SiteManagerServiceServicer(
        db, ApprovalService(db, config), config,
    )
    server = grpc.aio.server()
    harkeniq_pb2_grpc.add_SiteManagerServiceServicer_to_server(
        servicer, server
    )
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    yield f"127.0.0.1:{port}"
    await server.stop(grace=None)
    await engine.dispose()


async def test_snapshot_dict_carries_outcomes_and_candidates(sm_wire):
    snapshot = await SMClient().get_fleet_snapshot(
        sm_wire, "any-token", "t1", "",
    )

    outcomes = snapshot["outcomes"]
    assert len(outcomes) == 1
    assert outcomes[0]["action_type"] == "FAN_RESET"
    assert outcomes[0]["outcome"] == "SUCCESS"
    assert outcomes[0]["fault_resolved"] is True

    candidates = snapshot["candidate_skills"]
    assert len(candidates) == 1
    assert candidates[0]["skill_id"] == "auto-fan-9"
    assert candidates[0]["validation_state"] == "tested"
    assert candidates[0]["dry_run_matches"] == 2
    assert "name:" in candidates[0]["yaml_text"]
