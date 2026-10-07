"""A30.35 (A6-4B2-2) on a REAL PostgreSQL: the machine Attention contract.

The sqlite proof lives in tests/unit/cc/test_a30_35_machine_attention.py and
carries the contract. This file asks what only the production engine can
answer about the SELECTION, which is SQL:

* B0b's owner predicate on outcomes is in the WHERE, before the row limit,
  on the engine production runs -- hidden rows cannot fill the window;
* the held-device incident read, and its empty-set case, on PostgreSQL;
* the learned-signal and fleet-pattern reads carry no window;
* deletion equivalence and ordering hold on this engine's collation and
  types, not only sqlite's;
* `timestamptz` hands back ZONED readings, and `/runtime` and Attention
  still ask one rule;
* the exactly-once charge, with real transactions and the real window table.

Each stack owns a tenant and a suffix for every id it seeds, because the
database is shared and migrated, not created. Gated on
``HARKEN_TEST_CC_PG_DSN``.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from harkeniq_cc.db.base import make_engine
from harkeniq_cc.db.models import (
    CCAgentReadWindow,
    CCFleetCache,
    CCFleetPattern,
    CCLearnedSignal,
    CCOutcomeHistory,
)

from tests.unit.cc import b2_2_estate as E

DSN = os.environ.get("HARKEN_TEST_CC_PG_DSN", "")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not DSN, reason="HARKEN_TEST_CC_PG_DSN not set"),
]


async def _estate(keep=None):
    tag = uuid.uuid4().hex[:8]
    return await E.build(keep, engine=make_engine(DSN), tenant=f"b22-{tag}", tag=tag)


def _norm(stack, payload) -> dict:
    """The payload with this stack's per-run suffix and the clock removed, so
    two stacks in one shared database can be compared byte for byte."""
    text = json.dumps(payload, sort_keys=True).replace(f"-{stack.tag}", "")
    return E.normalised(json.loads(text))


async def _reads(stack, agent_id):
    async with stack.sessionmaker() as session:
        return int((await session.execute(
            sa.select(sa.func.coalesce(sa.func.sum(CCAgentReadWindow.reads), 0))
            .where(CCAgentReadWindow.tenant_id == stack.tenant,
                   CCAgentReadWindow.agent_id == agent_id)
        )).scalar_one())


@pytest.mark.parametrize("name", [
    "m-site-a", "m-device-a1", "m-class-switch", "m-mixed", "m-attention-only",
    "m-lapsed-all",
])
async def test_deletion_equivalence_on_postgres(name):
    full = await _estate()
    reduced = await _estate(E.visible(name))
    n = (await E.read(full, name))["returned"]
    for band in (None, "high", "insufficient_data"):
        for limit in (None, *range(1, n + 2)):
            params = {k: v for k, v in (("band", band), ("limit", limit)) if v is not None}
            assert _norm(full, await E.read(full, name, **params)) == _norm(
                reduced, await E.read(reduced, name, **params)), (name, params)


async def test_the_tenant_wide_control_reads_the_difference_on_postgres():
    full = await _estate()
    reduced = await _estate(E.visible("m-site-a"))
    assert _norm(full, await E.read(full, "m-tenant")) != _norm(
        reduced, await E.read(reduced, "m-tenant"))


async def test_the_outcome_predicate_is_before_the_limit_on_postgres():
    from harkeniq_cc.db.repos import OutcomeHistoryRepo
    from harkeniq_cc.governance import load_scope, machine_attention_selection

    stack = await _estate()
    agent_id = await E.machine(stack, "m-site-a")
    async with stack.sessionmaker() as session:
        for n in range(40):
            session.add(CCOutcomeHistory(
                site_id=stack.site("C"), action_id=stack.tagged(f"old-{n}"),
                action_type="SEL_CLEAR", device_agent_id=E.agent(stack, "c1"),
                vendor="Dell", model="R750", outcome="FAILURE",
                recorded_at=E.T0 - timedelta(days=400), ingested_at=E.T0,
            ))
        await session.commit()
        scope = await load_scope(
            session, tenant_id=stack.tenant, principal_ref=agent_id,
            role_permissions=list(E.ALL_PERMISSIONS), principal_type="agent",
        )
        reach = machine_attention_selection(scope).fleet
        # A30.41: the machine's read is the database's exact statistics --
        # no limit left to cut -- so the hidden rows never enter it at all.
        stats = await OutcomeHistoryRepo(session).device_stats(
            stack.tenant, scope=reach, now=datetime.now(timezone.utc), cohorts=False,
        )
        everything = await OutcomeHistoryRepo(session).list_device_outcome_dicts(
            stack.tenant, limit=5,
        )
    visible = {E.agent(stack, k) for k in ("a1", "a2", "a4", "mv")} | {stack.tagged("b22-ghost")}
    assert stats.devices and set(stats.devices) <= visible
    # The internal decision paths' read is unchanged (Phase 3 owns it): the
    # hidden rows, being the oldest, still fill its window.
    assert {r["device_agent_id"] for r in everything} == {E.agent(stack, "c1")}


async def test_the_held_device_incident_read_on_postgres():
    from harkeniq_cc.db.repos import IncidentRepo
    from harkeniq_cc.governance import (
        held_devices, load_scope, machine_attention_selection,
    )
    from harkeniq_cc.db.repos import FleetCacheRepo

    stack = await _estate()
    agent_id = await E.machine(stack, "m-mixed")
    async with stack.sessionmaker() as session:
        scope = await load_scope(
            session, tenant_id=stack.tenant, principal_ref=agent_id,
            role_permissions=list(E.ALL_PERMISSIONS), principal_type="agent",
        )
        selection = machine_attention_selection(scope)
        devices = await FleetCacheRepo(session).list_all(stack.tenant, scope=selection.fleet)
        held = held_devices(devices, selection.incidents)
        rows = await IncidentRepo(session).list_incidents(
            stack.tenant, status="open", limit=1000, scope=selection.incidents,
            device_agent_ids=held,
        )
        none = await IncidentRepo(session).list_incidents(
            stack.tenant, status="open", limit=1000, scope=selection.incidents,
            device_agent_ids=frozenset(),
        )
    assert held == {E.agent(stack, k) for k in ("a1", "a2", "a3", "a4", "mv")}
    assert {r.incident_id for r in rows} == {
        E.iid(stack, k) for k in ("a1-disk", "a1-child", "a3-fan", "a3-child")}
    assert list(none) == []


async def test_the_learning_reads_carry_no_window_on_postgres():
    stack = await _estate()
    baseline = _norm(stack, await E.read(stack, "m-site-a"))
    async with stack.sessionmaker() as session:
        for n in range(210):
            session.add(CCFleetPattern(
                id=stack.tagged(f"fp{n}"), tenant_id=stack.tenant,
                pattern_type="batch_failure", description=f"SECRET flood {n}",
                affected_scope={"vendor": "Dell", "model": "R750",
                                "action_type": "SEL_CLEAR", "sites": stack.site("C")},
                confidence=0.5, evidence={"site_failure_counts": {stack.site("C"): 7777}},
                status="active", detected_at=E.T0 + timedelta(minutes=n),
            ))
        for n in range(510):
            session.add(CCLearnedSignal(
                id=stack.tagged(f"fs{n}"), tenant_id=stack.tenant,
                signal_key=stack.tagged(f"flood:{n}"), scope_type="site",
                scope_ref=stack.site("C"), action_type="SEL_CLEAR", vendor="Dell",
                model="R750", statement=f"SECRET flood {n}", evidence={},
                confidence=0.99, source_pattern_id="", status="active",
                observation_count=1, first_observed_at=E.T0, last_confirmed_at=E.T0,
            ))
        await session.commit()
    assert _norm(stack, await E.read(stack, "m-site-a")) == baseline


async def test_order_is_a_total_order_on_postgres():
    stack = await _estate()
    for name in ("m-tenant", "m-org-ab", "m-site-a", "m-class-switch"):
        body = await E.read(stack, name)
        assert [i["order"] for i in body["items"]] == list(range(1, body["returned"] + 1))
        for limit in range(1, body["returned"] + 1):
            assert (await E.read(stack, name, limit=limit))["items"] == body["items"][:limit]


async def test_zoned_readings_and_one_rule_through_both_callers():
    stack = await _estate()
    async with stack.sessionmaker() as session:
        row = (await session.execute(sa.select(CCFleetCache).where(
            CCFleetCache.agent_id == E.agent(stack, "a1")))).scalar_one()
    assert row.last_seen_at.tzinfo is not None, "timestamptz came back without a zone"
    agent_id = await E.enter(stack, "m-site-a")
    attention = await E.read(stack, "m-site-a")
    states = [i["freshness"]["state"] for i in attention["items"]]
    assert sorted(states) == ["fresh", "fresh", "stale", "stale", "unknown"]
    assert all(i["freshness"]["snapshot_at"].endswith("+00:00") for i in attention["items"])
    stack.as_person()
    async with stack.client() as client:
        runtime = (await client.get(f"/api/operational-agents/{agent_id}/runtime")).json()
    assert runtime["devices"] == {"in_scope": 5, "seen_recently": 2, "stale": 2,
                                  "never_reported": 1}


async def test_exactly_one_charge_per_read_on_postgres():
    stack = await _estate()
    for name in ("m-site-a", "m-lapsed-all"):
        agent_id = await E.enter(stack, name)
        for params in ({}, {"limit": 1}, {"limit": "0"}):
            before = await _reads(stack, agent_id)
            async with stack.client() as client:
                res = await client.get("/api/attention/", params=params or None)
            assert res.status_code in (200, 422)
            assert await _reads(stack, agent_id) - before == 1, (name, params)
