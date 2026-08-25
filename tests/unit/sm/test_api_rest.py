"""Devices / incidents / actions HTTP API via ASGITransport."""

from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from harkeniq_sm.app import create_app
from harkeniq_sm.approvals import ApprovalService
from harkeniq_sm.config import SMConfig
from harkeniq_sm.db.repos import DeviceRepo, IncidentRepo, SiteRepo, StatusRepo
from harkeniq_sm.ingest import IngestService
from harkeniq_sm.runtime import AppState


@pytest.fixture
def config():
    return SMConfig(insecure=True, site_name="site-test")


@pytest.fixture
def state(db, config):
    state = AppState(config=config)
    state.sessionmaker = db
    state.ingest = IngestService(db, config)
    state.approvals = ApprovalService(db, config)
    return state


@pytest.fixture
async def client(state):
    transport = httpx.ASGITransport(app=create_app(state))
    async with httpx.AsyncClient(transport=transport, base_url="http://sm") as c:
        yield c


async def _seed_device(db, config, agent_id="a1", heartbeat=True, **kw):
    async with db() as session:
        site = await SiteRepo(session).get_or_create(config.site_name)
        device = await DeviceRepo(session).upsert_registration(
            site_id=site.id, agent_id=agent_id, **kw
        )
        if heartbeat:
            await StatusRepo(session).upsert(
                device.id, datetime.now(timezone.utc), "MONITORING",
                {"fan": "OK", "psu": "CRITICAL"}, None,
            )
        await session.commit()
        return device.id, site.id


def _report(agent_id="a1", action_id="act-1", status="PENDING"):
    return SimpleNamespace(
        agent_id=agent_id, action_id=action_id, type="FAN_RESET",
        sensor_id="fan:Fan1", skill_name="fan_health",
        verdict_severity="CRITICAL", params_json='{"target": "Fan1"}',
        status=status, proposed_at="2026-08-19T10:00:00Z", outcome_json="",
    )


class TestDevicesApi:
    async def test_list(self, db, config, client):
        await _seed_device(db, config, vendor="Dell", model="R750")
        devices = (await client.get("/api/devices")).json()
        assert len(devices) == 1
        d = devices[0]
        assert d["agent_id"] == "a1"
        assert d["vendor"] == "Dell"
        assert d["observation"] == "observed"
        assert d["health"] == "critical"

    async def test_detail_with_verdicts(self, db, config, state, client):
        await _seed_device(db, config)
        await state.ingest.verdict(
            agent_id="a1", sensor_id="psu:PS1", skill_name="psu_health",
            severity="CRITICAL", message="PSU failed",
        )
        d = (await client.get("/api/devices/a1")).json()
        assert d["subsystems"] == {"psu": "CRITICAL"}
        assert len(d["recent_verdicts"]) == 1
        assert d["recent_verdicts"][0]["sensor_id"] == "psu:PS1"

    async def test_unknown_404(self, client):
        assert (await client.get("/api/devices/ghost")).status_code == 404


class TestIncidentsApi:
    async def _seed_incidents(self, db, config):
        device_id, site_id = await _seed_device(db, config, heartbeat=False)
        async with db() as session:
            repo = IncidentRepo(session)
            parent = await repo.create(
                site_id=site_id, kind="shared_power", inferred=True,
                confidence=0.6, title="Shared power fault",
            )
            child = await repo.create(
                site_id=site_id, kind="device", device_id=device_id,
                subsystem="psu", parent_id=parent.id,
            )
            resolved = await repo.create(
                site_id=site_id, kind="device", device_id=device_id,
                subsystem="fan",
            )
            await repo.resolve(resolved)
            await session.commit()
            return parent.id, child.id

    async def test_parent_cards_with_children(self, db, config, client):
        parent_id, child_id = await self._seed_incidents(db, config)
        cards = (await client.get("/api/incidents")).json()
        assert [c["id"] for c in cards] == [parent_id]
        card = cards[0]
        assert card["inferred"] is True
        assert card["confidence"] == 0.6
        assert [c["id"] for c in card["children"]] == [child_id]
        assert card["children"][0]["agent_id"] == "a1"

    async def test_status_all_includes_resolved(self, db, config, client):
        await self._seed_incidents(db, config)
        cards = (await client.get("/api/incidents", params={"status": "all"})).json()
        kinds = {c["kind"] for c in cards}
        assert "shared_power" in kinds
        assert any(c["status"] == "resolved" for c in cards)

    async def test_detail_and_404(self, db, config, client):
        parent_id, child_id = await self._seed_incidents(db, config)
        detail = (await client.get(f"/api/incidents/{parent_id}")).json()
        assert [c["id"] for c in detail["children"]] == [child_id]
        assert (await client.get("/api/incidents/nope")).status_code == 404


class TestActionsApi:
    async def test_list(self, state, client):
        await state.approvals.report_action(_report())
        actions = (await client.get("/api/actions")).json()
        assert len(actions) == 1
        assert actions[0]["agent_id"] == "a1"
        assert actions[0]["status"] == "pending"
        assert actions[0]["params"] == {"target": "Fan1"}

    async def test_approve(self, state, client):
        await state.approvals.report_action(_report())
        action = (await client.get("/api/actions")).json()[0]
        resp = await client.post(
            f"/api/actions/{action['id']}/approve", json={"actor": "op@site"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "approved"
        # QA-006: SM-local identities are asserted, and recorded as such.
        assert body["decided_by"] == "sm-local:op@site"

    async def test_deny(self, state, client):
        await state.approvals.report_action(_report())
        action = (await client.get("/api/actions")).json()[0]
        resp = await client.post(
            f"/api/actions/{action['id']}/deny", json={"actor": "op"}
        )
        assert resp.json()["status"] == "denied"

    async def test_unknown_404(self, client):
        resp = await client.post("/api/actions/nope/approve", json={"actor": "op"})
        assert resp.status_code == 404

    async def test_conflict_409(self, state, client):
        await state.approvals.report_action(_report())
        action = (await client.get("/api/actions")).json()[0]
        await client.post(f"/api/actions/{action['id']}/deny", json={"actor": "op"})
        resp = await client.post(
            f"/api/actions/{action['id']}/approve", json={"actor": "op"}
        )
        assert resp.status_code == 409
        assert "denied" in resp.json()["detail"]


class TestActorRequired:
    async def test_empty_actor_rejected(self, state, client):
        """QA-006: no default identity — an empty actor is a 422."""
        await state.approvals.report_action(_report())
        action = (await client.get("/api/actions")).json()[0]
        resp = await client.post(
            f"/api/actions/{action['id']}/approve", json={"actor": ""}
        )
        assert resp.status_code == 422
        resp = await client.post(
            f"/api/actions/{action['id']}/approve", json={}
        )
        assert resp.status_code == 422
