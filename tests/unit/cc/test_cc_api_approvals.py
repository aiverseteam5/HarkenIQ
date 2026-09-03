"""Approvals API endpoint tests against in-memory CC database.

The approve/deny endpoints call SMClient.route_approval via gRPC, which
will fail in unit tests (no running SM). We mock the SMClient to isolate
the API logic and verify DB state changes.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from harkeniq_cc.app import create_app
from harkeniq_cc.auth import configure_auth
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.repos import ApprovalRouteRepo, SiteRepo
from harkeniq_cc.runtime import AppState

from tests.unit.cc.conftest import seed_tenant_admin

TENANT = "test-tenant"


@pytest.fixture
async def client():
    """FastAPI test client seeded with approval routes."""
    config = CCConfig(tenant_id=TENANT, insecure=True)
    configure_auth("", "", "", insecure=True)
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sessionmaker = make_sessionmaker(engine)
    state = AppState(config=config, engine=engine, sessionmaker=sessionmaker)
    app = create_app(state)
    # A23-5: a rowless tenant is STRICT now (A23.11), so this
    # fixture seeds the founding administrator that tenant
    # birth seeds (A23.14 D4) instead of leaning on the
    # `legacy_open` synthesis a missing row used to give.
    await seed_tenant_admin(sessionmaker, TENANT, "lab-user")

    async with sessionmaker() as session:
        site = await SiteRepo(session).upsert(
            TENANT, "dc-blr-1", "https://sm1.lab:50051",
        )
        repo = ApprovalRouteRepo(session)
        # 3 pending
        await repo.create(site.id, "act-1", action_type="fan_boost", device_agent_id="agent-01")
        await repo.create(site.id, "act-2", action_type="led_on", device_agent_id="agent-02")
        await repo.create(site.id, "act-3", action_type="bmc_reset", device_agent_id="agent-03")
        # 2 decided (history)
        r4 = await repo.create(site.id, "act-4", action_type="fan_boost", device_agent_id="agent-01")
        await repo.update_decision(r4, "approved", "op@lab")
        r5 = await repo.create(site.id, "act-5", action_type="led_on", device_agent_id="agent-02")
        await repo.update_decision(r5, "denied", "admin@lab")
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await engine.dispose()


@pytest.fixture
async def empty_client():
    """FastAPI test client with no approval routes."""
    config = CCConfig(tenant_id=TENANT, insecure=True)
    configure_auth("", "", "", insecure=True)
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sessionmaker = make_sessionmaker(engine)
    state = AppState(config=config, engine=engine, sessionmaker=sessionmaker)
    app = create_app(state)
    # A23-5: a rowless tenant is STRICT now (A23.11), so this
    # fixture seeds the founding administrator that tenant
    # birth seeds (A23.14 D4) instead of leaning on the
    # `legacy_open` synthesis a missing row used to give.
    await seed_tenant_admin(sessionmaker, TENANT, "lab-user")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await engine.dispose()


class TestListPending:
    async def test_list_pending(self, client):
        r = await client.get("/api/approvals/")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 3
        for action in data["actions"]:
            assert action["decision"] is None

    async def test_list_pending_empty(self, empty_client):
        r = await empty_client.get("/api/approvals/")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 0
        assert data["actions"] == []

    async def test_list_pending_paginated(self, client):
        r = await client.get("/api/approvals/", params={"page_size": 2, "page": 1})
        assert r.status_code == 200
        data = r.json()
        assert len(data["actions"]) == 2
        assert data["total"] == 3


class TestApproveAction:
    @patch("harkeniq_cc.api.approvals.SMClient")
    async def test_approve_action(self, mock_sm_cls, client):
        mock_instance = AsyncMock()
        mock_instance.route_approval.return_value = {
            "accepted": True, "delivered": True, "reason": "",
        }
        mock_sm_cls.return_value = mock_instance

        r = await client.post("/api/approvals/act-1/approve")
        assert r.status_code == 200
        data = r.json()
        assert data["decision"] == "approved"
        assert data["action_id"] == "act-1"

    @patch("harkeniq_cc.api.approvals.SMClient")
    async def test_deny_action(self, mock_sm_cls, client):
        mock_instance = AsyncMock()
        mock_instance.route_approval.return_value = {
            "accepted": True, "delivered": True, "reason": "",
        }
        mock_sm_cls.return_value = mock_instance

        r = await client.post("/api/approvals/act-2/deny")
        assert r.status_code == 200
        data = r.json()
        assert data["decision"] == "denied"
        assert data["action_id"] == "act-2"

    async def test_approve_not_found(self, client):
        r = await client.post("/api/approvals/nonexistent/approve")
        assert r.status_code == 404

    @patch("harkeniq_cc.api.approvals.SMClient")
    async def test_approve_already_decided(self, mock_sm_cls, client):
        """Approving an already-decided action returns 409."""
        r = await client.post("/api/approvals/act-4/approve")
        assert r.status_code == 409


class TestBatchDecide:
    @patch("harkeniq_cc.api.approvals.SMClient")
    async def test_batch_approve(self, mock_sm_cls, client):
        mock_instance = AsyncMock()
        mock_instance.route_approval.return_value = {
            "accepted": True, "delivered": True, "reason": "",
        }
        mock_sm_cls.return_value = mock_instance

        r = await client.post(
            "/api/approvals/batch",
            json={"action_ids": ["act-1", "act-2"], "decision": "approved"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["processed"] == 2
        ok_count = sum(1 for res in data["results"] if res["ok"])
        assert ok_count == 2

    async def test_batch_invalid_decision(self, client):
        r = await client.post(
            "/api/approvals/batch",
            json={"action_ids": ["act-1"], "decision": "maybe"},
        )
        assert r.status_code == 400


class TestHistory:
    async def test_history(self, client):
        r = await client.get("/api/approvals/history")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 2
        for action in data["actions"]:
            assert action["decision"] is not None

    async def test_history_paginated(self, client):
        r = await client.get(
            "/api/approvals/history", params={"page_size": 1, "page": 1},
        )
        assert r.status_code == 200
        data = r.json()
        assert len(data["actions"]) == 1
        assert data["total"] == 2

    async def test_history_empty(self, empty_client):
        r = await empty_client.get("/api/approvals/history")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 0
