"""PB-2 (A30.42 D-PB7/D-PB8, A30.45) on a REAL PostgreSQL: evaluator reach isolation.

The sqlite proof lives in tests/unit/cc/test_pb2_evaluator_ordering.py. The
selection is SQL -- B0b's owner predicate in the WHERE of the exact outcome
statistics and of the evaluator's incident read, with no row limit -- so the
master oracle is re-run on the engine production uses: an agent's decisions
over the full poisoned estate equal its decisions over an estate reduced to
its own reach, with a tenant-wide control that differs.

Each stack owns a tenant and a suffix for every id it seeds (the database is
shared and migrated). Gated on ``HARKEN_TEST_CC_PG_DSN``.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest

from harkeniq_cc.db.base import make_engine

from tests.unit.cc import b2_2_estate as E
from tests.unit.cc import test_pb2_evaluator_ordering as U

DSN = os.environ.get("HARKEN_TEST_CC_PG_DSN", "")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not DSN, reason="HARKEN_TEST_CC_PG_DSN not set"),
]


async def _estate(name: str, keep=None, **agent_kw):
    tag = uuid.uuid4().hex[:8]
    stack = await E.build(keep, engine=make_engine(DSN), tenant=f"pb2-{tag}", tag=tag)
    await U._agent(stack, name, **agent_kw)
    return stack


def _norm(stack, decisions: dict) -> dict:
    return json.loads(json.dumps(decisions, sort_keys=True).replace(f"-{stack.tag}", ""))


@pytest.mark.parametrize("name", U.SCOPED)
async def test_reduced_estate_equivalence_on_postgres(name):
    full = await _estate(name)
    reduced = await _estate(name, U.reach_keep(name))
    a = _norm(full, await U._decisions(full, refs=False))
    b = _norm(reduced, await U._decisions(reduced, refs=False))
    assert a == b, name
    assert a["admitted"] or a["withheld"], name


async def test_the_tenant_wide_control_differs_on_postgres():
    full = await _estate("tenant")
    reduced = await _estate("tenant", U.reach_keep("site-a"))
    assert _norm(full, await U._decisions(full, refs=False)) != _norm(
        reduced, await U._decisions(reduced, refs=False))


async def test_hidden_incident_volume_on_postgres():
    stack = await _estate("site-a")
    before = _norm(stack, U._dry_view(await U._dry_run(stack), refs=False))
    await U._poison(stack, outcomes=500, incidents=1001)
    after = _norm(stack, U._dry_view(await U._dry_run(stack), refs=False))
    assert after == before


async def test_no_cohort_prior_on_postgres():
    stack = await _estate("tenant")
    bases = {i["confidence"]["basis"] for i in (await U._internal(stack)).values()}
    assert "cohort_prior" not in bases, bases
