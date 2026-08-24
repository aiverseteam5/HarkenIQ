"""R4-3 exit gate (Governance & Ecosystem).

Exit criteria from the R4 Architecture Amendment §3 (R4-3):
  1. Marketplace has 10+ community skills reviewed and promoted.
  2. Air-gapped deployment runs with local LLM.
  3. Firmware orchestration tested with staged rollout and rollback.

Plus the P20 deliverable: predictive risk scoring over outcome history.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

REPO = Path(__file__).parents[2]

SKILL_TEMPLATE = """\
name: community-skill-{n}
version: 1
target: fan
description: Community skill number {n}
rules:
  - condition: "health == 'Warning'"
    verdict: WARNING
    message: "Fan {{name}} degraded"
default_verdict: HEALTHY
"""


class TestCriterion1MarketplacePromotions:
    """10+ community skills submitted, reviewed, and promoted."""

    async def test_ten_plus_skills_reviewed_and_promoted(self):
        from harkeniq_console.app import create_app
        from harkeniq_console.config import ConsoleConfig
        from harkeniq_console.db.base import (
            create_all, make_engine, make_sessionmaker,
        )
        from harkeniq_console.runtime import AppState

        engine = make_engine("sqlite+aiosqlite:///:memory:")
        await create_all(engine)
        state = AppState(config=ConsoleConfig(insecure=True), engine=engine,
                        sessionmaker=make_sessionmaker(engine))
        app = create_app(state)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            promoted = 0
            for n in range(1, 13):  # 12 submissions
                r = await client.post(
                    "/api/marketplace/skills",
                    json={"yaml_content": SKILL_TEMPLATE.format(n=n)},
                )
                assert r.status_code == 200
                entry = r.json()
                if n == 11:
                    # One reviewer rejection exercises the reject path
                    r = await client.post(
                        f"/api/admin/marketplace/skills/{entry['id']}/review",
                        json={"approve": False, "reason": "duplicate logic"},
                    )
                    assert r.json()["review_status"] == "rejected"
                    continue
                r = await client.post(
                    f"/api/admin/marketplace/skills/{entry['id']}/review",
                    json={"approve": True},
                )
                assert r.json()["published"] is True
                if n == 12:
                    continue  # published but not promoted (below gate)
                await client.post(
                    f"/api/admin/marketplace/skills/{entry['id']}/stats",
                    json={"successes": 97, "failures": 3, "devices": 60},
                )
                r = await client.post(
                    f"/api/admin/marketplace/skills/{entry['id']}/promote"
                )
                assert r.status_code == 200
                promoted += 1

            assert promoted >= 10
            r = await client.get("/api/marketplace/skills",
                                 params={"tier": "verified"})
            assert r.json()["total"] >= 10
        await engine.dispose()


class TestCriterion2AirGappedLLM:
    """Local model integrity + health metadata + compose service."""

    async def test_verified_model_serves_and_reports(self, tmp_path):
        from harkeniq_sm.app import create_app
        from harkeniq_sm.config import SMConfig
        from harkeniq_sm.runtime import make_state

        model = tmp_path / "mistral-7b.Q4_K_M.gguf"
        model.write_bytes(b"GGUF-demo-model")
        sha = hashlib.sha256(model.read_bytes()).hexdigest()

        state = await make_state(SMConfig(
            insecure=True, llm_enabled=True,
            llm_api_url="http://llama:8080/v1", llm_model="local-gguf",
            llm_model_path=str(model), llm_model_sha256=sha,
        ))
        try:
            assert state.model_info.status == "ok"
            assert any(
                type(p).__name__ == "LLMReasoner"
                for p in state.ingest.reasoning_pipeline._providers
            )
            app = create_app(state)
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as c:
                health = (await c.get("/healthz")).json()
            assert health["status"] == "ok"
            assert health["llm_model"]["status"] == "ok"
            assert health["llm_model"]["actual_sha256"] == sha
        finally:
            await state.engine.dispose()

    async def test_corrupted_model_never_serves(self, tmp_path):
        from harkeniq_sm.config import SMConfig
        from harkeniq_sm.runtime import make_state

        model = tmp_path / "model.gguf"
        model.write_bytes(b"corrupted")
        state = await make_state(SMConfig(
            insecure=True, llm_enabled=True,
            llm_api_url="http://llama:8080/v1",
            llm_model_path=str(model), llm_model_sha256="0" * 64,
        ))
        try:
            assert not any(
                type(p).__name__ == "LLMReasoner"
                for p in state.ingest.reasoning_pipeline._providers
            )
        finally:
            await state.engine.dispose()

    def test_compose_ships_llama_service(self):
        import yaml
        compose = yaml.safe_load(
            (REPO / "deploy" / "full-stack" / "docker-compose.yml").read_text()
        )
        assert "llama" in compose["services"]


class TestCriterion3FirmwareOrchestration:
    """Staged rollout + rollback over the REAL Redfish device path:
    orchestrator waves -> ActionExecutor -> simulator UpdateService."""

    async def test_staged_rollout_halt_and_rollback_end_to_end(self, tmp_path):
        from harkeniq.actions.executor import ActionExecutor
        from harkeniq.actions.queue import ActionQueue
        from harkeniq.mock.simulator import MockSimulator
        from harkeniq.models import ActionType, VerdictSeverity
        from harkeniq.redfish.client import RedfishClient
        from harkeniq_sm.db.base import (
            create_all, make_engine, make_sessionmaker,
        )
        from harkeniq_sm.db.repos import (
            DeviceRepo, DomainRepo, FirmwareCampaignRepo, SiteRepo,
        )
        from harkeniq_sm.firmware_orchestrator import (
            FirmwareOrchestrator, UpdateResult,
        )

        engine = make_engine("sqlite+aiosqlite:///:memory:")
        await create_all(engine)
        db = make_sessionmaker(engine)

        # Two simulated Dell BMCs sharing one power domain -> 2 waves
        sims: dict[str, MockSimulator] = {}
        clients: dict[str, RedfishClient] = {}
        for agent_id in ("agent-a", "agent-b"):
            sim = MockSimulator(device="dell-r750", port=0, no_auth=True)
            await sim.start()
            client = RedfishClient(host=sim.url, verify_ssl=False,
                                   request_timeout=10)
            await client.connect("admin", "password")
            sims[agent_id] = sim
            clients[agent_id] = client

        try:
            async with db() as session:
                site = await SiteRepo(session).get_or_create("site-1")
                device_repo = DeviceRepo(session)
                devices = {}
                for agent_id in sims:
                    devices[agent_id] = await device_repo.upsert_registration(
                        site_id=site.id, agent_id=agent_id, vendor="dell",
                        firmware=[{"component": "bmc", "name": "iDRAC9",
                                   "version": "7.00.00.00"}],
                    )
                domain_repo = DomainRepo(session)
                pdu = await domain_repo.create(site.id, "pdu-1", "power")
                await domain_repo.set_members(
                    pdu.id, [d.id for d in devices.values()]
                )
                await session.commit()
                site_id = site.id
                device_ids = [d.id for d in devices.values()]

            class RedfishSimUpdater:
                """Drives the real executor against the device's BMC."""

                def _executor(self, device) -> ActionExecutor:
                    ex = ActionExecutor(
                        clients[device.agent_id], "dell",
                        {"actions": {"allow_list": [
                            "FIRMWARE_UPDATE", "FIRMWARE_ROLLBACK",
                        ]}},
                    )
                    ex.task_poll_interval = 0.0
                    return ex

                def _action(self, action_type, params):
                    queue = ActionQueue()
                    action = queue.enqueue(
                        action_type, "firmware:bmc", "firmware-campaign",
                        VerdictSeverity.CRITICAL, params,
                    )
                    queue.approve(action.id)
                    return action

                async def update(self, device, campaign) -> UpdateResult:
                    outcome = await self._executor(device).execute(
                        self._action(ActionType.FIRMWARE_UPDATE, {
                            "component": campaign.component,
                            "target_version": campaign.target_version,
                        })
                    )
                    return UpdateResult(
                        success=outcome.success,
                        post_version=campaign.target_version
                        if outcome.success else "",
                        error=outcome.error_message or "",
                    )

                async def rollback(self, device, campaign) -> bool:
                    outcome = await self._executor(device).execute(
                        self._action(ActionType.FIRMWARE_ROLLBACK,
                                     {"component": campaign.component})
                    )
                    return outcome.success

            orch = FirmwareOrchestrator(db, updater=RedfishSimUpdater())

            # ── Run 1: staged success across both waves ──────────────
            campaign_id = await orch.create_campaign(
                site_id, device_ids, "bmc", "7.10.30.00",
            )
            async with db() as session:
                campaign = await FirmwareCampaignRepo(session).get(campaign_id)
                assert campaign.wave_count == 2  # shared domain -> staged
            await orch.approve(campaign_id, actor="vinod")
            first = await orch.advance(campaign_id)
            assert first["status"] == "running"
            # Exactly ONE of the two BMCs updated after wave 0
            updated = [a for a, s in sims.items()
                       if s.firmware_banks["bmc"]["active"] == "7.10.30.00"]
            assert len(updated) == 1
            second = await orch.advance(campaign_id)
            assert second["status"] == "completed"
            for sim in sims.values():
                assert sim.firmware_banks["bmc"]["active"] == "7.10.30.00"
                assert sim.firmware_banks["bmc"]["standby"] == "7.00.00.00"

            # ── Run 2: wave-1 failure halts + blue-green rollback ────
            campaign2 = await orch.create_campaign(
                site_id, device_ids, "bmc", "7.20.00.00",
            )
            await orch.approve(campaign2, actor="vinod")
            await orch.advance(campaign2)  # wave 0 ok
            # Identify the wave-1 device and make its BMC fail the task
            async with db() as session:
                targets = await FirmwareCampaignRepo(session).targets(
                    campaign2, 1
                )
                victim_device = targets[0].device_id
                victim_agent = next(
                    a for a, d in devices.items() if d.id == victim_device
                )
            sims[victim_agent].inject_firmware_update_failure()
            result = await orch.advance(campaign2)
            assert result["status"] == "halted"
            # The victim's BMC still runs its pre-campaign2 version...
            active = sims[victim_agent].firmware_banks["bmc"]["active"]
            assert active != "7.20.00.00"
            async with db() as session:
                repo = FirmwareCampaignRepo(session)
                targets = {t.device_id: t
                           for t in await repo.targets(campaign2)}
                # ...and the campaign recorded the rollback + halt
                assert targets[victim_device].status == "rolled_back"
                campaign = await repo.get(campaign2)
                assert campaign.status == "halted"
        finally:
            for client in clients.values():
                await client.close()
            for sim in sims.values():
                await sim.stop()
            await engine.dispose()


class TestP20PredictiveInfrastructure:
    async def test_risk_scoring_available(self):
        from harkeniq_cc.predictive import score_device

        risk = score_device("gate-device", [
            {"outcome": "FAILURE", "recorded_at": None} for _ in range(6)
        ])
        assert risk.band == "high"
