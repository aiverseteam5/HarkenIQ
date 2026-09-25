"""A30.32 (A6-4B1) on a REAL PostgreSQL: governed discovery.

The sqlite proof lives in tests/unit/cc/test_a30_32_governed_discovery.py and
carries the contract. This file asks what only the production engine can
answer about the SAME estate:

* the facts discovery composes are read out of JSONB -- a node's capability
  declaration, a site's error budgets and suppressions -- and have to survive
  a real round trip to be counted at all;
* an agent grant's lifecycle is timestamptz: an expired grant must reach
  nothing, and discovery must still answer the agent (A30.20);
* the catalogue's lazy seed takes a transaction-scoped advisory lock on
  PostgreSQL (a no-op on sqlite) and discovery rolls it back: the lock must
  release with the transaction and the tenant must be left unseeded;
* the doubly-declared route guard charges exactly one read per request on
  the engine production runs.

Each run owns a tenant and a suffix for every id it seeds, because the
database is shared and migrated, not created. Gated on
``HARKEN_TEST_CC_PG_DSN``.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest
import sqlalchemy as sa

from harkeniq_cc.db.base import make_engine
from harkeniq_cc.db.models import (
    CCAgentProposal, CCAgentReadWindow, CCCapabilityCatalogue,
)

from tests.unit.cc import s3_estate as E
from tests.unit.cc.s3_estate import ALL
from tests.unit.cc.test_a30_32_governed_discovery import (
    PREFIX, _agent, _discover, _estate as _sqlite_estate, _grant, klass, walk,
)

DSN = os.environ.get("HARKEN_TEST_CC_PG_DSN", "")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not DSN, reason="HARKEN_TEST_CC_PG_DSN not set"),
]


async def _estate(sites=ALL):
    tag = uuid.uuid4().hex[:8]
    stack = await _sqlite_estate(
        sites, engine=make_engine(DSN), tenant=f"b1-{tag}", tag=tag,
    )
    return stack


async def _machine(stack, name, grants, classes=("SEL_CLEAR", "IDENTIFY_LED")):
    agent_id = f"b1{stack.tag}{name}"[:32]
    await _agent(stack, agent_id, classes)
    grant_ids = [await _grant(stack, agent_id, t, k) for t, k in grants]
    return agent_id, grant_ids


async def test_native_reach_and_the_pre_e1_semantics_on_postgres():
    stack = await _estate()
    tenant_agent, _ = await _machine(stack, "t", [("tenant", "")])
    site_agent, _ = await _machine(stack, "a", [("site", "A")])
    device_agent, _ = await _machine(stack, "d", [("device", "C")])
    class_agent, _ = await _machine(stack, "k", [("device_class", "switch")])

    tenant = await _discover(stack, tenant_agent)
    # C's drop-back came back out of JSONB: the tenant-wide agent reads what
    # admission reads, so SEL_CLEAR definitively needs a human.
    sel = klass(tenant, "SEL_CLEAR")
    assert sel["governance"]["conclusion"] == "requires_approval"
    assert {"code": "error_budget_dropped_back", "scope": "site",
            "site_id": stack.site("C")} in sel["governance"]["reason_codes"]
    assert sel["approval_required"]["state"] == "required"
    assert tenant["scope"]["devices_in_reach"] == 4

    site = await _discover(stack, site_agent)
    sel = klass(site, "SEL_CLEAR")
    assert sel["governance"]["conclusion"] == "autonomous"
    assert sel["approval_required"]["state"] == "unknown"
    assert sel["currently_operable"]["state"] == "unknown"
    assert E.leaks(site, ("A",)) == []
    # The JSONB capability declaration was read: A permits SEL_CLEAR.
    assert sel["in_effective_scope"]["state"] == "available"

    device = await _discover(stack, device_agent)
    assert device["scope"]["reach"]["device_ids"] == [stack.device("C")]
    assert device["scope"]["reach"]["site_ids"] == []
    assert device["governance_basis"]["sites_in_composition"] == 0
    assert stack.site("C") not in json.dumps(device)

    klass_body = await _discover(stack, class_agent)
    assert klass_body["scope"]["reach"]["device_classes"] == ["switch"]
    assert klass(klass_body, "SEL_CLEAR")["in_effective_scope"]["state"] \
        == "no_effective_reach"


async def test_an_expired_grant_reaches_nothing_and_is_still_answered():
    stack = await _estate(("A",))
    agent_id, (grant_id,) = await _machine(stack, "x", [("site", "A")])
    assert (await _discover(stack, agent_id))["scope"]["reach"]["site_ids"] \
        == [stack.site("A")]
    await stack.lapse(grant_id, how="expired")
    body = await _discover(stack, agent_id)
    assert body["scope"]["empty"] is True
    assert body["scope"]["reach"]["site_ids"] == []
    assert klass(body, "SEL_CLEAR")["currently_operable"]["state"] == "not_operable"


async def test_the_lazy_seed_is_rolled_back_under_the_real_advisory_lock():
    stack = await _estate(("A",))
    agent_id, _ = await _machine(stack, "s", [("site", "A")])

    async def _catalogue_rows():
        async with stack.sessionmaker() as session:
            return (await session.execute(
                sa.select(sa.func.count()).select_from(CCCapabilityCatalogue)
                .where(CCCapabilityCatalogue.tenant_id == stack.tenant)
            )).scalar_one()

    assert await _catalogue_rows() == 0
    for _ in range(3):
        body = await _discover(stack, agent_id)
        assert klass(body, "SEL_CLEAR")["addressable"]["path"] == "condition_catalogue"
    # Released with each transaction (a held lock would hang the next read)
    # and nothing committed.
    assert await _catalogue_rows() == 0
    async with stack.sessionmaker() as session:
        proposals = (await session.execute(
            sa.select(sa.func.count()).select_from(CCAgentProposal)
            .where(CCAgentProposal.tenant_id == stack.tenant)
        )).scalar_one()
    assert proposals == 0


async def test_each_read_costs_exactly_one_on_postgres():
    stack = await _estate(("A",))
    agent_id, _ = await _machine(stack, "m", [("site", "A")])

    async def _reads():
        async with stack.sessionmaker() as session:
            return int((await session.execute(
                sa.select(sa.func.coalesce(sa.func.sum(CCAgentReadWindow.reads), 0))
                .where(CCAgentReadWindow.tenant_id == stack.tenant,
                       CCAgentReadWindow.agent_id == agent_id)
            )).scalar_one())

    before = await _reads()
    async with stack.as_machine(agent_id).client() as c:
        for _ in range(4):
            assert (await c.get(f"{PREFIX}/{agent_id}/discovery")).status_code == 200
    assert await _reads() == before + 4


async def test_deletion_equivalence_on_postgres():
    """The site-A agent's discovery over the full estate equals its
    discovery over an estate holding only site A -- ids normalised for the
    per-run suffix, which is the only difference two runs may have."""
    full = await _estate(ALL)
    alone = await _estate(("A",))
    bodies = []
    for stack in (full, alone):
        agent_id, _ = await _machine(stack, "e", [("site", "A")])
        body = await _discover(stack, agent_id)
        body.pop("generated_at")
        text = json.dumps(body, sort_keys=True).replace(stack.tag, "<tag>")
        bodies.append(json.loads(text))
    assert bodies[0] == bodies[1]
    keys, _ = walk(bodies[0])
    assert "learning" not in keys and "evidence" not in keys
