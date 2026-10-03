"""A30.40 on a REAL PostgreSQL: predictive / privacy projection hardening.

The sqlite proof lives in tests/unit/cc/test_a30_40_predictive_privacy.py and
carries the contract. This file asks what only the production engine can
answer, because the fix is SQL:

* B0b's owner predicate on the predictive outcome read -- the correlated
  `EXISTS` and `lower(device_class)` -- compiled and run on PostgreSQL, in
  the WHERE, before the 50k window: hidden rows can neither enter a scoped
  reader's answer nor push one of its rows out;
* deletion equivalence for the predictive route and scoped-human Attention,
  against a TENANT reader of an independently reduced estate, on this
  engine's types, ordering and collation;
* lapsed grants -- a `timestamptz` expiry and a revocation -- and a JSONB
  `permission_subset` without `fleet.view` read zero predictive evidence;
* a tenant-wide machine's learning is bounded on this engine too.

Each stack owns a tenant and a suffix for every id it seeds, because the
database is shared and migrated, not created. Gated on
``HARKEN_TEST_CC_PG_DSN``.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import insert

from harkeniq_cc.db.base import make_engine
from harkeniq_cc.db.models import CCOutcomeHistory

from tests.unit.cc import a30_40_estate as P
from tests.unit.cc import b2_2_estate as E

DSN = os.environ.get("HARKEN_TEST_CC_PG_DSN", "")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not DSN, reason="HARKEN_TEST_CC_PG_DSN not set"),
]

GRID = {0.0, 0.25, 0.5, 0.75, 1.0}
COUNT_TEXT = re.compile(r"\(\s*\d+\s*/\s*\d+\s*\)|\b\d+ of \d+ attempts\b")


async def _estate(keep=None):
    tag = uuid.uuid4().hex[:8]
    return await P.build(keep, engine=make_engine(DSN), tenant=f"a3040-{tag}", tag=tag)


def _norm(stack, payload):
    """The payload with this stack's per-run suffix and the clock removed, so
    two stacks in one shared database can be compared byte for byte."""
    text = json.dumps(payload, sort_keys=True).replace(f"-{stack.tag}", "")
    return E.normalised(json.loads(text))


@pytest.mark.parametrize("name", [
    "h-site-a", "h-org-ab", "h-device-a2", "h-device-mv", "h-class-server",
    "h-mixed", "h-mixed-incident",
])
async def test_deletion_equivalence_on_postgres(name):
    full = await _estate()
    reduced = await _estate(P.visible(name))
    for q in ({}, {"band": "high"}, {"band": "insufficient_data"}):
        scoped = _norm(full, await P.predictive(full, name, **q))
        tenant = _norm(reduced, await P.predictive(reduced, "h-owner", **q))
        assert scoped == tenant, (name, q)
        scoped = _norm(full, await P.attention(full, name, **q))
        tenant = _norm(reduced, await P.attention(reduced, "h-owner", **q))
        assert P.comparable(scoped) == P.comparable(tenant), (name, q)
    # NON-VACUITY: the tenant control reads the difference.
    assert _norm(full, await P.predictive(full, "h-owner")) != _norm(
        reduced, await P.predictive(reduced, "h-owner"))


@pytest.mark.parametrize("name", ["h-revoked", "h-expired", "h-lapsed-all", "h-approve-only",
                                  "h-no-grant"])
async def test_zero_reach_reads_zero_predictive_evidence_on_postgres(name):
    """`h-expired` lapses by `timestamptz` arithmetic, `h-approve-only` holds a
    JSONB `permission_subset` without `fleet.view`."""
    full = await _estate()
    body = await P.predictive(full, name)
    assert (body["risks"], body["devices_scored"], body["outcomes_considered"]) == ([], 0, 0)
    assert (await P.attention(full, name))["items"] == []
    assert (await P.predictive(full, "h-owner"))["outcomes_considered"] == P.SENTINEL_TOTAL


async def test_the_owner_predicate_runs_before_the_window_on_postgres():
    """50000 hidden rows OLDER than every site-A row: a site-A reader's answer
    does not move, and the tenant control shows the window exists."""
    full = await _estate()
    before = _norm(full, await P.predictive(full, "h-site-a"))
    base = P.T0 - timedelta(days=400)
    async with full.sessionmaker() as session:
        for start in range(0, 50_000, 10_000):
            await session.execute(insert(CCOutcomeHistory), [
                {"site_id": full.site("C"), "action_id": full.tagged(f"pgwin-{k}"),
                 "action_type": "SEL_CLEAR", "device_agent_id": P.agent(full, "c1"),
                 "vendor": "Dell", "model": E.R750, "outcome": "FAILURE",
                 "fault_resolved": False, "actor": "a3040",
                 "recorded_at": base + timedelta(seconds=k), "ingested_at": base}
                for k in range(start, start + 10_000)
            ])
        await session.commit()
    assert _norm(full, await P.predictive(full, "h-site-a")) == before
    assert (await P.predictive(full, "h-owner"))["outcomes_considered"] == 50_000


async def test_one_hidden_member_moves_nothing_on_postgres():
    full = await _estate()
    before = _norm(full, await P.predictive(full, "h-class-server"))
    await P.add_hidden(full, "FAILURE", 1, site="A")   # site-owned at A: not a class grant's
    await P.add_hidden(full, "SUCCESS", 2, site="C")
    assert _norm(full, await P.predictive(full, "h-class-server")) == before
    assert (await P.predictive(full, "h-owner"))["outcomes_considered"] == P.SENTINEL_TOTAL + 2


async def test_a_tenant_wide_machine_reads_bounded_learning_on_postgres():
    full = await _estate()
    body = await E.read(full, "m-tenant")
    seen = 0
    for item in body["items"]:
        for entry in item["prior_learning"] + item["fleet_patterns"]:
            text = (entry.get("statement") or entry.get("description"))["text"]
            assert entry["confidence"] in GRID, entry
            assert not COUNT_TEXT.search(text) and E.HIDDEN_COUNT not in text, text
            seen += 1
    assert seen
