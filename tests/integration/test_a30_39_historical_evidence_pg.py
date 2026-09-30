"""S3-E2 (A30.39) on a REAL PostgreSQL: historical proposal evidence.

The sqlite proof lives in tests/unit/cc/test_a30_39_historical_evidence.py and
carries the invariant (the reader matrix, deletion equivalence for every
native reach type, immutability, the golden, the structural pins). This file
asks what sqlite cannot answer honestly about the SAME estate:

* a proposal's `evidence` is JSONB here: key order and Python types do not
  survive, so the stored sentinels, the allow-list and the exact
  track-record clause the rationale projection re-renders from the stored
  statistic all have to hold after a real round trip;
* the viewer track record is the canonical outcome read with B0b's owner
  predicate compiled for this engine -- the correlated `EXISTS` and
  `lower(device_class)` for a device and a device_class reader, the org-unit
  expansion for a region reader -- so the deletion-equivalence oracle is
  re-run on it;
* a grant's lifecycle is timestamptz: a lapsed grant must stop the viewer
  block counting anything while the stored row does not move.

Every narrowing is paired with a CONTROL. Each run owns a tenant and a suffix
for every id it seeds, because the database is shared and migrated, not
created. Gated on ``HARKEN_TEST_CC_PG_DSN``.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest
from sqlalchemy import select

from harkeniq_cc import agent_runtime
from harkeniq_cc.api import approvals as approvals_api
from harkeniq_cc.db.base import make_engine
from harkeniq_cc.db.models import CCScopeGrant
from harkeniq_cc.proposal_evidence import (
    SCOPE_BROADER,
    SCOPE_FULLY_VISIBLE,
    WITHHELD_TRACK_RECORD,
    scoped_rationale,
    withheld_rationale,
)

from tests.unit.cc import s3e2_estate as E
from tests.unit.cc.s3_estate import OWNER
from tests.unit.cc.test_a30_39_historical_evidence import _FakeSM

DSN = os.environ.get("HARKEN_TEST_CC_PG_DSN", "")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not DSN, reason="HARKEN_TEST_CC_PG_DSN not set"),
]


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(agent_runtime, "SMClient", _FakeSM)
    monkeypatch.setattr(approvals_api, "SMClient", _FakeSM)


def _kw() -> dict:
    tag = uuid.uuid4().hex[:8]
    return {"engine": make_engine(DSN), "tenant": f"e2-{tag}", "tag": tag}


async def _queue(stack, subject=OWNER, role="tenant_owner") -> dict:
    res = await stack.as_person(subject, role).get("/api/approvals/")
    assert res.status_code == 200, res.text[:300]
    return {a["proposal"]["proposal_id"]: a["proposal"]
            for a in res.json()["actions"] if a.get("origin") == "agent"}


def _viewer(item) -> dict:
    out = dict(item["viewer_projected_evidence"])
    out.pop("as_of", None)
    return out


async def test_the_stored_sentinels_survive_jsonb_and_reach_only_the_tenant():
    stack, ids = await E.scenario(**_kw())
    owner = await _queue(stack)
    site_a = await E.persona(stack, "site_a")
    mine = await _queue(stack, site_a, "site_admin")
    for label in ("clean", "malformed", "extra_key", "no_evidence", "evaluated"):
        stored = await E.stored(stack, ids[label])
        # CONTROL: the tenant owner reads the JSONB row as stored.
        assert owner[ids[label]]["evidence"] == (stored.evidence or {})
        assert owner[ids[label]]["rationale"] == stored.rationale
        assert owner[ids[label]]["evidence_scope"] == SCOPE_FULLY_VISIBLE
        item = mine[ids[label]]
        assert item["evidence"]["outcome_evidence"] is None
        assert item["evidence"]["attention"] is None
        assert item["evidence_scope"] == SCOPE_BROADER
        assert E.sentinel_hits(item) == [], (label, E.sentinel_hits(item))
        assert item["rationale"] == scoped_rationale(
            stored.rationale, stored.evidence, action_type=stored.action_type,
            device_agent_id=stored.device_agent_id,
        )
    assert E.sentinel_hits(owner[ids["clean"]]) != []
    # The exact clause re-rendered from the JSONB statistic is removed ...
    assert mine[ids["clean"]]["rationale"] == E.HEAD + WITHHELD_TRACK_RECORD
    # ... and the malformed sentence fails closed.
    assert mine[ids["malformed"]]["rationale"] == withheld_rationale(
        "SEL_CLEAR", stack.tagged("node-s3-a"))
    assert "legacy_tenant_stat" in mine[ids["extra_key"]]["evidence"]["withheld"]


@pytest.mark.parametrize("name", sorted(
    n for n in E.PERSONAS if E.has_fleet_view_reach(n)))
async def test_the_viewer_block_is_deletion_equivalent_on_postgres(name):
    full = await E.build(**_kw())
    subject = await E.persona(full, name)
    mine = await E.evidence_view(full, subject)
    deleted = await E.build(keep=E.keep_for(name), **_kw())
    oracle = await E.evidence_view(deleted, OWNER, "tenant_owner")
    for action in ("SEL_CLEAR", "BMC_RESET"):
        got, want = mine.viewer(action), oracle.viewer(action)
        got.pop("as_of"), want.pop("as_of")
        assert got == want, (name, action)
        assert got["outcome_evidence"] == E.expected_track_record(name, action)


async def test_the_owner_is_the_control_on_postgres():
    stack = await E.build(**_kw())
    owner = await E.evidence_view(stack, OWNER, "tenant_owner")
    assert owner.viewer("SEL_CLEAR")["outcome_evidence"] == \
        E.expected_track_record("tenant")
    assert owner.viewer("SEL_CLEAR")["outcome_evidence"]["executions"] == 40


@pytest.mark.parametrize("how", ["expired", "revoked"])
async def test_a_lapsed_timestamptz_grant_counts_nothing_and_moves_nothing(how):
    stack, ids = await E.scenario(**_kw())
    before = await E.stored_bytes(stack)
    subject = await E.persona(stack, "site_a")
    assert _viewer((await _queue(stack, subject, "site_admin"))[ids["evaluated"]])[
        "outcome_evidence"]["executions"] == 11
    async with stack.sessionmaker() as session:
        (grant,) = (await session.execute(select(CCScopeGrant).where(
            CCScopeGrant.tenant_id == stack.tenant,
            CCScopeGrant.principal_ref == subject))).scalars().all()
        grant_id = grant.id
    await stack.lapse(grant_id, how=how)
    assert await _queue(stack, subject, "site_admin") == {}
    view = await E.evidence_view(stack, subject)
    assert view.reach_empty and view.viewer("SEL_CLEAR")["outcome_evidence"] is None
    assert await E.stored_bytes(stack) == before


async def test_the_subset_that_withholds_fleet_view_reads_no_track_record():
    """JSONB `permission_subset`: an approver whose grant withholds
    fleet.view reads the proposal and no track record at all."""
    stack, ids = await E.scenario(**_kw())
    subject = await E.persona(stack, "approve_only_a")
    item = (await _queue(stack, subject, "site_admin"))[ids["evaluated"]]
    assert item["viewer_projected_evidence"]["outcome_evidence"] is None
    assert item["viewer_projected_evidence"]["unavailable_reason"] == \
        "no_fleet_view_reach"
    assert json.dumps(item).count("918273645") == 0
