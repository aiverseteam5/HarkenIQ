"""Firmware campaign orchestration tests (R4-3 P19, OQ-21).

Covers: blast-radius wave planning (one device per fault domain per
wave), the campaign lifecycle (draft -> approved -> running ->
completed), halt-on-first-failure with blue-green rollback, the
approval gate, the no-updater refusal, and the campaign API.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from harkeniq_sm.app import create_app
from harkeniq_sm.config import SMConfig
from harkeniq_sm.db.repos import (
    AuditRepo,
    DeviceRepo,
    DomainRepo,
    FirmwareCampaignRepo,
    SiteRepo,
)
from harkeniq_sm.firmware_orchestrator import (
    FirmwareOrchestrator,
    UpdateResult,
    plan_waves,
)
from harkeniq_sm.runtime import AppState


class TestWavePlanning:
    def test_one_device_per_domain_per_wave(self):
        assignment = plan_waves(
            ["d1", "d2", "d3", "d4"],
            {"d1": ["rackA"], "d2": ["rackA"], "d3": ["rackB"], "d4": ["rackB"]},
        )
        # Same-domain devices never share a wave
        assert assignment["d1"] != assignment["d2"]
        assert assignment["d3"] != assignment["d4"]
        # Cross-domain devices can share wave 0
        assert assignment["d1"] == assignment["d3"] == 0

    def test_wave_size_cap(self):
        assignment = plan_waves(
            [f"d{i}" for i in range(7)], {}, max_wave_size=3,
        )
        from collections import Counter
        sizes = Counter(assignment.values())
        assert all(size <= 3 for size in sizes.values())
        assert sum(sizes.values()) == 7

    def test_multi_domain_device(self):
        assignment = plan_waves(
            ["d1", "d2", "d3"],
            {"d1": ["power1", "cool1"], "d2": ["power1"], "d3": ["cool1"]},
        )
        assert assignment["d1"] != assignment["d2"]
        assert assignment["d1"] != assignment["d3"]

    def test_deterministic(self):
        args = (["b", "a", "c"], {"a": ["x"], "b": ["x"], "c": []})
        assert plan_waves(*args) == plan_waves(*args)


class FakeUpdater:
    """Configurable updater: fails the device ids in `fail_devices`."""

    def __init__(self, fail_devices: set[str] | None = None):
        self.fail_devices = fail_devices or set()
        self.updated: list[str] = []
        self.rolled_back: list[str] = []

    async def update(self, device, campaign) -> UpdateResult:
        if device.id in self.fail_devices:
            return UpdateResult(success=False, error="task ended in Exception")
        self.updated.append(device.id)
        return UpdateResult(
            success=True, post_version=campaign.target_version,
        )

    async def rollback(self, device, campaign) -> bool:
        self.rolled_back.append(device.id)
        return True


async def _seed_site(db, n_devices: int = 4):
    """Site with n devices; d0/d1 share rack-1, d2/d3 share rack-2."""
    async with db() as session:
        site = await SiteRepo(session).get_or_create("site-1")
        device_repo = DeviceRepo(session)
        devices = []
        for i in range(n_devices):
            devices.append(await device_repo.upsert_registration(
                site_id=site.id, agent_id=f"agent-{i}",
                agent_name=f"srv-{i:02d}", vendor="dell", model="R750",
                firmware=[{"component": "bmc", "name": "iDRAC9",
                           "version": "7.00.00.00"}],
            ))
        domain_repo = DomainRepo(session)
        rack1 = await domain_repo.create(site.id, "rack-1", "power")
        rack2 = await domain_repo.create(site.id, "rack-2", "power")
        await domain_repo.set_members(rack1.id, [d.id for d in devices[:2]])
        await domain_repo.set_members(rack2.id, [d.id for d in devices[2:4]])
        await session.commit()
        return site.id, [d.id for d in devices]


class TestCampaignLifecycle:
    async def test_create_plans_domain_aware_waves(self, db):
        site_id, device_ids = await _seed_site(db)
        orch = FirmwareOrchestrator(db, updater=FakeUpdater())
        campaign_id = await orch.create_campaign(
            site_id, device_ids, "bmc", "7.10.30.00",
        )
        async with db() as session:
            repo = FirmwareCampaignRepo(session)
            campaign = await repo.get(campaign_id)
            assert campaign.status == "draft"
            assert campaign.wave_count == 2
            targets = await repo.targets(campaign_id)
            assert len(targets) == 4
            assert all(t.pre_version == "7.00.00.00" for t in targets)
            waves_by_device = {t.device_id: t.wave_index for t in targets}
            # rack-mates never share a wave
            assert waves_by_device[device_ids[0]] != waves_by_device[device_ids[1]]
            assert waves_by_device[device_ids[2]] != waves_by_device[device_ids[3]]

    async def test_advance_requires_approval(self, db):
        site_id, device_ids = await _seed_site(db)
        orch = FirmwareOrchestrator(db, updater=FakeUpdater())
        campaign_id = await orch.create_campaign(
            site_id, device_ids, "bmc", "7.10.30.00",
        )
        result = await orch.advance(campaign_id)
        assert result["detail"] == "campaign is not running"

    async def test_staged_rollout_completes(self, db):
        site_id, device_ids = await _seed_site(db)
        updater = FakeUpdater()
        orch = FirmwareOrchestrator(db, updater=updater)
        campaign_id = await orch.create_campaign(
            site_id, device_ids, "bmc", "7.10.30.00",
        )
        await orch.approve(campaign_id, actor="vinod")

        first = await orch.advance(campaign_id)   # wave 0
        assert first["status"] == "running"
        assert len(updater.updated) == 2          # strictly one wave per advance
        second = await orch.advance(campaign_id)  # wave 1
        assert second["status"] == "completed"
        assert len(updater.updated) == 4

        async with db() as session:
            targets = await FirmwareCampaignRepo(session).targets(campaign_id)
            assert all(t.status == "completed" for t in targets)
            assert all(t.post_version == "7.10.30.00" for t in targets)
            # The whole lifecycle is on the (verifiable) audit chain
            audit = AuditRepo(session)
            result = await audit.verify_chain()
            assert result.valid is True
            actions = [r.action for r in await audit.list_all()]
            assert "firmware.campaign.create" in actions
            assert "firmware.campaign.approve" in actions
            assert "firmware.campaign.complete" in actions

    async def test_failure_rolls_back_and_halts(self, db):
        site_id, device_ids = await _seed_site(db)
        # Fail a device that lands in wave 1 (second advance)
        async def _find_wave1_device():
            orch = FirmwareOrchestrator(db, updater=FakeUpdater())
            cid = await orch.create_campaign(
                site_id, device_ids, "bmc", "7.10.30.00",
            )
            async with db() as session:
                targets = await FirmwareCampaignRepo(session).targets(cid, 1)
                return cid, targets[0].device_id

        campaign_id, victim = await _find_wave1_device()
        updater = FakeUpdater(fail_devices={victim})
        orch = FirmwareOrchestrator(db, updater=updater)
        await orch.approve(campaign_id, actor="vinod")

        await orch.advance(campaign_id)              # wave 0 ok
        result = await orch.advance(campaign_id)     # wave 1 fails
        assert result["status"] == "halted"
        assert victim in result["halt_reason"]
        assert updater.rolled_back == [victim]

        async with db() as session:
            repo = FirmwareCampaignRepo(session)
            campaign = await repo.get(campaign_id)
            assert campaign.status == "halted"
            targets = {t.device_id: t for t in await repo.targets(campaign_id)}
            assert targets[victim].status == "rolled_back"
            # A halted campaign never advances again
            further = await orch.advance(campaign_id)
            assert further["status"] == "halted"

    async def test_double_approval_rejected(self, db):
        site_id, device_ids = await _seed_site(db)
        orch = FirmwareOrchestrator(db, updater=FakeUpdater())
        campaign_id = await orch.create_campaign(
            site_id, device_ids, "bmc", "7.10.30.00",
        )
        await orch.approve(campaign_id, actor="vinod")
        with pytest.raises(ValueError):
            await orch.approve(campaign_id, actor="mallory")

    async def test_no_updater_refuses_to_advance(self, db):
        site_id, device_ids = await _seed_site(db)
        orch = FirmwareOrchestrator(db, updater=None)
        campaign_id = await orch.create_campaign(
            site_id, device_ids, "bmc", "7.10.30.00",
        )
        await orch.approve(campaign_id, actor="vinod")
        with pytest.raises(RuntimeError):
            await orch.advance(campaign_id)


@pytest.fixture
async def client(db):
    config = SMConfig(insecure=True)
    state = AppState(config=config, sessionmaker=db)
    state.firmware_updater = FakeUpdater()
    app = create_app(state)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.db = db
        yield c


class TestCampaignAPI:
    async def test_create_approve_advance_flow(self, client):
        _, device_ids = await _seed_site(client.db, n_devices=2)
        r = await client.post("/api/firmware-campaigns", json={
            "agent_ids": ["agent-0", "agent-1"],
            "target_version": "7.10.30.00",
        })
        assert r.status_code == 200, r.text
        campaign = r.json()
        assert campaign["status"] == "draft"
        assert campaign["wave_count"] == 2  # rack-mates split

        r = await client.post(
            f"/api/firmware-campaigns/{campaign['id']}/approve",
            json={"actor": "vinod"},
        )
        assert r.json()["status"] == "approved"

        r = await client.post(
            f"/api/firmware-campaigns/{campaign['id']}/advance"
        )
        assert r.status_code == 200
        r = await client.post(
            f"/api/firmware-campaigns/{campaign['id']}/advance"
        )
        assert r.json()["status"] == "completed"

        r = await client.get(f"/api/firmware-campaigns/{campaign['id']}")
        detail = r.json()
        assert all(t["status"] == "completed" for t in detail["targets"])
        assert {t["agent_id"] for t in detail["targets"]} == {
            "agent-0", "agent-1",
        }

    async def test_unknown_agent_404(self, client):
        r = await client.post("/api/firmware-campaigns", json={
            "agent_ids": ["ghost"], "target_version": "1.0",
        })
        assert r.status_code == 404
