"""A30.31 (A6-4B0c) on a REAL PostgreSQL: the guard-level meter under concurrency.

The unit suite proves the matrix on sqlite, whose StaticPool hands every
session ONE shared connection -- so two requests there can never race on
the counter at all. What only the production engine can answer:

* Concurrent machine reads through the REAL app -- served, not found,
  malformed and `self`, every one crossing the doubly-declared guard --
  charge EXACTLY their number: no double charge from the second guard, no
  charge lost to a race.
* A concurrent flood against a nearly-spent window admits EXACTLY the
  remaining allowance and not one read more: the increment is a single
  UPDATE under the row lock, so no two requests can both see room.
* A person's reads charge nothing, on the engine production runs.

Rows are namespaced by a fresh tenant and agent per test. Gated on
``HARKEN_TEST_CC_PG_DSN``; skipped when unset.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import sqlalchemy as sa

from harkeniq_cc import ingress_limits
from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.app import create_app
from harkeniq_cc.auth import ROLE_PERMISSIONS, UserContext, configure_auth
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.models import CCAgentReadWindow, CCOperationalAgent
from harkeniq_cc.ingress_limits import READ_MAX_PER_WINDOW, read_window_start
from harkeniq_cc.route_contract import MACHINE_JOBS
from harkeniq_cc.runtime import AppState

from tests.unit.cc.conftest import seed_tenant_admin

DSN = os.environ.get("HARKEN_TEST_CC_PG_DSN", "")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not DSN, reason="HARKEN_TEST_CC_PG_DSN not set"),
]

EVERYTHING = ["proposal.submit", "fleet.view", "incident.view"]


class Stack:
    def __init__(self, app, state, tenant, agent_id):
        self.app, self.state = app, state
        self.tenant, self.agent_id = tenant, agent_id
        self.sessionmaker = state.sessionmaker

    def as_machine(self):
        async def _fake():
            return UserContext(
                user_id=self.agent_id, email=f"op-agent:{self.agent_id}@v1",
                tenant_id=self.tenant, role="", permissions=list(EVERYTHING),
                species="agent", identity_id="id-pg", machine_jobs=MACHINE_JOBS,
            )

        self.app.dependency_overrides[get_current_user] = _fake
        return self

    def as_person(self):
        async def _fake():
            return UserContext(
                user_id="kc-owner", email="owner@example.com",
                tenant_id=self.tenant, role="tenant_owner",
                permissions=list(ROLE_PERMISSIONS["tenant_owner"]),
            )

        self.app.dependency_overrides[get_current_user] = _fake
        return self

    def client(self):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test",
        )


async def _stack() -> Stack:
    tenant = f"b0c-{uuid.uuid4().hex[:10]}"
    agent_id = uuid.uuid4().hex
    configure_auth("", "", "", insecure=True)
    engine = make_engine(DSN)
    await create_all(engine)
    sessionmaker = make_sessionmaker(engine)
    state = AppState(
        config=CCConfig(tenant_id=tenant, insecure=True),
        engine=engine, sessionmaker=sessionmaker,
    )
    app = create_app(state)
    await seed_tenant_admin(sessionmaker, tenant, "kc-owner")
    async with sessionmaker() as session:
        session.add(CCOperationalAgent(
            id=agent_id, tenant_id=tenant, name=f"b0c {agent_id[:6]}",
            status="active", version=1, activated_version=1,
            created_by="kc-owner",
        ))
        await session.commit()
    return Stack(app, state, tenant, agent_id)


async def _rows(stack) -> list:
    async with stack.sessionmaker() as session:
        return list((await session.execute(
            sa.select(CCAgentReadWindow).where(
                CCAgentReadWindow.tenant_id == stack.tenant,
            )
        )).scalars().all())


@pytest.mark.asyncio
async def test_concurrent_machine_reads_charge_exactly_their_number():
    stack = (await _stack()).as_machine()
    agent = stack.agent_id
    urls = [
        "/api/attention/",                          # served, off the OA router
        "/api/incidents/",                          # served, off the OA router
        "/api/incidents/b0c-does-not-exist",        # 404: charged like a 200
        "/api/attention/?limit=not-a-number",       # 422: refused pre-handler
        f"/api/operational-agents/{agent}/runtime",  # self
    ] * 8                                           # 40 requests
    async with stack.client() as c:
        responses = await asyncio.gather(*(c.get(u) for u in urls))
    codes = sorted({r.status_code for r in responses})
    assert codes == [200, 404, 422], codes
    rows = await _rows(stack)
    assert sum(r.reads for r in rows) == len(urls), [
        (r.window_start, r.reads) for r in rows
    ]
    # At most a minute boundary apart; never one row per request.
    assert 1 <= len(rows) <= 2, len(rows)
    assert {r.agent_id for r in rows} == {agent}


@pytest.mark.asyncio
async def test_a_concurrent_flood_admits_exactly_the_remaining_allowance(
    monkeypatch,
):
    stack = (await _stack()).as_machine()
    window = read_window_start(datetime.now(timezone.utc)) + timedelta(days=3)
    monkeypatch.setattr(
        ingress_limits, "read_window_start", lambda now=None: window,
    )
    already = READ_MAX_PER_WINDOW - 10
    async with stack.sessionmaker() as session:
        session.add(CCAgentReadWindow(
            tenant_id=stack.tenant, agent_id=stack.agent_id,
            window_start=window, reads=already,
        ))
        await session.commit()
    flood = 30
    async with stack.client() as c:
        responses = await asyncio.gather(
            *(c.get("/api/incidents/") for _ in range(flood))
        )
    served = [r for r in responses if r.status_code == 200]
    throttled = [r for r in responses if r.status_code == 429]
    assert len(served) == 10, [r.status_code for r in responses]
    assert len(throttled) == flood - 10
    (row,) = [r for r in await _rows(stack)
              if r.window_start.replace(tzinfo=timezone.utc) == window]
    # Every request moved the counter once -- the 429s included.
    assert row.reads == already + flood


@pytest.mark.asyncio
async def test_a_person_charges_nothing():
    stack = (await _stack()).as_person()
    async with stack.client() as c:
        for url in ("/api/attention/", "/api/incidents/",
                    f"/api/operational-agents/{stack.agent_id}/runtime"):
            assert (await c.get(url)).status_code == 200, url
    assert await _rows(stack) == []
