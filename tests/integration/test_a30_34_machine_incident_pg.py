"""A30.34 (A6-4B2-1) on a REAL PostgreSQL: the machine incident contract.

The sqlite proof lives in tests/unit/cc/test_a30_34_machine_incident_contract.py
and carries the contract. This file asks what only the production engine can
answer about it:

* JSONB hands back what a hostile Site Manager wrote -- a provider that is a
  number, an object or a list; nested correlation keys; unicode and control
  characters; components of the wrong type -- and the projection still
  withholds, envelopes and fails closed on exactly those values;
* `timestamptz` comes back zoned, and the contract says so;
* the one new query (`children_index`) agrees with `children_of` on the
  engine production runs;
* site-less ownership and the exactly-once charge hold with real
  transactions and the real read-window table.

Each run owns a tenant and a suffix for every id it seeds, because the
database is shared and migrated, not created. Gated on
``HARKEN_TEST_CC_PG_DSN``.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime

import pytest
import sqlalchemy as sa

from harkeniq_cc.db.base import make_engine
from harkeniq_cc.db.models import CCAgentReadWindow, CCIncident

from tests.unit.cc import b2_estate as B

DSN = os.environ.get("HARKEN_TEST_CC_PG_DSN", "")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not DSN, reason="HARKEN_TEST_CC_PG_DSN not set"),
]

DEEP = "DEEPCORRb2pg"
UNICODE = "Lüfter ✓ ​‮ rtl \t tab"


async def _estate():
    tag = uuid.uuid4().hex[:8]
    return await B.build(engine=make_engine(DSN), tenant=f"b2-{tag}", tag=tag)


async def _get(stack, path, **params):
    async with stack.client() as client:
        return await client.get(path, params=params or None)


async def _reads(stack, agent):
    async with stack.sessionmaker() as session:
        return int((await session.execute(
            sa.select(sa.func.coalesce(sa.func.sum(CCAgentReadWindow.reads), 0))
            .where(CCAgentReadWindow.tenant_id == stack.tenant,
                   CCAgentReadWindow.agent_id == agent)
        )).scalar_one())


async def _hostile(stack, key, provider, **extra):
    incident_id = stack.tagged(f"b2pg-{key}")
    explanation = {
        "summary": f"{UNICODE} {B.GEN}", "confidence": 0.5,
        "reasoning_steps": [UNICODE], "suggested_action": "",
        "generation_visibility": {"scope": "site", "site_id": stack.site("A"),
                                  "projection_version": 1},
    }
    if provider is not ...:
        explanation["provider"] = provider
    explanation.update(extra)
    async with stack.sessionmaker() as session:
        session.add(CCIncident(
            incident_id=incident_id, tenant_id=stack.tenant, site_id=stack.site("A"),
            device_agent_id=stack.device("A"), kind="device", subsystem="disk",
            status="open", title=f"hostile {B.TITLE}",
            correlation_meta={
                B.CORR_KEY: [{DEEP: {"deeper": [B.CORR_VALUE, {B.CORR_PEER: 1}]}}],
                "votes": {B.CORR_PEER: "UNRESPONSIVE"},
            },
            explanation=explanation,
            components=[
                {"component": f"PSU.Slot.1 {B.BMC_HOSTILE}", "severity": "CRITICAL",
                 "skill_name": 7, "at": "2026-09-20T07:59:00"},
                17, None, {"component": ["not", "text"]},
            ],
        ))
        await session.commit()
    return incident_id


@pytest.mark.parametrize("provider,origin", [
    (42, "unknown"), ({"name": "llm"}, "unknown"), (["llm"], "unknown"),
    ("LLM", "unknown"), (..., "unknown"), ("llm", "llm"),
])
async def test_hostile_jsonb_round_trips_and_trust_fails_closed(provider, origin):
    stack = await _estate()
    incident_id = await _hostile(stack, f"p{abs(hash(repr(provider))) % 10_000}", provider)
    agent = await B.machine(stack, "m-tenant")
    stack.as_machine(agent)
    res = await _get(stack, f"/api/incidents/{incident_id}")
    assert res.status_code == 200, res.text
    body = res.json()
    text = json.dumps(body)
    for planted in B.NEVER_ON_A_MACHINE + (DEEP, B.TITLE):
        assert planted not in text, planted
    diagnosis = body["diagnosis"]
    assert diagnosis["origin"] == origin
    assert diagnosis["trust"] == "untrusted_generated"
    # The generated block survives JSONB byte for byte, inside its envelope.
    assert diagnosis["generated"]["summary"] == f"{UNICODE} {B.GEN}"
    assert diagnosis["generated"]["reasoning_steps"] == [UNICODE]
    # The hostile controller's string sits in a telemetry envelope; the
    # entries that are not component objects are dropped, not echoed.
    items = body["components"]["items"]
    assert len(items) == 1
    assert items[0]["reported"] == {
        "trust": "untrusted_telemetry",
        "component": f"PSU.Slot.1 {B.BMC_HOSTILE}"[:256],
        "skill_name": "",
    }
    assert items[0]["at"] == "2026-09-20T07:59:00+00:00"
    assert datetime.fromisoformat(body["as_of"]).tzinfo is not None


async def test_a_malformed_block_is_withheld_on_postgres_too():
    stack = await _estate()
    incident_id = await _hostile(
        stack, "malformed", "llm", reasoning_steps={"not": "a list"},
    )
    agent = await B.machine(stack, "m-tenant")
    stack.as_machine(agent)
    diagnosis = (await _get(stack, f"/api/incidents/{incident_id}")).json()["diagnosis"]
    assert diagnosis["generated"] == {
        "trust": "untrusted_generated", "withheld": True,
        "summary": "", "suggested_action": "", "reasoning_steps": [],
    }
    assert diagnosis["generation_visibility"] is None


async def test_timestamptz_comes_back_zoned():
    stack = await _estate()
    agent = await B.machine(stack, "m-site-a")
    stack.as_machine(agent)
    body = (await _get(stack, f"/api/incidents/{B.iid(stack, 'a-resolved')}")).json()
    assert body["timeline"] == {
        "opened_at": "2026-09-20T08:05:00+00:00",
        "last_seen_at": "2026-09-20T08:35:00+00:00",
        "resolved_at": "2026-09-20T10:00:00+00:00",
    }


@pytest.mark.parametrize("persona", ["m-tenant", "m-org-ab", "m-site-a", "m-device-a"])
async def test_the_list_and_the_detail_report_the_same_relations(persona):
    """`children_index` (one query) and `children_of` agree on PostgreSQL."""
    stack = await _estate()
    agent = await B.machine(stack, persona)
    stack.as_machine(agent)
    listed = (await _get(stack, "/api/incidents/", status="all")).json()
    assert {i["incident_id"] for i in listed["incidents"]} == {
        B.iid(stack, k) for k in B.EXPECTED[persona]
    }
    for item in listed["incidents"]:
        detail = (await _get(stack, f"/api/incidents/{item['incident_id']}")).json()
        assert item["relation"] == detail["relation"], (persona, item["incident_id"])
    if persona == "m-tenant":
        parent = next(i for i in listed["incidents"]
                      if i["incident_id"] == B.iid(stack, "a-parent"))
        assert parent["relation"]["children"] == [
            B.iid(stack, "a-child"), B.iid(stack, "b-child"),
        ]


async def test_siteless_agreement_and_exactly_once_charges_on_postgres():
    stack = await _estate()
    tenant_agent = await B.machine(stack, "m-tenant")
    stack.as_machine(tenant_agent)
    siteless = B.iid(stack, "siteless")
    assert siteless in {i["incident_id"] for i in
                        (await _get(stack, "/api/incidents/", status="all")).json()["incidents"]}
    assert (await _get(stack, f"/api/incidents/{siteless}")).status_code == 200

    narrow = await B.machine(stack, "m-site-a")
    stack.as_machine(narrow)
    assert siteless not in {i["incident_id"] for i in
                            (await _get(stack, "/api/incidents/", status="all")).json()["incidents"]}
    before = await _reads(stack, narrow)
    hidden = await _get(stack, f"/api/incidents/{siteless}")
    middle = await _reads(stack, narrow)
    missing = await _get(stack, f"/api/incidents/b2pg-missing-{stack.tag}")
    after = await _reads(stack, narrow)
    assert hidden.status_code == missing.status_code == 404
    assert hidden.json() == missing.json()
    assert (middle - before, after - middle) == (1, 1)


async def test_the_human_path_on_postgres_is_d5b_and_d6_only():
    stack = await _estate()
    stack.as_person()
    nopro = (await _get(stack, f"/api/incidents/{B.iid(stack, 'a-nopro')}")).json()
    assert nopro["diagnosis"]["origin"] == "unknown"
    assert nopro["diagnosis"]["trust"] == "untrusted_generated"          # D5b
    disk = (await _get(stack, f"/api/incidents/{B.iid(stack, 'a-disk')}")).json()
    rec = disk["recommended_next"]
    assert rec["capability"] == "propose_action" and rec["summary"] == B.SUGGESTION
    assert rec["summary_trust"] == "untrusted_generated"                 # D6
    assert rec["summary_source"] == "diagnosis.generated.suggested_action"
    # Everything else a person read before is still there.
    assert disk["correlation"][B.CORR_KEY] == B.CORR_VALUE
    assert B.TITLE in disk["title"] and "view" not in disk
