"""A6-4B0b-S4 (A30.28) on a REAL PostgreSQL: learned-signal payload isolation.

The sqlite proof (tests/unit/cc/test_a30_28_learning_payload_isolation.py)
carries the invariant: the twin estates, the order twin, 22 killed mutants.
This file asks what sqlite cannot answer honestly about the SAME twins:

* a signal's and a pattern's `evidence`, a pattern's `affected_scope`, a
  cycle's `outcomes_before` and an incident's `explanation` are JSONB here.
  JSONB does not keep key order and does not keep Python's types, and the
  projection reads site ids out of KEYS (`site_failure_counts`);
* `confidence` is double precision, not sqlite's REAL-as-text affinity, so
  the band a stored 0.7 falls in is decided by the engine production runs;
* the side channel this slice found is an ORDER BY over `timestamptz`
  values microseconds apart. sqlite stores those as strings. Whether the
  tenant order really flips, and the scoped order really does not, has to
  be seen under a real `ORDER BY detected_at DESC LIMIT n`;
* the frozen copy on a proposal is written by the REAL evaluator under the
  advisory lock `admit_proposal` takes on PostgreSQL.

Every narrowing is paired with a CONTROL that reads the same fact.

Each run owns a tenant and a suffix for every id it seeds, because the
database is shared and migrated, not created. Nothing is cleaned up.

Gated on ``HARKEN_TEST_CC_PG_DSN``.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest

from harkeniq_cc import agent_runtime
from harkeniq_cc.api import approvals as approvals_api
from harkeniq_cc.db.base import make_engine

from tests.unit.cc import s3_estate as E
from tests.unit.cc import s4_estate as S
from tests.unit.cc.test_a30_28_learning_payload_isolation import (
    COUNT_SHAPES, _FakeSM, _agent, _frozen_of, _get, _strings, _surfaces,
)

DSN = os.environ.get("HARKEN_TEST_CC_PG_DSN", "")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not DSN, reason="HARKEN_TEST_CC_PG_DSN not set"),
]


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(agent_runtime, "SMClient", _FakeSM)
    monkeypatch.setattr(approvals_api, "SMClient", _FakeSM)


async def _twin(variant: str) -> E.Stack:
    tag = uuid.uuid4().hex[:8]
    return await S.build(
        variant, engine=make_engine(DSN), tenant=f"s4-{tag}", tag=tag,
    )


async def _bytes(stack, payload) -> str:
    """Canonical bytes with this run's tenant and id suffix removed."""
    text = S.canonical(payload, await S.aliases(stack))
    return text.replace(stack.tenant, "<tenant>").replace(f"-{stack.tag}", "")


class TestTheTwinsOnPostgres:
    async def test_a_site_reader_is_returned_the_same_bytes_and_the_owner_is_not(self):
        scoped, owner = {}, {}
        for variant in ("X", "Y"):
            stack = await _twin(variant)
            incident_id = await S.seed_cited_incident(stack)
            subject, holds = await E.persona(stack, "site_a")
            mine = await _surfaces(stack, subject, incident_id=incident_id)
            assert S.hidden_markers(mine, holds) == []
            for text in _strings(mine):
                for shape in COUNT_SHAPES:
                    assert not shape.search(text), text
            scoped[variant] = {k: await _bytes(stack, v) for k, v in mine.items()}
            theirs = await _surfaces(stack, E.OWNER, "tenant_owner", incident_id)
            owner[variant] = {k: await _bytes(stack, v) for k, v in theirs.items()}
        assert {"incident", "incidents"} <= set(scoped["X"])
        for surface in scoped["X"]:
            assert scoped["X"][surface] == scoped["Y"][surface], surface
        assert [s for s in owner["X"] if owner["X"][s] == owner["Y"][s]] == []

    async def test_jsonb_keys_still_name_the_readers_own_site(self):
        stack = await _twin("X")
        subject, _ = await E.persona(stack, "site_a")
        body = await _get(stack, subject, "/api/learning/signals")
        cohort = next(s for s in body["signals"]
                      if s["signal_key"] == "cohort:dell/r750:SEL_CLEAR")
        assert cohort["evidence"] == {
            "failure_rate": 0.65,
            "site_failure_counts": {stack.site("A"): 3},
            "projection": "scoped",
            "withheld": ["failures", "sites_affected", "total"],
            "partial": ["site_failure_counts"],
        }
        assert (cohort["confidence"], type(cohort["confidence"])) == (1.0, float)


class TestARealOrderBy:
    async def test_timestamptz_order_flips_for_the_owner_only(self):
        seen = {}
        for variant in ("X", "Y"):
            stack = await _twin(variant)
            names = await S.aliases(stack)
            subject, _ = await E.persona(stack, "site_a")

            def order(body, key):
                return [names[r["pattern_id"]] for r in body[key]
                        if "SEL_CLEAR" not in names[r["pattern_id"]]]

            row = []
            for who, role in ((E.OWNER, "tenant_owner"), (subject, "site_admin")):
                row.append((
                    order(await _get(stack, who, "/api/outcomes/patterns", role), "patterns"),
                    order(await _get(stack, who, "/api/learning/cycles", role), "cycles"),
                    [names[p["pattern_id"]] for p in (await _get(
                        stack, who, "/api/outcomes/patterns", role, limit=2))["patterns"]],
                ))
            seen[variant] = row
        assert seen["X"][0] != seen["Y"][0], "the owner must see detection order"
        # The cross-site detector runs last, so LIMIT 1 is that pattern in
        # both twins; LIMIT 2 adds the LAST batch pattern stamped -- the
        # cohort with the smallest tenant total.
        assert seen["X"][0][2] != seen["Y"][0][2], "LIMIT 2 in SQL names the smallest total"
        assert seen["X"][1] == seen["Y"][1]

    async def test_scoped_instants_are_floored_and_keep_their_type(self):
        stack = await _twin("X")
        subject, _ = await E.persona(stack, "site_a")
        owner = await _get(stack, E.OWNER, "/api/outcomes/patterns", "tenant_owner")
        mine = await _get(stack, subject, "/api/outcomes/patterns")
        assert len({p["detected_at"] for p in owner["patterns"]}) == 4
        assert len({p["detected_at"] for p in mine["patterns"]}) == 1
        (instant,) = {p["detected_at"] for p in mine["patterns"]}
        assert instant.endswith(":00+00:00"), instant


class TestTheOwnerIsUnchanged:
    async def test_what_is_stored_is_what_is_returned(self):
        stack = await _twin("X")
        body = await _get(stack, E.OWNER, "/api/learning/signals", "tenant_owner")
        stored = {s.signal_key: s for s in await S.signals(stack)}
        for got in body["signals"]:
            row = stored[got["signal_key"]]
            assert (got["evidence"], got["statement"], got["confidence"]) == (
                row.evidence or {}, row.statement, row.confidence)
        assert "projection" not in json.dumps(body)


class TestTheRealEvaluatorsFrozenCopy:
    async def test_recorded_signals_are_bounded_at_read(self):
        seen = {}
        for variant in ("X", "Y"):
            stack = await _twin(variant)
            agent_id = await _agent(stack, ("A",), name=f"S4 PG {stack.tag}")
            await E.seed_incident(stack, "A")
            stats = await agent_runtime.run_once(stack.state, stack.tenant)
            assert stats["proposed"] == 1, stats
            subject, _ = await E.persona(stack, "site_a")
            path = f"/api/operational-agents/{agent_id}/proposals"
            owner = _frozen_of((await _get(stack, E.OWNER, path, "tenant_owner"))["proposals"][0])
            mine = _frozen_of((await _get(stack, subject, path))["proposals"][0])
            assert any("attempts" in s["statement"] for s in owner)
            assert mine
            for text in _strings(mine):
                for shape in COUNT_SHAPES:
                    assert not shape.search(text), text
            seen[variant] = json.dumps(
                [{k: v for k, v in s.items() if k != "signal_id"} for s in mine]
            ).replace(f"-{stack.tag}", "")
        assert seen["X"] == seen["Y"]
