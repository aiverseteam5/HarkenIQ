"""PB-2 (A30.42 D-PB7/D-PB8/D-PB9, boundary A30.45): evaluator ordering.

The evaluator decides which in-reach device gets a scarce proposal slot.
Before PB-2 that choice read the tenant's oldest 50,000 outcome rows and a
tenant-wide cohort prior, and its incidents were the tenant's newest 1,000
-- so a site the agent cannot reach could reorder, or crowd out, the work it
can. The master oracle (A30.42 H1):

    an agent's decisions over the FULL estate == its decisions over an
    estate REDUCED to its own reach

compared over the admitted proposals, their order, their dedupe keys, the
frozen `evidence.attention`, the dry-run's order, its withheld list and the
budget cut -- for org-unit, site, device, device_class and mixed agents,
with a tenant-wide control that MUST differ. Excluded by ratified design:
S3-E2's creation record (whole-tenant outcome evidence) and the closed
global gate.

The estate is B2-2's poisoned one (`b2_2_estate`): hidden site C carries
twenty R750 failures, a moved device's six failures, incidents and pending
work. The reduction here is an INDEPENDENT restatement of the owner rule,
permission-neutral -- the evaluator reads an agent's WHERE-only reach, which
carries no permission (A22.13) -- and imports nothing from
`harkeniq_cc.scope`.
"""

from __future__ import annotations

import inspect
import json
from datetime import timedelta

import pytest
import sqlalchemy as sa

from harkeniq_cc import agent_runtime, operational_agent
from harkeniq_cc.db.models import (
    CCAgentCapability,
    CCAgentProposal,
    CCIncident,
    CCOperationalAgent,
    CCOutcomeHistory,
)
from harkeniq_cc.governance import load_agent_scope
from harkeniq_cc.proposal_admission import CODE_PROPOSAL_BUDGET_EXHAUSTED
from harkeniq_cc.scope import where_reach

from tests.unit.cc import b2_2_estate as E
from tests.unit.cc import s3_estate as S3

AGENT = "pb2-agent"
BOUND = ("COLLECT_DIAGNOSTICS", "IDENTIFY_LED", "SEL_CLEAR")
PER_DAY = 2

#: Operational personas: the agent's scope rows, refs as `b2_2_estate` spells
#: them ("@ab" a region, "dev:<key>" a device).
AGENTS: dict[str, list[tuple[str, str]]] = {
    "tenant": [("tenant", "")],
    "org-ab": [("org_unit", "@ab")],
    "site-a": [("site", "A")],
    "device-a1": [("device", "dev:a1")],
    "class-switch": [("device_class", "switch")],
    "mixed": [("site", "A"), ("device", "dev:b1"), ("device_class", "switch")],
}
SCOPED = tuple(n for n in AGENTS if n != "tenant")


# ---------------------------------------------------------------------------
# The independent, permission-neutral reduction
# ---------------------------------------------------------------------------


def _site_in(rules, site: str) -> bool:
    for kind, ref in rules:
        if kind == "tenant":
            return True
        if not site:
            continue
        if kind == "site" and ref == site:
            return True
        if kind == "org_unit" and E._REGION.get(site) == ref[1:]:
            return True
    return False


def _device_in(rules, key: str) -> bool:
    device = E.DEVICES[key]
    if _site_in(rules, device.site):
        return True
    return any(
        (kind == "device" and ref == f"dev:{key}")
        or (kind == "device_class" and ref == device.device_class)
        for kind, ref in rules
    )


def _row_in(rules, site: str, device_key: str) -> bool:
    """The owner rule: a row naming a device that RESOLVES at the row's own
    site is the device's; every other row is its site's."""
    if device_key in E.DEVICES and E.DEVICES[device_key].site == site:
        return _device_in(rules, device_key)
    return _site_in(rules, site)


def reach_keep(name: str) -> E.Keep:
    rules = AGENTS[name]
    devices = frozenset(k for k in E.DEVICES if _device_in(rules, k))
    outcomes = frozenset(
        i for i, (dev, site, *_r) in enumerate(E.OUTCOMES) if _row_in(rules, site, dev)
    )
    routes = frozenset(
        key for key, site, dev, *_r in E.ROUTES if _row_in(rules, site, dev)
    )
    incidents = frozenset(
        key for key, inc in E.INCIDENTS.items()
        if _row_in(rules, inc.site, inc.device)
    )
    held = {s for s in S3.ALL if _site_in(rules, s)}
    signals = frozenset(
        key for key, scope_type, site, *_r in E.SIGNALS
        if scope_type == "cohort" or site in held
    )
    patterns = frozenset(
        key for key, _t, named, *_r in E.PATTERNS if not named or set(named) & held
    )
    sites = (
        held | {E.DEVICES[k].site for k in devices}
        | {E.OUTCOMES[i][1] for i in outcomes}
        | {E.INCIDENTS[k].site for k in incidents if E.INCIDENTS[k].site}
    )
    return E.Keep(sites=frozenset(sites), devices=devices, outcomes=outcomes,
                  routes=routes, incidents=incidents, signals=signals,
                  patterns=patterns)


# ---------------------------------------------------------------------------
# Building an agent on an estate, and reading its decisions
# ---------------------------------------------------------------------------


def _aid(stack) -> str:
    """The proof agent's id on THIS stack (a PostgreSQL stack tags its ids)."""
    return stack.tagged(AGENT)[:32]


async def _agent(stack, name: str, *, per_day: int = PER_DAY) -> str:
    AGENT_ID = _aid(stack)
    async with stack.sessionmaker() as session:
        session.add(CCOperationalAgent(
            id=AGENT_ID, tenant_id=stack.tenant, name=AGENT_ID, status="active",
            version=1, activated_version=1, max_proposals_per_day=per_day,
            require_approval_always=True,
        ))
        for ref in ("attention", "autonomy"):
            session.add(CCAgentCapability(
                agent_id=AGENT_ID, tenant_id=stack.tenant, kind="read",
                capability_ref=ref,
            ))
        for action in BOUND:
            session.add(CCAgentCapability(
                agent_id=AGENT_ID, tenant_id=stack.tenant, kind="action_class",
                capability_ref=action,
            ))
        await session.commit()
    for kind, ref in AGENTS[name]:
        if ref.startswith("@"):
            ref = stack.regions[ref[1:]]
        elif ref.startswith("dev:"):
            ref = E.agent(stack, ref[4:])
        elif ref in S3.SITES:
            ref = stack.site(ref)
        await stack.grant(AGENT_ID, kind, ref, principal_type="agent", role="")
    return AGENT_ID


async def _estate(name: str, keep: E.Keep | None = None, **agent_kw):
    stack = await E.build(keep)
    await _agent(stack, name, **agent_kw)
    return stack


def _dry_view(body: dict, *, refs: bool = True) -> dict:
    """`refs=False` drops the candidate references, which digest the TENANT
    and so differ between two PostgreSQL stacks sharing one database."""
    return {
        "would_propose": [
            (p["candidate_ref"] if refs else "", p["device_agent_id"], p["site_id"],
             p["action_type"], json.dumps(p["params"], sort_keys=True),
             json.dumps((p.get("evidence") or {}).get("attention"), sort_keys=True))
            for p in body["would_propose"]
        ],
        "withheld": [
            (w["device_agent_id"], w["site_id"], w["action_type"], w["code"],
             w.get("candidate_ref") if refs else bool(w.get("candidate_ref")))
            for w in body["withheld"]
        ],
    }


async def _dry_run(stack) -> dict:
    stack.as_person()
    async with stack.client() as c:
        res = await c.get(f"/api/operational-agents/{_aid(stack)}/dry-run")
    assert res.status_code == 200, res.text
    return res.json()


async def _admitted(stack) -> list:
    await agent_runtime.evaluate_agents(stack.state, stack.tenant)
    async with stack.sessionmaker() as session:
        rows = (await session.execute(
            sa.select(CCAgentProposal).where(CCAgentProposal.agent_id == _aid(stack))
            .order_by(CCAgentProposal.created_at, CCAgentProposal.id)
        )).scalars().all()
    return [
        (r.dedupe_key, r.device_agent_id, r.site_id, r.action_type, r.status,
         json.dumps((r.evidence or {}).get("attention"), sort_keys=True))
        for r in rows
    ]


async def _decisions(stack, *, refs: bool = True) -> dict:
    view = _dry_view(await _dry_run(stack), refs=refs)
    view["admitted"] = await _admitted(stack)
    return view


async def _internal(stack) -> dict:
    async with stack.sessionmaker() as session:
        scope = await load_agent_scope(
            session, tenant_id=stack.tenant, agent_id=_aid(stack))
        return await agent_runtime.decision_attention(
            session, stack.tenant, where_reach(scope),
        )


# ---------------------------------------------------------------------------
# 1. The master oracle
# ---------------------------------------------------------------------------


class TestReducedEstateEquivalence:
    @pytest.mark.parametrize("name", SCOPED)
    async def test_full_estate_equals_the_estate_reduced_to_reach(self, name):
        full = await _decisions(await _estate(name))
        reduced = await _decisions(await _estate(name, reach_keep(name)))
        assert full == reduced, name
        # Non-trivial: every scoped persona here makes or withholds work.
        assert full["admitted"] or full["withheld"], name

    async def test_the_tenant_wide_control_differs(self):
        """Non-vacuity: the hidden rows ARE material to an agent that reaches
        them, so equality above is a property of the selection."""
        full = await _decisions(await _estate("tenant"))
        reduced = await _decisions(await _estate("tenant", reach_keep("site-a")))
        assert full != reduced

    async def test_the_budget_cut_is_part_of_the_equivalence(self):
        """The site-A agent has more admissible work than its budget: what is
        cut, and in what order, is decided by its own reach alone."""
        full = await _decisions(await _estate("site-a", per_day=1))
        reduced = await _decisions(
            await _estate("site-a", reach_keep("site-a"), per_day=1))
        assert full == reduced
        cut = [w for w in full["withheld"] if w[3] == CODE_PROPOSAL_BUDGET_EXHAUSTED]
        assert cut and all(w[4] for w in cut), full["withheld"]
        assert len(full["would_propose"]) == 1 and len(full["admitted"]) == 1


# ---------------------------------------------------------------------------
# 2. Hidden state: poison added AFTER a baseline moves nothing
# ---------------------------------------------------------------------------


async def _poison(stack, *, outcomes: int = 0, incidents: int = 0) -> None:
    """More of exactly what a site the agent cannot reach holds."""
    site_c, c1 = stack.site("C"), E.agent(stack, "c1")
    async with stack.sessionmaker() as session:
        if outcomes:
            await session.execute(sa.insert(CCOutcomeHistory), [
                {"id": stack.tagged(f"pb2-poison-o{n}")[:32], "site_id": site_c,
                 "action_id": stack.tagged(f"pb2-poison-{n}"), "action_type": "COLLECT_DIAGNOSTICS",
                 "device_agent_id": c1, "vendor": "Dell", "model": E.R750,
                 "outcome": "FAILURE", "recorded_at": E.T0 + timedelta(days=1),
                 "ingested_at": E.T0 + timedelta(days=1)}
                for n in range(outcomes)
            ])
        if incidents:
            await session.execute(sa.insert(CCIncident), [
                {"incident_id": stack.tagged(f"pb2-poison-i{n:05d}"),
                 "tenant_id": stack.tenant,
                 "site_id": site_c, "device_agent_id": c1, "kind": "device",
                 "subsystem": "psu", "status": "open", "title": "poison",
                 "confidence": 0.9, "opened_at": E.T0 + timedelta(days=2, seconds=n)}
                for n in range(incidents)
            ])
        await session.commit()


class TestHiddenStateDecidesNothing:
    async def test_poisoned_hidden_outcomes(self):
        stack = await _estate("site-a")
        before = _dry_view(await _dry_run(stack))
        internal_before = await _internal(stack)
        await _poison(stack, outcomes=500)
        assert _dry_view(await _dry_run(stack)) == before
        assert await _internal(stack) == internal_before

    async def test_hidden_incident_volume_beyond_a_thousand(self):
        """PB-F6: the tenant's newest 1,000 open incidents used to be the
        evaluator's whole view, so 1,001 newer ones at a hidden site pushed
        every in-reach condition out of evaluation."""
        stack = await _estate("site-a")
        before = _dry_view(await _dry_run(stack))
        assert before["would_propose"], "the baseline must propose something"
        await _poison(stack, incidents=1001)
        assert _dry_view(await _dry_run(stack)) == before

    async def test_a_moved_device_is_not_scored_on_history_left_behind(self):
        """`mv` moved from C to A; its six failures at C are C's rows."""
        stack = await _estate("site-a")
        item = (await _internal(stack))[(stack.site("A"), E.agent(stack, "mv"))]
        assert item["confidence"]["basis"] != "cohort_prior"
        assert item["band"] == "insufficient_data" or item["factors"].get(
            "sample_count", 1) <= 1

    async def test_no_cohort_prior_ever_reaches_the_evaluator(self):
        """D-PB7: the cohort prior is NONE, as for a machine -- even for a
        tenant-wide agent whose reach holds the whole cohort."""
        for name in ("tenant", "site-a"):
            stack = await _estate(name)
            bases = {i["confidence"]["basis"] for i in (await _internal(stack)).values()}
            assert "cohort_prior" not in bases, (name, bases)

    async def test_more_than_fifty_thousand_outcomes_are_all_read(self):
        """The oldest-50,000 window is gone: past it, the NEWEST row -- the
        one the window dropped -- still moves the evaluator's answer."""
        stack = await _estate("site-a")
        site_a, a1 = stack.site("A"), E.agent(stack, "a1")
        async with stack.sessionmaker() as session:
            await session.execute(sa.insert(CCOutcomeHistory), [
                {"id": f"pb2-old-{n}", "site_id": site_a, "action_id": f"pb2-old-{n}",
                 "action_type": "SEL_CLEAR", "device_agent_id": a1, "vendor": "Dell",
                 "model": E.R750, "outcome": "SUCCESS",
                 "recorded_at": E.T0 - timedelta(days=400),
                 "ingested_at": E.T0 - timedelta(days=400)}
                for n in range(50_000)
            ])
            await session.commit()
        before = (await _internal(stack))[(site_a, a1)]
        async with stack.sessionmaker() as session:
            session.add(CCOutcomeHistory(
                site_id=site_a, action_id="pb2-newest", action_type="SEL_CLEAR",
                device_agent_id=a1, vendor="Dell", model=E.R750, outcome="FAILURE",
                recorded_at=E.T0 + timedelta(days=30),
                ingested_at=E.T0 + timedelta(days=30),
            ))
            await session.commit()
        after = (await _internal(stack))[(site_a, a1)]
        assert after["risk_score"] != before["risk_score"]

    def test_the_fifty_thousand_read_is_deleted(self):
        from harkeniq_cc.db.repos import OutcomeHistoryRepo

        assert not hasattr(OutcomeHistoryRepo, "list_device_outcome_dicts")


# ---------------------------------------------------------------------------
# 3. Incidents: every in-reach one, in a total order
# ---------------------------------------------------------------------------


class TestIncidentInput:
    async def test_more_than_a_thousand_in_reach_incidents_are_all_read(self):
        stack = await _estate("site-a")
        site_a, a3 = stack.site("A"), E.agent(stack, "a3")
        async with stack.sessionmaker() as session:
            await session.execute(sa.insert(CCIncident), [
                {"incident_id": f"pb2-many-{n:05d}", "tenant_id": stack.tenant,
                 "site_id": site_a, "device_agent_id": a3, "kind": "device",
                 "subsystem": "fan", "status": "open", "title": "many",
                 "confidence": 0.9, "opened_at": E.T0 - timedelta(days=3, seconds=n)}
                for n in range(1001)
            ])
            await session.commit()
            scope = await load_agent_scope(
                session, tenant_id=stack.tenant, agent_id=_aid(stack))
            by_device = await agent_runtime._incidents_by_device(
                session, stack.tenant, where_reach(scope),
            )
        assert len(by_device[a3]) >= 1001 + 2
        assert E.agent(stack, "c1") not in by_device

    async def test_equal_timestamps_order_by_incident_id_and_rerun_identically(self):
        stack = await _estate("site-a")
        site_a, a4 = stack.site("A"), E.agent(stack, "a4")
        at = E.T0 + timedelta(days=5)
        async with stack.sessionmaker() as session:
            for iid in ("pb2-tie-c", "pb2-tie-a", "pb2-tie-b"):
                session.add(CCIncident(
                    incident_id=iid, tenant_id=stack.tenant, site_id=site_a,
                    device_agent_id=a4, kind="device", subsystem="psu",
                    status="open", title="tie", confidence=0.9, opened_at=at,
                ))
            await session.commit()
            scope = await load_agent_scope(
                session, tenant_id=stack.tenant, agent_id=_aid(stack))
            by_device = await agent_runtime._incidents_by_device(
                session, stack.tenant, where_reach(scope),
            )
        assert [i["incident_id"] for i in by_device[a4]] == [
            "pb2-tie-a", "pb2-tie-b", "pb2-tie-c"]
        first = _dry_view(await _dry_run(stack))
        for _ in range(20):
            assert _dry_view(await _dry_run(stack)) == first

    async def test_an_incident_at_another_site_is_not_this_rows_condition(self):
        """PB-F13: incidents name a device id; one recorded at another site
        is not a condition of this device's row here."""
        device = type("D", (), {"agent_id": "n1", "site_id": "S1",
                                "observation": "observed"})()
        incidents = {"n1": [
            {"incident_id": "i-here", "subsystem": "psu", "title": "here",
             "site_id": "S1"},
            {"incident_id": "i-there", "subsystem": "fan", "title": "there",
             "site_id": "S2"},
        ]}
        kept = [i for i in incidents["n1"]
                if not i.get("site_id") or i["site_id"] == device.site_id]
        assert [i["incident_id"] for i in kept] == ["i-here"]
        src = inspect.getsource(operational_agent.evaluate)
        assert 'i["site_id"] == device.site_id' in src

    async def test_the_reach_is_required(self):
        stack = await _estate("site-a")
        async with stack.sessionmaker() as session:
            with pytest.raises(TypeError, match="PB-F6"):
                await agent_runtime._incidents_by_device(session, stack.tenant, None)


# ---------------------------------------------------------------------------
# 4. The cut: reported by the dry-run, never a refusal at ingress
# ---------------------------------------------------------------------------


class TestTheCut:
    async def test_the_dry_run_reports_cut_candidates_bounded(self):
        stack = await _estate("site-a", per_day=1)
        body = await _dry_run(stack)
        cut = [w for w in body["withheld"] if w["code"] == CODE_PROPOSAL_BUDGET_EXHAUSTED]
        assert cut, body["withheld"]
        for entry in cut:
            assert entry["candidate_ref"].startswith("cand_")
            assert set(entry) == {
                "device_agent_id", "site_id", "action_type", "subsystem",
                "condition", "code", "reason", "candidate_ref",
            }
        assert len(body["would_propose"]) == 1

    async def test_a_machine_dry_run_carries_no_rank_band_or_score(self):
        stack = await _estate("site-a", per_day=1)
        stack.as_machine(_aid(stack))
        async with stack.client() as c:
            res = await c.get(f"/api/operational-agents/{_aid(stack)}/dry-run")
        assert res.status_code == 200, res.text
        body = res.json()
        text = json.dumps(body)
        for leak in ('"rank"', '"risk_score"', '"band"'):
            assert leak not in text, leak
        # S3-E2 D9: the attention block is withheld from every machine.
        for p in body["would_propose"]:
            assert p["evidence"]["attention"] is None
            assert "attention" in p["evidence"]["withheld"]
        stack.as_person()

    def test_the_ingress_rederivation_is_not_cut(self):
        from harkeniq_cc.api import operational_agents as api

        src = inspect.getsource(api.submit_proposal)
        assert "cut_at_budget=False" in src
        assert "cut_at_budget" not in inspect.getsource(agent_runtime.evaluate_agents)
        assert "cut_at_budget" not in inspect.getsource(api.dry_run_agent)

    def test_the_evaluator_cuts_and_the_ingress_does_not(self):
        """Pure: the same inputs, with and without the cut."""
        from types import SimpleNamespace as NS

        agent = NS(id="ag", tenant_id="t", name="ag", version=1,
                   max_proposals_per_day=1, require_approval_always=True,
                   autonomy_ceiling=0)
        devices = [NS(agent_id=f"n{i}", site_id="s1", device_class="server",
                      observation="observed", vendor="", model="", health="",
                      capabilities=None) for i in range(3)]
        incidents = {f"n{i}": [{"incident_id": f"i{i}", "subsystem": "psu",
                                "title": "p", "site_id": "s1"}] for i in range(3)}
        row = {"action_type": "COLLECT_DIAGNOSTICS", "disposition": "requires_approval",
               "blocking_conditions": [], "never_budget_grantable": False}

        class _A:
            stop_switch_active = False

            def local_rows(self, _site):
                return {"COLLECT_DIAGNOSTICS": row}

            def evidence_rows(self):
                return {}

            def gate_verdict(self, *_a):
                return NS(clear=True)

        kw = dict(
            agent=agent, scopes=[NS(scope_type="site", scope_ref="s1")],
            capabilities=[NS(kind="action_class", capability_ref="COLLECT_DIAGNOSTICS")],
            devices=devices, incidents_by_device=incidents, assessments=_A(),
            unattended_approved=frozenset(),
            catalogue={"psu": [{"action_type": "COLLECT_DIAGNOSTICS",
                                "because": "b", "provenance": "p"}]},
        )
        assert len(operational_agent.evaluate(**kw)) == 1
        assert len(operational_agent.evaluate(**kw, cut_at_budget=False)) == 3


# ---------------------------------------------------------------------------
# 5. Structure
# ---------------------------------------------------------------------------


class TestStructure:
    def test_one_decision_read_for_all_three_paths(self):
        from harkeniq_cc.api import operational_agents as api

        for fn in (agent_runtime.evaluate_agents, api.dry_run_agent,
                   api.submit_proposal):
            src = inspect.getsource(fn)
            assert "decision_attention(" in src, fn.__name__
            assert "load_attention(" not in src, fn.__name__
            assert "_incidents_by_device(" in src, fn.__name__

    def test_the_device_order_is_total(self):
        src = inspect.getsource(operational_agent.evaluate)
        assert 'getattr(dev, "site_id", "") or ""' in src

    def test_the_internal_path_takes_no_cohort(self):
        from harkeniq_cc import governance

        src = inspect.getsource(governance._compose_attention)
        assert "cohorts=False" in src
        assert "list_device_outcome_dicts" not in src
