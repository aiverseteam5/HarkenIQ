"""A6-4B0a (A30) on a REAL PostgreSQL: expiry is a timestamptz question.

Why this cannot be proved on sqlite
-----------------------------------
`expires_at` is a timezone-aware timestamp, and sqlite stores it as text
with no timezone semantics at all. The A26 slice already hit this: a
lifecycle rule that passes on sqlite can behave differently on the engine
production runs, and an authorization lifecycle is exactly the wrong
place to find that out. So the boundary case that decides reach -- a
timestamp one second in the past versus one second in the future --
is asserted against a real server.

Gated on ``HARKEN_TEST_CC_PG_DSN``, e.g. from the full-stack compose:

    postgresql+asyncpg://harkeniq:harkeniq@localhost:5432/harkeniq_cc
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from harkeniq_cc.db.base import make_engine, make_sessionmaker
from harkeniq_cc.db.models import CCFleetCache, CCOperationalAgent, CCScopeGrant, CCSite
from harkeniq_cc.db.repos import OperationalAgentRepo
from harkeniq_cc.governance import load_agent_reach

DSN = os.environ.get("HARKEN_TEST_CC_PG_DSN", "")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not DSN, reason="HARKEN_TEST_CC_PG_DSN not set"),
]


async def _fixture(sessionmaker, tenant: str, *, expires):
    """One tenant, one site, one device, one agent, one grant."""
    async with sessionmaker() as session:
        site = CCSite(
            tenant_id=tenant, site_name=f"pg-{uuid.uuid4().hex[:6]}",
            sm_endpoint="sm:50051", sm_token="tok",
        )
        session.add(site)
        await session.flush()
        session.add(CCFleetCache(
            site_id=site.id, agent_id=f"node-{uuid.uuid4().hex[:8]}",
            agent_name="pg-node", vendor="Dell", model="R750",
            device_class="server", observation="observed", health="Critical",
        ))
        agent = CCOperationalAgent(
            tenant_id=tenant, name=f"pg-agent-{uuid.uuid4().hex[:6]}",
            status="active", version=1, activated_version=1,
            created_by="pg-test", autonomy_ceiling=0,
        )
        session.add(agent)
        await session.flush()
        session.add(CCScopeGrant(
            tenant_id=tenant, principal_type="agent", principal_ref=agent.id,
            scope_type="site", scope_ref=site.id, granted_by="pg-test",
            expires_at=expires,
        ))
        await session.commit()
        return agent.id, site.id


async def _reach(sessionmaker, tenant, agent_id, site_id):
    async with sessionmaker() as session:
        from harkeniq_cc.db.repos import FleetCacheRepo

        devices = await FleetCacheRepo(session).list_by_site(site_id)
        reach = await load_agent_reach(
            session, tenant_id=tenant, agent_id=agent_id, devices=devices,
        )
        return [d.agent_id for d in reach.devices]


async def _cleanup(sessionmaker, tenant: str):
    async with sessionmaker() as session:
        for table in ("cc_scope_grants", "cc_operational_agents"):
            await session.execute(
                sa.text(f"DELETE FROM {table} WHERE tenant_id = :t"),
                {"t": tenant},
            )
        await session.execute(
            sa.text(
                "DELETE FROM cc_fleet_cache WHERE site_id IN "
                "(SELECT id FROM cc_sites WHERE tenant_id = :t)"
            ),
            {"t": tenant},
        )
        await session.execute(
            sa.text("DELETE FROM cc_sites WHERE tenant_id = :t"), {"t": tenant}
        )
        await session.commit()


@pytest.mark.parametrize("label,delta,expected_devices", [
    ("expired by one hour", timedelta(hours=-1), 0),
    ("expired by one second", timedelta(seconds=-1), 0),
    ("expiring in one hour", timedelta(hours=1), 1),
    ("no expiry at all", None, 1),
])
async def test_expiry_decides_reach_on_a_real_server(
    label, delta, expected_devices
):
    """The boundary, on the engine production runs.

    One second either side of `now` is the whole rule. If the driver, the
    column type or the comparison lost the timezone, this is where it
    shows -- and it decides whether an Operational Agent may act.
    """
    tenant = f"a30-{uuid.uuid4().hex[:8]}"
    engine = make_engine(DSN)
    sessionmaker = make_sessionmaker(engine)
    try:
        expires = (
            None if delta is None
            else datetime.now(timezone.utc) + delta
        )
        agent_id, site_id = await _fixture(sessionmaker, tenant, expires=expires)
        devices = await _reach(sessionmaker, tenant, agent_id, site_id)
        assert len(devices) == expected_devices, label
    finally:
        await _cleanup(sessionmaker, tenant)
        await engine.dispose()


async def test_clear_scopes_retires_an_expired_row_on_postgres():
    """The administrative read, proved where it matters.

    A lifecycle-filtered read cannot see a lapsed row, and a row nobody
    can see is a row nobody can retire. On a real server this also proves
    the UPDATE lands and the revocation is durable across sessions.
    """
    tenant = f"a30-{uuid.uuid4().hex[:8]}"
    engine = make_engine(DSN)
    sessionmaker = make_sessionmaker(engine)
    try:
        agent_id, site_id = await _fixture(
            sessionmaker, tenant,
            expires=datetime.now(timezone.utc) - timedelta(days=1),
        )
        # Reach is already zero -- the grant has lapsed.
        assert await _reach(sessionmaker, tenant, agent_id, site_id) == []

        async with sessionmaker() as session:
            repo = OperationalAgentRepo(session)
            rows = await repo.list_administrative_scope_rows(agent_id)
            assert len(rows) == 1, "the lapsed row must remain administrable"
            assert rows[0].revoked_at is None
            assert await repo.clear_scopes(agent_id, revoked_by="pg-test") == 1
            await session.commit()

        async with sessionmaker() as session:
            repo = OperationalAgentRepo(session)
            assert await repo.list_administrative_scope_rows(agent_id) == []

        async with sessionmaker() as session:
            revoked = (await session.execute(
                sa.text(
                    "SELECT revoked_at, revoked_by FROM cc_scope_grants "
                    "WHERE principal_ref = :a"
                ),
                {"a": agent_id},
            )).one()
            assert revoked[0] is not None, "the row was deleted, not retired"
            assert revoked[1] == "pg-test"
    finally:
        await _cleanup(sessionmaker, tenant)
        await engine.dispose()
