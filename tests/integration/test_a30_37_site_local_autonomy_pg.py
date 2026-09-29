"""S3-E1 (A30.37) on a REAL PostgreSQL.

The sqlite proofs live in tests/unit/cc/test_a30_37_*.py and carry the
contract. This file asks what only the production engine can answer:

* D11's recovery is a timestamptz comparison -- which preflight row was
  CURRENT at `activated_at` -- and it must pick the same row on the engine
  production runs, including after a later preflight re-run;
* D8's freshness is judged on real zoned values: a current report vouches, a
  stale one does not, and a Site Manager clock running ahead cannot keep a
  dark site fresh;
* a site's local assessment over rows PostgreSQL returned is unmoved by
  another site's halt, drop-back and silence, and equals the assessment of
  an estate where that site does not exist;
* the dispatch gate withholds and releases on the same engine.

Each run owns a tenant and a suffix, because the database is shared and
migrated, not created. Gated on ``HARKEN_TEST_CC_PG_DSN``.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from harkeniq_cc import agent_runtime
from harkeniq_cc import global_safety as G
from harkeniq_cc.agent_lifecycle import activation_approved_unattended
from harkeniq_cc.autonomy import site_reported
from harkeniq_cc.db.base import make_engine
from harkeniq_cc.db.models import (
    CCAgentPreflight,
    CCAgentProposal,
    CCOperationalAgent,
    CCSafetyState,
)
from harkeniq_cc.db.repos import SafetyStateRepo
from harkeniq_cc.governance import load_site_assessments

from tests.unit.cc.test_a30_32_governed_discovery import (
    _agent, _estate as _sqlite_estate, _grant,
)
from tests.unit.cc import s3_estate as E

DSN = os.environ.get("HARKEN_TEST_CC_PG_DSN", "")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not DSN, reason="HARKEN_TEST_CC_PG_DSN not set"),
]


class Toggle:
    member_id = "test_toggle_pg"

    def __init__(self):
        self.on = False

    def evaluate(self, context):
        return G.MemberVerdict.CONSTRAIN if self.on else G.MemberVerdict.CLEAR


@pytest.fixture
def toggle(monkeypatch):
    member = Toggle()
    monkeypatch.setattr(G, "_ACTIVE", (member,))
    return member


async def _estate(sites=("A", "B")):
    tag = uuid.uuid4().hex[:8]
    return await _sqlite_estate(
        sites, engine=make_engine(DSN), tenant=f"s3e1-{tag}", tag=tag,
    )


async def _safety(stack, key, **fields):
    async with stack.sessionmaker() as session:
        row = await session.get(CCSafetyState, stack.site(key))
        for name, value in fields.items():
            setattr(row, name, value)
        await session.commit()


async def _clean(stack, keys):
    for key in keys:
        await _safety(stack, key, suppressions=[], error_budgets=[],
                      site_budgets={"SEL_CLEAR": 5}, sm_stop_switch=False,
                      reported=True, as_of=datetime.now(timezone.utc),
                      ingested_at=datetime.now(timezone.utc))


async def test_d11_recovers_the_activation_time_set_on_postgres():
    stack = await _estate(("A",))
    agent_id = f"s3e1{stack.tag}d11"[:32]
    await _agent(stack, agent_id)
    async with stack.sessionmaker() as session:
        agent = await session.get(CCOperationalAgent, agent_id)
        assert agent.activated_at.tzinfo is not None           # timestamptz
        assert await activation_approved_unattended(
            session, stack.tenant, agent) == frozenset({"SEL_CLEAR"})
        # A preflight re-run AFTER activation does not rewrite what was approved.
        session.add(CCAgentPreflight(
            agent_id=agent_id, tenant_id=stack.tenant, configuration_version=1,
            overall="warn", requires_activation_approval=True,
            result={"unattended_classes": ["BMC_RESET", "SEL_CLEAR"]},
            produced_at=agent.activated_at + timedelta(minutes=1),
        ))
        await session.commit()
    async with stack.sessionmaker() as session:
        agent = await session.get(CCOperationalAgent, agent_id)
        assert await activation_approved_unattended(
            session, stack.tenant, agent) == frozenset({"SEL_CLEAR"})


async def test_freshness_on_real_zoned_values():
    stack = await _estate(("A", "B", "C"))
    now = datetime.now(timezone.utc)
    await _safety(stack, "A", reported=True, as_of=now, ingested_at=now)
    await _safety(stack, "B", reported=True, as_of=now - timedelta(hours=1),
                  ingested_at=now - timedelta(hours=1))
    await _safety(stack, "C", reported=True, as_of=now + timedelta(hours=3),
                  ingested_at=now - timedelta(hours=1))
    async with stack.sessionmaker() as session:
        rows = {r.site_id: r for r in await SafetyStateRepo(session).list_for_tenant(
            stack.tenant)}
    assert rows[stack.site("A")].as_of.tzinfo is not None
    assert site_reported(rows[stack.site("A")], now) is True
    assert site_reported(rows[stack.site("B")], now) is False
    assert site_reported(rows[stack.site("C")], now) is False


async def test_a_site_is_decided_by_itself_on_postgres():
    stack = await _estate(("A", "B"))
    await _clean(stack, ("A", "B"))

    async def local_a():
        async with stack.sessionmaker() as session:
            assessments = await load_site_assessments(
                session, tenant_id=stack.tenant, actor_id="op-agent:pg@v1",
                actor_species="agent", permissions=["fleet.view"],
            )
        contract = json.loads(json.dumps(
            assessments.local_contract((stack.site("A"),)), default=str))
        contract.pop("generated_at")
        for site in contract["scope"]["sites"]:
            site["safety_as_of"] = bool(site["safety_as_of"])
        return contract

    before = await local_a()
    await _safety(stack, "B", sm_stop_switch=True, reported=False,
                  error_budgets=[{"action_type": "SEL_CLEAR", "dropped_back": True,
                                  "total_count": 97531, "success_count": 1,
                                  "failure_count": 97530}],
                  site_budgets={"SEL_CLEAR": 0})
    after = await local_a()
    assert before == after
    assert next(c for c in after["action_classes"]
                if c["action_type"] == "SEL_CLEAR")["disposition"] == "autonomous"
    assert stack.site("B") not in json.dumps(after)
    assert "97531" not in json.dumps(after)


async def test_the_dispatch_gate_withholds_and_releases_on_postgres(toggle, monkeypatch):
    calls = []

    class RecordingSM:
        def __init__(self, *_a, **_kw):
            pass

        async def dispatch_action(self, endpoint, token, **kw):
            calls.append(kw)
            return {"accepted": True, "directive_id": f"d{len(calls)}"}

    monkeypatch.setattr("harkeniq_cc.agent_runtime.SMClient", RecordingSM)
    stack = await _estate(("A",))
    await _clean(stack, ("A",))
    agent_id = f"s3e1{stack.tag}disp"[:32]
    await _agent(stack, agent_id)
    await _grant(stack, agent_id, "site", "A")
    await E.seed_incident(stack, "A")
    created = await agent_runtime.evaluate_agents(stack.state, stack.tenant)
    assert len(created) == 1 and created[0].authorization_basis == "autonomous_grant"

    toggle.on = True
    assert await agent_runtime.dispatch_decided(stack.state, stack.tenant) == []
    assert calls == []
    async with stack.sessionmaker() as session:
        row = (await session.execute(sa.select(CCAgentProposal).where(
            CCAgentProposal.agent_id == agent_id))).scalar_one()
        assert (row.status, row.authorization_basis) == ("approved", "autonomous_grant")
        assert row.dispatch_reason == G.GLOBAL_WITHHELD_REASON

    toggle.on = False
    dispatched = await agent_runtime.dispatch_decided(stack.state, stack.tenant)
    assert len(dispatched) == 1 and len(calls) == 1
