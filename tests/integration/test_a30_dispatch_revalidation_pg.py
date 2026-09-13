"""A6-4B0a remediation (A30.17) on a REAL PostgreSQL: dispatch revalidates.

The sqlite proofs live in tests/unit/cc/test_a30_execution_time_authority.py.
This file asks the one question sqlite cannot answer honestly: when the
dispatch gate compares a grant's `expires_at` against now, does the
timestamptz boundary hold on the engine production runs -- on BOTH
dispatch paths, through the real approval route and the real background
pass, with admission done by the real evaluator?

Audit rows are deliberately NOT cleaned up: the chain is append-only and
deleting from it would break verification for everything after.

Gated on ``HARKEN_TEST_CC_PG_DSN``, e.g. from the full-stack compose:

    postgresql+asyncpg://harkeniq:harkeniq@<postgres>:5432/harkeniq_cc
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import sqlalchemy as sa

from harkeniq_cc import agent_runtime
from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.app import create_app
from harkeniq_cc.auth import ROLE_PERMISSIONS, UserContext, configure_auth
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import make_engine, make_sessionmaker
from harkeniq_cc.db.models import (
    CCAgentCapability,
    CCAgentProposal,
    CCFleetCache,
    CCIncident,
    CCOperationalAgent,
    CCScopeGrant,
    CCSite,
)
from harkeniq_cc.runtime import AppState

from tests.unit.cc.conftest import seed_tenant_admin

DSN = os.environ.get("HARKEN_TEST_CC_PG_DSN", "")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not DSN, reason="HARKEN_TEST_CC_PG_DSN not set"),
]


class FakeSM:
    calls: list = []
    raise_: bool = False

    def __init__(self, *_a, **_kw):
        pass

    async def dispatch_action(self, endpoint, token, **kw):
        if FakeSM.raise_:
            raise ConnectionError("site manager unreachable")
        FakeSM.calls.append(kw)
        return {"accepted": True, "directive_id": uuid.uuid4().hex, "reason": ""}


@pytest.fixture(autouse=True)
def _fake_sm(monkeypatch):
    FakeSM.calls, FakeSM.raise_ = [], False
    monkeypatch.setattr("harkeniq_cc.agent_runtime.SMClient", FakeSM)
    monkeypatch.setattr("harkeniq_cc.api.approvals.SMClient", FakeSM)
    yield


async def _estate(sessionmaker, tenant: str) -> tuple[str, str]:
    """One site, one device with an open disk incident, one agent bound to
    IDENTIFY_LED with a site grant. Returns (agent_id, site_id)."""
    caps = {"reach_known": True, "implemented": ["IDENTIFY_LED"],
            "allowed": ["IDENTIFY_LED"], "effective": ["IDENTIFY_LED"]}
    async with sessionmaker() as session:
        site = CCSite(tenant_id=tenant, site_name=f"pg-{uuid.uuid4().hex[:6]}",
                      sm_endpoint="sm:50051", sm_token="tok")
        session.add(site)
        await session.flush()
        node = f"node-{uuid.uuid4().hex[:8]}"
        session.add_all([
            CCFleetCache(
                site_id=site.id, agent_id=node, agent_name="pg-node",
                vendor="Dell", model="R750", device_class="server",
                observation="observed", health="Critical", capabilities=caps,
            ),
            CCIncident(
                incident_id=f"inc-{uuid.uuid4().hex[:8]}", tenant_id=tenant,
                site_id=site.id, kind="device", status="open",
                title="disk failing", device_agent_id=node, subsystem="disk",
                confidence=0.9,
                components=[{"component": "Disk.Bay.1", "severity": "CRITICAL"}],
            ),
        ])
        agent = CCOperationalAgent(
            tenant_id=tenant, name=f"pg-agent-{uuid.uuid4().hex[:6]}",
            status="active", version=1, activated_version=1,
            created_by="pg-test", autonomy_ceiling=0,
        )
        session.add(agent)
        await session.flush()
        session.add_all([
            CCScopeGrant(
                tenant_id=tenant, principal_type="agent", principal_ref=agent.id,
                scope_type="site", scope_ref=site.id, granted_by="pg-test",
            ),
            CCAgentCapability(
                agent_id=agent.id, tenant_id=tenant, kind="action_class",
                capability_ref="IDENTIFY_LED",
            ),
        ])
        await session.commit()
        return agent.id, site.id


async def _set_expiry(sessionmaker, agent_id: str, delta):
    async with sessionmaker() as session:
        await session.execute(
            sa.update(CCScopeGrant)
            .where(CCScopeGrant.principal_ref == agent_id)
            .values(expires_at=(
                None if delta is None else datetime.now(timezone.utc) + delta
            ))
        )
        await session.commit()


async def _cleanup(sessionmaker, tenant: str):
    """Every tenant-keyed row this test wrote, except the audit chain."""
    async with sessionmaker() as session:
        await session.execute(sa.text(
            "DELETE FROM cc_fleet_cache WHERE site_id IN "
            "(SELECT id FROM cc_sites WHERE tenant_id = :t)"), {"t": tenant})
        await session.commit()
    tables = [
        r[0] for r in (await _tenant_tables(sessionmaker))
        if r[0] != "cc_audit_log"
    ]
    # FK order is not known generically; retry until a pass makes no
    # progress. Each DELETE is its own transaction.
    pending = list(tables)
    for _ in range(len(pending) + 1):
        failed = []
        for table in pending:
            try:
                async with sessionmaker() as session:
                    await session.execute(
                        sa.text(f"DELETE FROM {table} WHERE tenant_id = :t"),
                        {"t": tenant},
                    )
                    await session.commit()
            except Exception:  # noqa: BLE001 - FK order, retried below
                failed.append(table)
        if not failed or failed == pending:
            break
        pending = failed


async def _tenant_tables(sessionmaker):
    async with sessionmaker() as session:
        return (await session.execute(sa.text(
            "SELECT table_name FROM information_schema.columns "
            "WHERE column_name = 'tenant_id' AND table_schema = 'public' "
            "AND table_name LIKE 'cc_%'"
        ))).all()


async def _app(sessionmaker, engine, tenant: str):
    config = CCConfig(tenant_id=tenant, insecure=True)
    configure_auth("", "", "", insecure=True)
    state = AppState(config=config, engine=engine, sessionmaker=sessionmaker)
    app = create_app(state)
    await seed_tenant_admin(sessionmaker, tenant, "kc-owner")

    async def _fake():
        return UserContext(
            user_id="kc-owner", email="owner@example.com", tenant_id=tenant,
            role="tenant_owner",
            permissions=list(ROLE_PERMISSIONS["tenant_owner"]),
        )

    app.dependency_overrides[get_current_user] = _fake
    return app, state


@pytest.mark.parametrize("path", ["sync", "background"])
@pytest.mark.parametrize("label,delta,dispatches", [
    ("expired by one hour", timedelta(hours=-1), False),
    ("expired by one second", timedelta(seconds=-1), False),
    ("expiring in one hour", timedelta(hours=1), True),
    ("no expiry at all", None, True),
])
async def test_dispatch_revalidates_expiry_on_a_real_server(
    path, label, delta, dispatches,
):
    """Admitted under live authority; the grant's expiry is then set; the
    approved proposal either crosses CC -> SM or does not -- on the path
    under test, decided by a timestamptz comparison on PostgreSQL."""
    tenant = f"a30d-{uuid.uuid4().hex[:8]}"
    engine = make_engine(DSN)
    sessionmaker = make_sessionmaker(engine)
    try:
        app, state = await _app(sessionmaker, engine, tenant)
        agent_id, _site = await _estate(sessionmaker, tenant)

        created = await agent_runtime.evaluate_agents(state, tenant)
        assert len(created) == 1, "admission under live authority"
        pid = created[0].id

        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        )
        async with client as c:
            if path == "background":
                FakeSM.raise_ = True
                res = await c.post(f"/api/approvals/{pid}/approve")
                assert res.status_code == 200, res.text
                FakeSM.raise_ = False
                await _set_expiry(sessionmaker, agent_id, delta)
                await agent_runtime.dispatch_decided(state, tenant)
            else:
                await _set_expiry(sessionmaker, agent_id, delta)
                res = await c.post(f"/api/approvals/{pid}/approve")
                assert res.status_code == 200, res.text

        async with sessionmaker() as session:
            row = await session.get(CCAgentProposal, pid)
        if dispatches:
            assert len(FakeSM.calls) == 1, label
            assert row.status == "dispatched", label
        else:
            assert FakeSM.calls == [], f"{path}: {label} crossed CC -> SM"
            assert row.status != "dispatched"
            assert "no longer reaches" in (row.dispatch_reason or "")
            # History intact: the human who approved it is still named.
            assert row.decided_by == "owner@example.com"
    finally:
        await _cleanup(sessionmaker, tenant)
        await engine.dispose()
