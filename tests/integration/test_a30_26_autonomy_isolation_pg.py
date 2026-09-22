"""A6-4B0b-S3 (A30.26) on a REAL PostgreSQL: autonomy scope isolation.

The sqlite proof lives in tests/unit/cc/test_a30_26_autonomy_scope_isolation.py
and carries the invariant (deletion equivalence over all 16 site subsets,
the poisoned rows, 24 killed mutants). This file asks what sqlite cannot
answer honestly about the SAME estate and the SAME personas:

* `cc_safety_state.suppressions`, `.error_budgets` and `.site_budgets` are
  JSONB here, and so are a proposal's `blocking_conditions` and `evidence`
  -- the columns every fact in this slice is read out of. JSONB does not
  keep key order and does not keep Python's types, so "the row that names
  site C" has to survive a real round trip to be filtered at all;
* a grant's lifecycle is timestamptz, and `permission_subset` is JSONB: an
  expired grant and a subset-narrowed grant on the hidden site have to
  confer nothing under the engine production actually runs;
* the proposal under test is produced by the REAL evaluator
  (`agent_runtime.run_once`) and read back through the production routes,
  under the advisory lock `admit_proposal` takes on PostgreSQL and is a
  no-op on sqlite.

Every narrowing is paired with a CONTROL that reads the same fact.

Each run owns a tenant and a suffix for every id it seeds, because the
database is shared and migrated, not created. Nothing is cleaned up: the
audit chain is append-only.

Gated on ``HARKEN_TEST_CC_PG_DSN``.
"""

from __future__ import annotations

import os
import uuid

import pytest

from harkeniq_cc import agent_runtime
from harkeniq_cc.api import approvals as approvals_api
from harkeniq_cc.autonomy import AUTONOMOUS, REQUIRES_APPROVAL, WITHHELD_REASON
from harkeniq_cc.db.base import make_engine

from tests.unit.cc import s3_estate as E
from tests.unit.cc.s3_estate import ALL
from tests.unit.cc.test_a30_26_autonomy_scope_isolation import (
    _FakeSM, _agent, _sites_named, _statements, _stored, klass,
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


async def _estate(sites=ALL) -> E.Stack:
    tag = uuid.uuid4().hex[:8]
    return await E.build(
        sites, engine=make_engine(DSN), tenant=f"s3-{tag}", tag=tag,
    )


async def _read(stack, subject, path="/api/autonomy/", **params) -> dict:
    res = await stack.as_person(subject, "site_admin").get(path, **params)
    assert res.status_code == 200, (path, res.status_code, res.text[:300])
    return res.json()


def _remaining(stack, keys, action="SEL_CLEAR") -> dict:
    return {
        stack.site(k): E.SAFETY[k]["site_budgets"][action]
        for k in keys if action in E.SAFETY[k].get("site_budgets", {})
    }


@pytest.mark.parametrize("name", [
    "tenant", "org_ab", "site_a", "site_b", "site_d", "device_a",
    "class_server", "no_scope", "a_plus_narrow_c", "tenant_narrow",
    "a_plus_approve_c", "a_plus_device_c",
])
async def test_every_persona_reads_only_its_own_sites_on_postgres(name):
    stack = await _estate()
    subject, reach = await E.persona(stack, name)
    holds = ALL if reach is None else reach
    contract = await _read(stack, subject)
    row = klass(contract)

    assert E.leaks(contract, holds) == []
    budget = E.expected_error_budget(holds)
    if budget is not None:
        budget["sites_dropped_back"] = [
            stack.tagged(s) for s in budget["sites_dropped_back"]
        ]
    assert row["safety"]["error_budget"] == budget
    assert row["safety"]["site_budget_remaining"] == _remaining(stack, holds)
    assert [d["domain_id"] for d in row["safety"]["suppressed_domains"]] \
        == E.expected_domains(holds)
    assert row["evidence"]["executions"] == E.expected_executions(holds)
    assert contract["safety_state"]["sites_reporting"] \
        == sorted(stack.tagged(s) for s in E.expected_reporting(holds))
    assert [s["id"] for s in contract["scope"]["sites"]] \
        == sorted(stack.site(k) for k in holds)
    # A hidden site never decides the answer; a held one does.
    assert row["disposition"] == (REQUIRES_APPROVAL if "C" in holds else AUTONOMOUS)
    assert klass(contract, "BMC_RESET")["disposition"] \
        == (REQUIRES_APPROVAL if "C" in holds else AUTONOMOUS)


async def test_the_tenant_reader_is_the_control_on_postgres():
    """Site C IS there to be read: JSONB gave back the drop-back, the
    fault domain and the spent budget the narrowed readers must not get."""
    stack = await _estate()
    subject, _ = await E.persona(stack, "tenant")
    contract = await _read(stack, subject)
    row = klass(contract)
    assert row["safety"]["error_budget"]["total"] == 90_909
    assert row["safety"]["error_budget"]["sites_dropped_back"] == [stack.site("C")]
    assert "SECRET-C" in [d["domain_id"] for d in row["safety"]["suppressed_domains"]]
    assert row["safety"]["site_budget_remaining"][stack.site("C")] == 50_000
    assert contract["posture"]["stop_switch"]["sites_reporting_active"] == 1
    assert [s["statement"] for s in row["learning"]] == [
        "cohort-knowledge", "signal-A", "signal-B", "SECRET-SIGNAL-C",
    ]


@pytest.mark.parametrize("how", ["expired", "revoked"])
async def test_a_lapsed_timestamptz_grant_on_the_hidden_site_confers_nothing(how):
    stack = await _estate()
    subject = f"kc-s3-lapsed-{how}"
    await stack.grant(subject, "site", stack.site("A"))
    grant_c = await stack.grant(subject, "site", stack.site("C"))
    before = await _read(stack, subject)
    assert klass(before)["safety"]["error_budget"]["total"] == 90_009      # control
    await stack.lapse(grant_c, how=how)
    after = await _read(stack, subject)
    assert E.leaks(after, holds=("A",)) == []
    assert klass(after)["safety"]["error_budget"]["total"] == 9


async def test_the_probe_is_not_an_oracle_on_postgres():
    stack = await _estate()
    subject, _ = await E.persona(stack, "site_a")
    absent = await _read(stack, subject, site_id="s3-no-such-site")
    for key in ("B", "C", "D"):
        probe = await _read(stack, subject, site_id=stack.site(key))
        probe["scope"]["site_id"] = absent["scope"]["site_id"]
        assert E.normalised(probe) == E.normalised(absent), key
    owner = (await stack.as_person().get(
        "/api/autonomy/", site_id=stack.site("C"))).json()
    assert klass(owner)["safety"]["error_budget"]["total"] == 90_000       # control


async def test_a_stored_verdict_round_trips_jsonb_and_is_narrowed_where_read():
    """The REAL evaluator writes the proposal; every human projection and
    the machine's own receipts read it back out of JSONB."""
    stack = await _estate()
    agent_id = await _agent(stack, ("A",), name=f"S3 PG {stack.tag}", ingress=True)
    await E.seed_incident(stack, "A")

    # The machine reads and submits BEFORE the runtime pass, so the work is
    # still a candidate when it asks.
    base = f"/api/operational-agents/{agent_id}"
    async with stack.as_machine(agent_id).client() as client:
        preview = await client.get(f"{base}/dry-run")
        assert preview.status_code == 200, preview.text
        (candidate,) = preview.json()["would_propose"]
        submitted = await client.post(f"{base}/proposals", json={
            "candidate_ref": candidate["candidate_ref"],
            "idempotency_key": f"s3-pg-{stack.tag}-0001",
        })
        assert submitted.status_code == 201, submitted.text
        machine = {
            "dry-run": preview.json(),
            "submit": submitted.json(),
            "detail": (await client.get(base)).json(),
            "list": (await client.get(f"{base}/proposals")).json(),
            "receipt": (await client.get(
                f"{base}/proposals/{submitted.json()['proposal_id']}")).json(),
        }

    # CONTROL: what JSONB holds names every reporting site.
    (stored,) = await _stored(stack, agent_id)
    assert _sites_named(stored.blocking_conditions) == {
        stack.site("A"), stack.site("B"), stack.site("C"),
    }
    assert "SECRET-SIGNAL-C" in _statements(stored.evidence)

    for where, payload in machine.items():
        assert E.leaks(payload, holds=("A",)) == [], where
    assert _sites_named(machine["submit"]["proposal"]["blocking_conditions"]) \
        == {stack.site("A")}
    assert _sites_named(machine["receipt"]["proposal"]["blocking_conditions"]) \
        == {stack.site("A")}
    # The recorded reason is site C's drop-back row verbatim; it goes with it.
    assert "error budget" in stored.disposition_reason
    assert machine["submit"]["proposal"]["disposition_reason"] == WITHHELD_REASON
    assert machine["receipt"]["proposal"]["disposition_reason"] == WITHHELD_REASON

    site_a, _ = await E.persona(stack, "site_a")
    org_ab, _ = await E.persona(stack, "org_ab")
    for subject, names in ((site_a, {"A"}), (org_ab, {"A", "B"})):
        detail = await _read(stack, subject, base)
        listed = await _read(stack, subject, f"{base}/proposals")
        queue = await _read(stack, subject, "/api/approvals/")
        queued = next(i["proposal"] for i in queue["actions"]
                      if i.get("origin") == "agent" and i["id"] == stored.id)
        for proposal in (detail["proposals"][0], listed["proposals"][0], queued):
            assert _sites_named(proposal["blocking_conditions"]) \
                == {stack.site(k) for k in names}
            assert "SECRET" not in str(proposal["blocking_conditions"])
            assert "SECRET-SIGNAL-C" not in _statements(proposal["evidence"])
            assert proposal["disposition_reason"] == WITHHELD_REASON
        assert E.leaks(detail, holds=tuple(sorted(names))) == []
    owner = (await stack.as_person().get(f"{base}/proposals")).json()
    assert owner["proposals"][0]["disposition_reason"] == stored.disposition_reason  # control
