"""R4-2 exit gate (Fleet Intelligence).

Exit criteria from the R4 Architecture Amendment §3 (R4-2):
  1. Audit trail is cryptographically verifiable.
  2. Config drift detected and corrected via playbook.
  3. Fleet dashboard shows warranty status per device.

Plus the P14 deliverable: firmware inventory reaches CC and CVE
exposure is computed against the local feed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from harkeniq.agent import Agent
from harkeniq.audit.chain import GENESIS_HASH, next_link, verify_chain
from harkeniq.mock.simulator import MockSimulator
from harkeniq.models import ActionStatus, ActionType
from harkeniq.state.checkpoint import CheckpointManager

REPO = Path(__file__).parents[2]
TENANT = "test-tenant"

POLICY_YAML = """\
policy_id: exit-gate-baseline
name: Exit gate baseline
device_types: ["dell"]
severity: WARNING
expected:
  NTPConfigGroup.1.NTPEnable: Enabled
"""


@pytest.fixture
async def dell_sim():
    sim = MockSimulator(device="dell-r750", port=0, no_auth=True)
    await sim.start()
    yield sim
    await sim.stop()


class TestCriterion1AuditVerifiable:
    """Every audit store chains at write and verifies on demand."""

    async def test_agent_chain_verifiable_and_tamper_evident(self, tmp_path):
        cp = CheckpointManager(tmp_path / "cp.db")
        for i in range(5):
            await cp.save_audit_entry(
                action="IDENTIFY_LED", target=f"t{i}", outcome="success",
            )
        assert (await cp.verify_audit_chain()).valid is True
        cp.conn.execute("UPDATE audit_log SET outcome='forged' WHERE seq=3")
        cp.conn.commit()
        result = await cp.verify_audit_chain()
        assert result.valid is False and result.first_bad_seq == 3
        await cp.close()

    async def test_all_three_service_stores_chain(self):
        """SM, CC, Console repos all append chained + verify."""
        # SM
        from harkeniq_sm.db.base import create_all as sm_create
        from harkeniq_sm.db.base import make_engine as sm_engine
        from harkeniq_sm.db.base import make_sessionmaker as sm_maker
        from harkeniq_sm.db.repos import AuditRepo as SMAudit

        engine = sm_engine("sqlite+aiosqlite:///:memory:")
        await sm_create(engine)
        async with sm_maker(engine)() as session:
            repo = SMAudit(session)
            await repo.append("op", "one")
            await repo.append("op", "two")
            await session.commit()
            assert (await repo.verify_chain()).valid is True
        await engine.dispose()

        # CC
        from harkeniq_cc.db.base import create_all as cc_create
        from harkeniq_cc.db.base import make_engine as cc_engine
        from harkeniq_cc.db.base import make_sessionmaker as cc_maker
        from harkeniq_cc.db.repos import AuditRepo as CCAudit

        engine = cc_engine("sqlite+aiosqlite:///:memory:")
        await cc_create(engine)
        async with cc_maker(engine)() as session:
            repo = CCAudit(session)
            await repo.append("op", "one", tenant_id=TENANT)
            await session.commit()
            assert (await repo.verify_chain()).valid is True
        await engine.dispose()

        # Console
        from harkeniq_console.db.base import create_all as con_create
        from harkeniq_console.db.base import make_engine as con_engine
        from harkeniq_console.db.base import make_sessionmaker as con_maker
        from harkeniq_console.db.repos import AuditRepo as ConAudit

        engine = con_engine("sqlite+aiosqlite:///:memory:")
        await con_create(engine)
        async with con_maker(engine)() as session:
            repo = ConAudit(session)
            await repo.append(None, "a@x.com", "one")
            await session.commit()
            assert (await repo.verify_chain()).valid is True
        await engine.dispose()

    def test_oq20_chain_shape(self):
        """OQ-20 resolution: SHA-256 chain, per-service seq from 1,
        genesis-linked, verified on demand (not on read)."""
        seq, prev, h = next_link(0, None, {"k": "v"})
        assert (seq, prev) == (1, GENESIS_HASH)
        assert verify_chain([(seq, prev, h, {"k": "v"})]).valid is True


class TestCriterion2ConfigDriftCorrected:
    """Drift detected -> approved -> corrected via playbook, verified."""

    async def test_end_to_end(self, dell_sim, tmp_path):
        policy_dir = tmp_path / "policies"
        policy_dir.mkdir()
        (policy_dir / "p.yaml").write_text(POLICY_YAML)
        agent = Agent({
            "bmc": {"host": dell_sim.url, "username": "admin",
                    "password": "password", "verify_ssl": False},
            "skills": {"directory": str(REPO / "skills")},
            "polling": {"sensor_interval": 0.05},
            "actions": {"enabled": True, "approval_mode": "queue",
                        "allow_list": ["CONFIG_RESTORE"]},
            "compliance": {"enabled": True,
                           "policy_directory": str(policy_dir),
                           "interval": 3600, "dry_run": False,
                           "verification_wait_scale": 0.0},
            "checkpoint": {"path": str(tmp_path / "cp.db"), "interval": 600},
        })
        await agent.start()
        try:
            dell_sim.inject_config_drift("NTPConfigGroup.1.NTPEnable", "Disabled")
            findings = await agent.check_compliance()
            assert any(f.status == "DRIFT" for f in findings)
            pending = [a for a in agent.action_queue.all()
                       if a.type == ActionType.CONFIG_RESTORE]
            assert len(pending) == 1
            agent.action_queue.approve(pending[0].id)
            await agent.poll_and_evaluate()
            assert pending[0].status == ActionStatus.COMPLETED
            assert dell_sim.bmc_attributes[
                "NTPConfigGroup.1.NTPEnable"] == "Enabled"

            # The remediation itself is on the (verifiable) audit chain
            entries = await agent.checkpoint.list_audit_entries()
            actions = [e["action"] for e in entries]
            assert "CONFIG_RESTORE_PLAYBOOK" in actions
            assert (await agent.checkpoint.verify_audit_chain()).valid is True
        finally:
            await agent.stop()


@pytest.fixture
async def cc_client():
    from harkeniq_cc.app import create_app
    from harkeniq_cc.auth import configure_auth
    from harkeniq_cc.config import CCConfig
    from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
    from harkeniq_cc.db.repos import FleetCacheRepo, SiteRepo, WarrantyRepo
    from harkeniq_cc.runtime import AppState
    from harkeniq_cc.warranty.base import WarrantyRecord

    config = CCConfig(tenant_id=TENANT, insecure=True)
    configure_auth("", "", "", insecure=True)
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sessionmaker = make_sessionmaker(engine)
    state = AppState(config=config, engine=engine, sessionmaker=sessionmaker)
    app = create_app(state)

    async with sessionmaker() as session:
        site = await SiteRepo(session).upsert(TENANT, "dc-blr-1", "sm:50051")
        row = await FleetCacheRepo(session).upsert_device(
            site_id=site.id, agent_id="agent-1", agent_name="srv-01",
            vendor="dell", model="R750", service_tag="DTAG1",
            firmware=[{"component": "bmc", "name": "iDRAC9",
                       "version": "7.00.00.00"}],
        )
        await WarrantyRepo(session).upsert_records([
            WarrantyRecord("DTAG1", "dell", "ProSupport", "2024-01-01",
                           "2029-12-31", "dell_techdirect"),
        ])
        await session.commit()
        device_id = row.id

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.device_id = device_id
        yield c
    await engine.dispose()


class TestCriterion3WarrantyOnDashboard:
    """The dashboard's data source shows warranty status per device."""

    async def test_list_and_detail_carry_warranty(self, cc_client):
        r = await cc_client.get("/api/fleet/")
        device = r.json()["devices"][0]
        assert device["warranty"]["status"] == "active"

        r = await cc_client.get(f"/api/fleet/{cc_client.device_id}")
        assert r.json()["warranty"]["service_level"] == "ProSupport"


class TestP14FirmwareToCve:
    """Firmware inventory reaches CC; CVE exposure computed locally."""

    async def test_example_feed_flags_old_firmware(self, cc_client):
        feed = json.loads((REPO / "deploy" / "cve-feed-example.json").read_text())
        r = await cc_client.post("/api/firmware/cve-feed",
                                 json={"entries": feed["entries"]})
        assert r.json()["imported"] == 3
        r = await cc_client.get("/api/firmware/exposure")
        exposures = r.json()["exposures"]
        assert any(
            e["cve_id"] == "EXAMPLE-2026-0001" and e["agent_id"] == "agent-1"
            for e in exposures
        )
