"""CC audit hash chain tests + audit API (R4-2 P12)."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update

from harkeniq_cc.app import create_app
from harkeniq_cc.auth import configure_auth
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.models import CCAuditLog
from harkeniq_cc.db.repos import AuditRepo
from harkeniq_cc.runtime import AppState

from tests.unit.cc.conftest import seed_tenant_admin

TENANT = "test-tenant"


class TestCCAuditChain:
    async def test_appends_chain_and_verifies(self, session):
        repo = AuditRepo(session)
        for i in range(4):
            await repo.append("admin", f"agent.disable.{i}",
                              subject=f"agent-{i}", tenant_id=TENANT)
        await session.commit()
        result = await repo.verify_chain()
        assert result.valid is True
        assert result.length == 4

    async def test_tamper_detected(self, session):
        repo = AuditRepo(session)
        for i in range(3):
            await repo.append("admin", f"act.{i}", tenant_id=TENANT)
        await session.commit()
        await session.execute(
            update(CCAuditLog).where(CCAuditLog.seq == 3)
            .values(subject="forged")
        )
        await session.commit()
        result = await repo.verify_chain()
        assert result.valid is False
        assert result.first_bad_seq == 3


@pytest.fixture
async def client():
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
        repo = AuditRepo(session)
        for i in range(3):
            await repo.append("admin", f"policy.update.{i}", tenant_id=TENANT)
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, sessionmaker
    await engine.dispose()


class TestCCAuditAPI:
    async def test_list_returns_entries(self, client):
        c, _ = client
        r = await c.get("/api/audit/")
        assert r.status_code == 200
        entries = r.json()["entries"]
        assert len(entries) == 3
        assert entries[0]["entry_hash"]
        assert {e["seq"] for e in entries} == {1, 2, 3}

    async def test_verify_endpoint_valid(self, client):
        c, _ = client
        r = await c.get("/api/audit/verify")
        assert r.status_code == 200
        data = r.json()
        assert data["valid"] is True
        assert data["length"] == 3

    async def test_verify_endpoint_detects_tamper(self, client):
        c, sessionmaker = client
        async with sessionmaker() as session:
            await session.execute(
                update(CCAuditLog).where(CCAuditLog.seq == 2)
                .values(actor="attacker")
            )
            await session.commit()
        r = await c.get("/api/audit/verify")
        data = r.json()
        assert data["valid"] is False
        assert data["first_bad_seq"] == 2
        assert "mismatch" in data["error"]
