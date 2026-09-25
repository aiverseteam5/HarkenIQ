"""A30.33 (G12) on a REAL PostgreSQL: tenant-scope agent administration.

The sqlite proof lives in tests/unit/cc/test_a30_33_g12_tenant_scope_admin.py
and carries the contract. This file asks what only the production engine
can answer about the same fix:

* the canonical tenant reference is an EMPTY STRING that survives a real
  round trip as `''` -- not NULL -- and is rebuilt without raising;
* the safety controls' durable effects are timestamptz facts: an identity's
  `revoked_at`, and the `revoked_at` retire stamps on every scope row;
* a lapsed human grant is lapsed by timestamptz arithmetic, and confers
  nothing on the engine production runs.

Each run owns a tenant and a suffix for every id it seeds, because the
database is shared and migrated, not created. Gated on
``HARKEN_TEST_CC_PG_DSN``.
"""

from __future__ import annotations

import os
import uuid

import pytest
import sqlalchemy as sa

from harkeniq_cc.db.base import make_engine
from harkeniq_cc.db.models import CCAgentIdentity, CCScopeGrant
from harkeniq_cc.db.repos import OperationalAgentRepo
from harkeniq_cc.api.operational_agents import _agent_scope_rules
from harkeniq_cc.scope import PRINCIPAL_AGENT

from tests.unit.cc.test_a3_machine_identity import (  # noqa: F401
    FakeConsole, _fake_console,
)
from tests.unit.cc.test_a30_32_governed_discovery import (
    _agent, _estate as _sqlite_estate, _grant,
)
from tests.unit.cc.test_a30_33_g12_tenant_scope_admin import (
    _all_calls, _call, _human, _state,
)
from tests.unit.cc import s3_estate as E

DSN = os.environ.get("HARKEN_TEST_CC_PG_DSN", "")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not DSN, reason="HARKEN_TEST_CC_PG_DSN not set"),
]


async def _estate(sites=("A", "C")):
    tag = uuid.uuid4().hex[:8]
    return await _sqlite_estate(
        sites, engine=make_engine(DSN), tenant=f"g12-{tag}", tag=tag,
    )


async def _tenant_agent(stack, name, **kw) -> str:
    agent_id = f"g12{stack.tag}{name}"[:32]
    await _agent(stack, agent_id, **kw)
    await _grant(stack, agent_id, "tenant")
    return agent_id


async def test_the_empty_reference_round_trips_and_every_control_works():
    stack = await _estate()
    aid = await _tenant_agent(stack, "life")

    async with stack.sessionmaker() as session:
        stored = (await session.execute(
            sa.select(CCScopeGrant.scope_ref).where(
                CCScopeGrant.principal_type == PRINCIPAL_AGENT,
                CCScopeGrant.principal_ref == aid)
        )).scalar_one()
        # An empty string, not NULL, and the rebuild accepts it.
        assert stored == "" and stored is not None
        rules = await _agent_scope_rules(OperationalAgentRepo(session), aid)
        assert [(r.scope_type, r.scope_ref) for r in rules] == [("tenant", "")]

    stack.as_person()
    assert (await _call(stack, aid, "POST", "/identity")).status_code == 200
    res = await _call(stack, aid, "POST", "/identity/revoke", {"reason": "g12 pg"})
    assert res.status_code == 200, res.text
    async with stack.sessionmaker() as session:
        identity = (await session.execute(
            sa.select(CCAgentIdentity).where(CCAgentIdentity.agent_id == aid)
        )).scalar_one()
        assert identity.status == "revoked"
        assert identity.revoked_at is not None and identity.revoked_at.tzinfo is not None

    res = await _call(stack, aid, "POST", "/retire")
    assert res.status_code == 200, res.text
    async with stack.sessionmaker() as session:
        rows = (await session.execute(
            sa.select(CCScopeGrant).where(
                CCScopeGrant.principal_type == PRINCIPAL_AGENT,
                CCScopeGrant.principal_ref == aid)
        )).scalars().all()
        assert [(r.scope_type, r.scope_ref) for r in rows] == [("tenant", "")]
        assert rows[0].revoked_at is not None and rows[0].revoked_at.tzinfo is not None
    assert (await _state(stack, aid))["status"] == "retired"


async def test_a_retired_active_identity_on_postgres():
    stack = await _estate()
    aid = await _tenant_agent(stack, "ret")
    stack.as_person()
    assert (await _call(stack, aid, "POST", "/identity")).status_code == 200
    assert (await _call(stack, aid, "POST", "/retire")).status_code == 200
    state = await _state(stack, aid)
    assert (state["status"], state["identity"]) == ("retired", "retired")
    assert "agent_identity.retired" in state["audit"]


@pytest.mark.parametrize("how", ["expired", "revoked"])
async def test_a_lapsed_human_grant_administers_nothing_on_postgres(how):
    stack = await _estate()
    aid = await _tenant_agent(stack, f"lap{how[0]}")
    subject = f"g12-{stack.tag}-{how}"
    grant_id = await _human(stack, subject, "tenant", role="tenant_owner")
    stack.as_person(subject, "tenant_owner")
    assert (await _call(stack, aid, "PATCH", "", {"description": "live"})).status_code == 200
    await stack.lapse(grant_id, how=how)
    before = await _state(stack, aid)
    codes = await _all_calls(stack, aid)
    assert set(codes.values()) == {403}, codes
    assert await _state(stack, aid) == before


async def test_a_site_administrator_is_refused_and_nothing_moves_on_postgres():
    stack = await _estate()
    aid = await _tenant_agent(stack, "site")
    subject, _ = await E.persona(stack, "site_a")
    stack.as_person(subject, "site_admin")
    before = await _state(stack, aid)
    codes = await _all_calls(stack, aid)
    assert set(codes.values()) == {403}, codes
    assert await _state(stack, aid) == before
