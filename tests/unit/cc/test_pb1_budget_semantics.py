"""PB-1 (A30.42 D-PB2/D-PB3/D-PB4/D-PB11, boundary A30.44): budget semantics.

Two budgets, kept apart, each enforced at one point:

* the ADMISSION budget (`max_proposals_per_day`) -- checked and charged by
  `admit_proposal` under the tenant admission lock, for the evaluator and
  the ingress alike; `blocked` rows charge nothing; nothing releases a
  charge;
* the UNATTENDED execution budget (`execution_budget`) -- counts only
  `autonomous_grant` dispatches the Site Manager accepted or a claim has
  reserved; a human's decision never spends it or waits on it.

A transient denial (tenant stop switch, Site Manager halt) writes no row,
so nothing is charged and nothing is fenced; the condition is proposed
again once the stop lifts.

These drive the REAL dispatch and admission paths against a REAL Site
Manager (PB-0's `Estate`). True concurrency -- two admitters racing for
the last slot, two replicas reserving the last unattended unit -- is proven
on real PostgreSQL in `tests/integration/test_pb1_budget_semantics_pg.py`.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, update

from harkeniq_cc import agent_runtime, proposal_admission
from harkeniq_cc.db.models import (
    CCAgentProposal,
    CCAuditLog,
    CCFleetCache,
    CCIncident,
    CCOperationalAgent,
    CCOutcomeHistory,
    CCSafetyState,
    CCScopeGrant,
)
from harkeniq_cc.db.repos import AgentProposalRepo
from harkeniq_cc.operational_agent import (
    TRANSIENT_DENIAL_REASONS,
    transient_denial_code,
)
from harkeniq_cc.proposal_admission import (
    CODE_PROPOSAL_BUDGET_EXHAUSTED,
    admission_day_start,
    admit_proposal,
    admitted_today,
)
from harkeniq_sm.db.repos import DeviceRepo

from tests.unit.cc.test_pb0_dispatch_idempotency import (
    TENANT,
    Estate,
)


@pytest.fixture
async def estate():
    e = Estate()
    try:
        yield await e.start()
    finally:
        await e.stop()


@pytest.fixture
async def autonomous_estate():
    e = Estate()
    try:
        yield await e.start(autonomous=True)
    finally:
        await e.stop()


# -- helpers -----------------------------------------------------------------


async def _set_agent(estate, **values):
    """Configuration written straight to the row -- no version bump, so the
    activation stays the one the fixture approved."""
    async with estate.cc() as session:
        await session.execute(
            update(CCOperationalAgent)
            .where(CCOperationalAgent.id == estate.agent_id).values(**values)
        )
        await session.commit()


async def _add_device(estate, agent_id: str, incident_id: str) -> None:
    """Another device with an open SEL incident, known to BOTH services."""
    async with estate.sm() as session:
        await DeviceRepo(session).upsert_registration(
            site_id=estate.sm_site_id, agent_id=agent_id, vendor="dell",
        )
        await session.commit()
    async with estate.cc() as session:
        session.add(CCFleetCache(
            site_id=estate.site_id, agent_id=agent_id, agent_name=agent_id,
            vendor="Dell", model="R750", device_class="server",
            observation="observed", health="Critical",
        ))
        session.add(CCIncident(
            incident_id=incident_id, tenant_id=TENANT, site_id=estate.site_id,
            kind="device", status="open", title="BMC event log saturated",
            device_agent_id=agent_id, subsystem="log", confidence=0.9,
        ))
        await session.commit()


def _payload(estate, *, key: str, status: str = "awaiting_approval",
             device: str = "node-1", basis: str = "human_approval") -> dict:
    """A `govern_proposal`-shaped payload, for driving admission directly."""
    return {
        "tenant_id": TENANT, "agent_id": estate.agent_id,
        "actor": f"op-agent:{estate.agent_id}@v1", "agent_version": 1,
        "site_id": estate.site_id, "device_agent_id": device,
        "action_type": "SEL_CLEAR", "params": {"reason": key},
        "rationale": "r", "evidence": {}, "disposition": "requires_approval",
        "disposition_reason": "", "blocking_conditions": [],
        "authorization_basis": basis, "status": status, "decided_by": "",
        "decided_at": None, "dedupe_key": f"{estate.agent_id}:{device}:{key}",
    }


async def _admit(estate, payload) -> tuple:
    async with estate.cc() as session:
        row, code, reason = await admit_proposal(
            session, tenant_id=TENANT, payload=payload, origin="evaluator",
        )
        await session.commit()
        return (row.id if row else None), code, reason


async def _used(estate) -> int:
    async with estate.cc() as session:
        return await admitted_today(session, TENANT, estate.agent_id)


async def _unattended(estate) -> int:
    from harkeniq_cc.agent_lifecycle import executions_used

    async with estate.cc() as session:
        agent = await session.get(CCOperationalAgent, estate.agent_id)
        return await executions_used(session, TENANT, agent)


async def _rows(estate) -> list:
    async with estate.cc() as session:
        return list((await session.execute(
            select(CCAgentProposal).order_by(CCAgentProposal.created_at)
        )).scalars().all())


async def _set_safety(estate, **values):
    async with estate.cc() as session:
        await session.execute(
            update(CCSafetyState).where(CCSafetyState.site_id == estate.site_id)
            .values(as_of=datetime.now(timezone.utc),
                    ingested_at=datetime.now(timezone.utc), **values)
        )
        await session.commit()


async def _dry_run(estate) -> dict:
    async with estate.client() as c:
        res = await c.get(f"/api/operational-agents/{estate.agent_id}/dry-run")
        assert res.status_code == 200, res.text
        return res.json()


# ---------------------------------------------------------------------------
# 1. The admission budget: 0, 1, N; checked and charged at admission only
# ---------------------------------------------------------------------------


class TestAdmissionBudget:
    async def test_zero_admits_nothing_charged(self, estate):
        await _set_agent(estate, max_proposals_per_day=0)
        assert await agent_runtime.evaluate_agents(estate.state, TENANT) == []
        pid, code, _ = await _admit(estate, _payload(estate, key="a"))
        assert pid is None and code == CODE_PROPOSAL_BUDGET_EXHAUSTED
        assert await _rows(estate) == []

    async def test_one_admits_one_then_refuses(self, estate):
        await _set_agent(estate, max_proposals_per_day=1)
        first = await _admit(estate, _payload(estate, key="a"))
        assert first[0] and first[1] == ""
        second = await _admit(estate, _payload(estate, key="b", device="node-x"))
        assert second == (
            None, CODE_PROPOSAL_BUDGET_EXHAUSTED,
            proposal_admission.REASON_PROPOSAL_BUDGET_EXHAUSTED,
        )
        assert await _used(estate) == 1
        assert len(await _rows(estate)) == 1

    async def test_n_admits_exactly_n(self, estate):
        await _set_agent(estate, max_proposals_per_day=4)
        results = [
            await _admit(estate, _payload(estate, key=f"k{i}", device=f"d{i}"))
            for i in range(7)
        ]
        assert [bool(r[0]) for r in results] == [True] * 4 + [False] * 3
        assert {r[1] for r in results[4:]} == {CODE_PROPOSAL_BUDGET_EXHAUSTED}
        assert await _used(estate) == 4

    async def test_a_blocked_row_charges_nothing_and_is_admitted_when_spent(
        self, estate,
    ):
        """A `blocked` row records a final denial; it puts nothing in front of
        governance, so it neither counts nor is refused by the budget."""
        await _set_agent(estate, max_proposals_per_day=1)
        assert (await _admit(estate, _payload(estate, key="a")))[0]
        blocked = await _admit(
            estate, _payload(estate, key="b", device="d2", status="blocked"),
        )
        assert blocked[0] and blocked[1] == ""
        assert await _used(estate) == 1

    async def test_the_count_spans_versions(self, estate):
        """An edit must not refill the day's allowance."""
        await _set_agent(estate, max_proposals_per_day=2)
        await _admit(estate, _payload(estate, key="a"))
        p = _payload(estate, key="b", device="d2")
        p.update(actor=f"op-agent:{estate.agent_id}@v2", agent_version=2)
        await _admit(estate, p)
        refused = await _admit(estate, _payload(estate, key="c", device="d3"))
        assert refused[1] == CODE_PROPOSAL_BUDGET_EXHAUSTED

    async def test_only_the_current_utc_day_counts(self, estate):
        await _set_agent(estate, max_proposals_per_day=1)
        pid, _, _ = await _admit(estate, _payload(estate, key="a"))
        async with estate.cc() as session:
            await session.execute(
                update(CCAgentProposal).where(CCAgentProposal.id == pid)
                .values(created_at=admission_day_start() - timedelta(seconds=1))
            )
            await session.commit()
        assert await _used(estate) == 0
        assert (await _admit(estate, _payload(estate, key="b", device="d2")))[0]

    async def test_day_start_is_utc_midnight(self):
        ist = timezone(timedelta(hours=5, minutes=30))
        at = datetime(2026, 10, 11, 2, 0, tzinfo=ist)  # 20:30 UTC the day before
        assert admission_day_start(at) == datetime(
            2026, 10, 10, tzinfo=timezone.utc,
        )

    async def test_no_transition_releases_a_charge(self, estate):
        """D-PB14: approval, denial, dispatch, failure -- the count stays."""
        await _set_agent(estate, max_proposals_per_day=5)
        ids = [
            (await _admit(estate, _payload(estate, key=f"k{i}", device=f"d{i}")))[0]
            for i in range(4)
        ]
        async with estate.cc() as session:
            for pid, status in zip(ids, ("approved", "denied", "dispatched", "failed")):
                await session.execute(
                    update(CCAgentProposal).where(CCAgentProposal.id == pid)
                    .values(status=status)
                )
            await session.commit()
        assert await _used(estate) == 4

    async def test_a_logical_duplicate_is_refused_without_a_charge(self, estate):
        await _set_agent(estate, max_proposals_per_day=5)
        assert (await _admit(estate, _payload(estate, key="a")))[0]
        dup = await _admit(estate, _payload(estate, key="a"))
        assert dup[0] is None and dup[1] == "duplicate"
        assert await _used(estate) == 1

    async def test_the_evaluator_stops_at_the_budget(self, estate):
        await _add_device(estate, "node-2", "inc-2")
        await _add_device(estate, "node-3", "inc-3")
        await _set_agent(estate, max_proposals_per_day=2)
        created = await agent_runtime.evaluate_agents(estate.state, TENANT)
        assert len(created) == 2
        assert await agent_runtime.evaluate_agents(estate.state, TENANT) == []
        assert await _used(estate) == 2

    async def test_admission_is_the_authority_not_the_soft_cut(
        self, estate, monkeypatch,
    ):
        """Even if a caller's soft cut believed there was room, admission
        refuses: the budget has exactly one authority."""
        await _add_device(estate, "node-2", "inc-2")
        await _set_agent(estate, max_proposals_per_day=1)

        async def _always_room(*a, **kw):
            return 0

        # The evaluator's cut is fooled; admission's own count is not.
        monkeypatch.setattr(agent_runtime, "admitted_today", _always_room)
        created = await agent_runtime.evaluate_agents(estate.state, TENANT)
        assert len(created) == 1
        assert await _used(estate) == 1


# ---------------------------------------------------------------------------
# 2. Ingress shares the admission budget
# ---------------------------------------------------------------------------


class TestIngressSharesTheBudget:
    async def test_a_spent_budget_refuses_an_ingress_submission(
        self, monkeypatch,
    ):
        from harkeniq_cc.api import operational_agents as oa_api

        from tests.unit.cc.test_a6_external_ingress import (
            _body,
            _first_ref,
            _ready,
            _stack,
        )

        stack = await _stack()
        agent_id, _site = await _ready(stack)
        ref = await _first_ref(stack, agent_id)
        async with stack.sessionmaker() as session:
            await session.execute(
                update(CCOperationalAgent).where(CCOperationalAgent.id == agent_id)
                .values(max_proposals_per_day=1)
            )
            # The day's one slot, spent on other work.
            session.add(CCAgentProposal(
                tenant_id=TENANT, agent_id=agent_id, actor=f"op-agent:{agent_id}@v1",
                agent_version=1, site_id=_site, device_agent_id="elsewhere",
                action_type="SEL_CLEAR", params={}, rationale="r", evidence={},
                disposition="requires_approval",
                authorization_basis="human_approval", status="awaiting_approval",
                dedupe_key=f"{agent_id}:elsewhere:spent",
            ))
            await session.commit()

        # Fool the re-derivation's soft cut, so the refusal that answers is
        # admission's -- the authority for both callers.
        async def _always_room(*a, **kw):
            return 0

        monkeypatch.setattr(oa_api, "admitted_today", _always_room)
        async with stack.as_machine(agent_id).client() as c:
            res = await c.post(
                f"/api/operational-agents/{agent_id}/proposals", json=_body(ref),
            )
        assert res.status_code == 409, res.text
        assert res.json()["code"] == CODE_PROPOSAL_BUDGET_EXHAUSTED
        async with stack.sessionmaker() as session:
            assert (await session.execute(
                select(func.count(CCAgentProposal.id)))).scalar() == 1

    async def test_evaluator_then_ingress_share_one_count(self):
        from tests.unit.cc.test_a6_external_ingress import (
            _body,
            _first_ref,
            _ready,
            _stack,
        )

        stack = await _stack()
        agent_id, _site = await _ready(stack)
        ref = await _first_ref(stack, agent_id)
        async with stack.sessionmaker() as session:
            await session.execute(
                update(CCOperationalAgent).where(CCOperationalAgent.id == agent_id)
                .values(max_proposals_per_day=1)
            )
            await session.commit()
        # The evaluator spends the one slot ...
        assert len(await agent_runtime.evaluate_agents(stack.state, TENANT)) == 1
        # ... and the ingress, submitting the SAME candidate, is refused --
        # as a duplicate, never as a second charge.
        async with stack.as_machine(agent_id).client() as c:
            res = await c.post(
                f"/api/operational-agents/{agent_id}/proposals", json=_body(ref),
            )
        assert res.status_code == 409
        async with stack.sessionmaker() as session:
            assert await admitted_today(session, TENANT, agent_id) == 1


# ---------------------------------------------------------------------------
# 3. Transient denials write nothing, charge nothing, fence nothing
# ---------------------------------------------------------------------------


class TestTransientDenial:
    async def test_the_tenant_stop_switch_creates_no_row(self, estate):
        async with estate.client() as c:
            assert (await c.post("/api/policies/stop-switch")).status_code == 200
        assert await agent_runtime.evaluate_agents(estate.state, TENANT) == []
        assert await _rows(estate) == []
        async with estate.cc() as session:
            assert await AgentProposalRepo(session).all_dedupe_keys(TENANT) == set()
        dry = await _dry_run(estate)
        assert [w["code"] for w in dry["withheld"]] == ["stop_switch_active"]
        assert dry["would_propose"] == []

    async def test_lifting_the_stop_switch_proposes_again(self, estate):
        await _set_agent(estate, max_proposals_per_day=1)
        async with estate.client() as c:
            await c.post("/api/policies/stop-switch")
            await agent_runtime.evaluate_agents(estate.state, TENANT)
            assert await _used(estate) == 0, "a transient stop spent a slot"
            assert (await c.post(
                "/api/policies/stop-switch/deactivate")).status_code == 200
        created = await agent_runtime.evaluate_agents(estate.state, TENANT)
        assert len(created) == 1
        assert created[0].status == "awaiting_approval"

    async def test_a_site_manager_halt_creates_no_row(self, estate):
        await _set_safety(estate, sm_stop_switch=True)
        assert await agent_runtime.evaluate_agents(estate.state, TENANT) == []
        assert await _rows(estate) == []
        dry = await _dry_run(estate)
        assert [w["code"] for w in dry["withheld"]] == ["site_stop_switch_active"]
        assert estate.site_id not in dry["withheld"][0]["reason"]
        await _set_safety(estate, sm_stop_switch=False)
        assert len(await agent_runtime.evaluate_agents(estate.state, TENANT)) == 1

    async def test_no_proposal_created_audit_entry_while_stopped(self, estate):
        async with estate.client() as c:
            await c.post("/api/policies/stop-switch")
        await agent_runtime.evaluate_agents(estate.state, TENANT)
        async with estate.cc() as session:
            assert (await session.execute(
                select(func.count(CCAuditLog.id))
                .where(CCAuditLog.action == "agent_proposal.created")
            )).scalar() == 0

    def test_a_fenced_class_is_never_transient(self):
        """A permanent denial stays a `blocked` row (D16), whatever stop is
        also in force."""
        stop = [{"code": "stop_switch_active"}]
        assert transient_denial_code("denied", {"never_budget_grantable": True},
                                     stop) == ""
        assert transient_denial_code("denied", {}, stop) == "stop_switch_active"
        assert transient_denial_code(
            "denied", {}, [{"code": "site_stop_switch_active"}],
        ) == "site_stop_switch_active"
        # Not a denial, or a denial with no stop: never transient.
        assert transient_denial_code("requires_approval", {}, stop) == ""
        assert transient_denial_code("denied", {}, [{"code": "other"}]) == ""

    def test_the_reasons_name_no_site(self):
        for reason in TRANSIENT_DENIAL_REASONS.values():
            assert "site_id" not in reason and "{" not in reason


# ---------------------------------------------------------------------------
# 4. The unattended budget counts unattended dispatches only
# ---------------------------------------------------------------------------


class TestUnattendedBudget:
    async def test_human_approved_work_neither_spends_nor_waits(self, estate):
        await _set_agent(estate, execution_budget=1)
        pid = (await agent_runtime.evaluate_agents(estate.state, TENANT))[0].id
        async with estate.client() as c:
            res = await c.post(f"/api/approvals/{pid}/approve")
        assert res.json()["delivery"]["accepted"] is True, res.text
        assert await _unattended(estate) == 0
        # Even with a "spent" budget a human's decision dispatches.
        await _set_agent(estate, execution_budget=1)
        async with estate.cc() as session:
            session.add(CCOutcomeHistory(
                site_id=estate.site_id, action_id="x", action_type="SEL_CLEAR",
                device_agent_id="node-1", vendor="Dell", model="R750",
                outcome="SUCCESS", actor=f"op-agent:{estate.agent_id}@v1",
            ))
            await session.commit()
        assert await _unattended(estate) == 0, "an outcome is not a dispatch"

    async def test_autonomous_work_spends_and_is_withheld_when_spent(
        self, autonomous_estate,
    ):
        e = autonomous_estate
        await _add_device(e, "node-2", "inc-2")
        await _set_agent(e, execution_budget=1)
        created = await agent_runtime.evaluate_agents(e.state, TENANT)
        assert [p.authorization_basis for p in created] == ["autonomous_grant"] * 2
        dispatched = await agent_runtime.dispatch_decided(e.state, TENANT)
        assert len(dispatched) == 1
        assert await _unattended(e) == 1
        rows = {r.id: r for r in await _rows(e)}
        withheld = [r for r in rows.values() if r.status == "awaiting_approval"]
        assert len(withheld) == 1
        assert withheld[0].authorization_basis == "human_approval"
        assert withheld[0].dispatch_claimed_at is None
        assert await e.all_directives() == 1

    async def test_a_site_manager_refusal_releases_the_reservation(
        self, autonomous_estate, monkeypatch,
    ):
        e = autonomous_estate
        await _set_agent(e, execution_budget=1)
        await agent_runtime.evaluate_agents(e.state, TENANT)

        class Refuses(agent_runtime.SMClient):
            async def dispatch_action(self, *a, **kw):
                return {"accepted": False, "directive_id": "", "reason": "no"}

        monkeypatch.setattr(agent_runtime, "SMClient", Refuses)
        assert await agent_runtime.dispatch_decided(e.state, TENANT) == []
        assert (await e.only_proposal()).status == "failed"
        assert await _unattended(e) == 0

    async def test_a_transport_error_releases_the_reservation(
        self, autonomous_estate, monkeypatch,
    ):
        e = autonomous_estate
        await _set_agent(e, execution_budget=1)
        await agent_runtime.evaluate_agents(e.state, TENANT)

        class Down(agent_runtime.SMClient):
            async def dispatch_action(self, *a, **kw):
                raise ConnectionError("site unreachable")

        monkeypatch.setattr(agent_runtime, "SMClient", Down)
        assert await agent_runtime.dispatch_decided(e.state, TENANT) == []
        row = await e.only_proposal()
        assert row.status == "approved" and row.dispatch_claimed_at is None
        assert await _unattended(e) == 0

    async def test_a_held_claim_is_a_reservation(self, autonomous_estate):
        e = autonomous_estate
        await agent_runtime.evaluate_agents(e.state, TENANT)
        row = await e.only_proposal()
        async with e.cc() as session:
            await session.execute(
                update(CCAgentProposal).where(CCAgentProposal.id == row.id)
                .values(dispatch_claimed_at=datetime.now(timezone.utc))
            )
            await session.commit()
        assert await _unattended(e) == 1

    async def test_a_node_refusal_after_acceptance_keeps_the_charge(
        self, autonomous_estate,
    ):
        e = autonomous_estate
        await agent_runtime.evaluate_agents(e.state, TENANT)
        assert len(await agent_runtime.dispatch_decided(e.state, TENANT)) == 1
        row = await e.only_proposal()
        async with e.cc() as session:
            await AgentProposalRepo(session).settle(
                await AgentProposalRepo(session).get(TENANT, row.id), "FAILURE",
            )
            await session.commit()
        assert (await e.only_proposal()).status == "failed"
        assert await _unattended(e) == 1

    async def test_revoked_reach_before_dispatch_charges_nothing(
        self, autonomous_estate,
    ):
        e = autonomous_estate
        await agent_runtime.evaluate_agents(e.state, TENANT)
        async with e.cc() as session:
            await session.execute(
                update(CCScopeGrant).where(
                    CCScopeGrant.principal_type == "agent",
                    CCScopeGrant.principal_ref == e.agent_id,
                ).values(revoked_at=datetime.now(timezone.utc))
            )
            await session.commit()
        assert await agent_runtime.dispatch_decided(e.state, TENANT) == []
        row = await e.only_proposal()
        assert row.status == "approved" and row.dispatch_claimed_at is None
        assert await _unattended(e) == 0
        assert await e.all_directives() == 0

    async def test_expired_reach_before_dispatch_charges_nothing(
        self, autonomous_estate,
    ):
        e = autonomous_estate
        await agent_runtime.evaluate_agents(e.state, TENANT)
        async with e.cc() as session:
            await session.execute(
                update(CCScopeGrant).where(
                    CCScopeGrant.principal_type == "agent",
                    CCScopeGrant.principal_ref == e.agent_id,
                ).values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
            )
            await session.commit()
        assert await agent_runtime.dispatch_decided(e.state, TENANT) == []
        assert await _unattended(e) == 0

    async def test_never_into_an_exhausted_site_window(self, autonomous_estate):
        """D-PB12, pinned: until the Site Manager slice lands, Central
        Command never dispatches autonomous work into a site that reports
        its window spent."""
        e = autonomous_estate
        await agent_runtime.evaluate_agents(e.state, TENANT)
        await _set_safety(e, site_budgets={"SEL_CLEAR": 0})
        assert await agent_runtime.dispatch_decided(e.state, TENANT) == []
        assert await e.all_directives() == 0
        assert await _unattended(e) == 0

    async def test_an_old_dispatch_leaves_the_window(self, autonomous_estate):
        e = autonomous_estate
        await agent_runtime.evaluate_agents(e.state, TENANT)
        await agent_runtime.dispatch_decided(e.state, TENANT)
        assert await _unattended(e) == 1
        async with e.cc() as session:
            await session.execute(
                update(CCAgentProposal).values(
                    dispatched_at=datetime.now(timezone.utc) - timedelta(days=2))
            )
            await session.commit()
        assert await _unattended(e) == 0


# ---------------------------------------------------------------------------
# 5. What the surfaces report
# ---------------------------------------------------------------------------


class TestReporting:
    async def test_runtime_reports_the_proposal_day_and_unattended_count(
        self, estate,
    ):
        await _set_agent(estate, max_proposals_per_day=5)
        await _admit(estate, _payload(estate, key="a"))
        await _admit(estate, _payload(estate, key="b", device="d2", status="blocked"))
        async with estate.client() as c:
            r = (await c.get(
                f"/api/operational-agents/{estate.agent_id}/runtime")).json()
        assert r["proposals_in_window"] == 1, "blocked rows are not charged"
        assert r["budget"]["counts"] == "unattended_dispatches"
        assert r["budget"]["executions_used"] == 0

    async def test_actions_used_is_not_published_as_a_number(self, estate):
        async with estate.client() as c:
            posture = (await c.get("/api/autonomy/")).json()["posture"]
            budgets = (await c.get("/api/policies/autonomy")).json()
        assert posture["actions_used"] is None
        assert posture["actions_used_reason"] == "not_tracked"
        rows = budgets if isinstance(budgets, list) else budgets.get("budgets", [])
        assert rows and all(b["actions_used"] is None for b in rows)

    def test_discovery_version_is_bumped(self):
        from harkeniq_cc.discovery import DISCOVERY_VERSION

        assert DISCOVERY_VERSION == "2"


# ---------------------------------------------------------------------------
# 6. Structure: one authority each, never an authorization input
# ---------------------------------------------------------------------------


class TestStructure:
    def test_budget_is_never_authority(self):
        """A budget may refuse, withhold, or return work to a human. It is
        never an input to disposition, governance or the dispatch gates."""
        from harkeniq_cc import agent_activation, operational_agent

        for fn in (
            agent_activation.dispatch_permitted,
            operational_agent.effective_disposition,
            operational_agent.govern_proposal,
            agent_runtime.revalidate_dispatch,
        ):
            src = inspect.getsource(fn)
            for name in ("max_proposals_per_day", "execution_budget",
                         "admitted_today", "executions_used",
                         "count_unattended", "count_admitted_since"):
                assert name not in src, (fn.__name__, name)

    def test_the_dispatch_gate_is_named_for_what_it_asks(self):
        from harkeniq_cc.agent_activation import DISPATCH_GATES

        assert "budget" not in DISPATCH_GATES
        assert "agent_pause" in DISPATCH_GATES

    def test_exactly_one_admission_refusal_site(self):
        """Only `admit_proposal` refuses on the admission budget. The one
        other reader of the code is the evaluator's dry-run report of what
        its own cut withheld (PB-2, D-PB9) -- a report, never a refusal."""
        import pathlib

        from harkeniq_cc import operational_agent

        root = pathlib.Path(proposal_admission.__file__).parent
        hits = sorted(
            p.relative_to(root).as_posix()
            for p in root.rglob("*.py")
            if "CODE_PROPOSAL_BUDGET_EXHAUSTED" in p.read_text()
        )
        assert hits == ["operational_agent.py", "proposal_admission.py"], hits
        assert "CODE_PROPOSAL_BUDGET_EXHAUSTED" in inspect.getsource(
            operational_agent.evaluate)
        assert "CODE_PROPOSAL_BUDGET_EXHAUSTED" not in inspect.getsource(
            operational_agent.govern_proposal)
        assert "CODE_PROPOSAL_BUDGET_EXHAUSTED" in inspect.getsource(
            proposal_admission.admit_proposal)

    def test_the_unattended_count_is_read_under_its_lock(self):
        src = inspect.getsource(agent_runtime._unattended_allowed)
        assert src.index("lock_unattended_budget(") < src.index("executions_used(")
        dispatch = inspect.getsource(agent_runtime._dispatch_one)
        assert dispatch.index("_unattended_allowed(") < dispatch.index(
            "claim_dispatch(")

    def test_blocked_is_written_only_at_creation(self):
        """A charge is fixed at insert because nothing turns a row `blocked`
        later -- the only `PROPOSAL_BLOCKED` assignment is a creation status."""
        import pathlib

        root = pathlib.Path(proposal_admission.__file__).parent
        writers = []
        for p in root.rglob("*.py"):
            text = p.read_text()
            for needle in ('.status = "blocked"', ".status = PROPOSAL_BLOCKED",
                           'status="blocked"'):
                if needle in text:
                    writers.append((p.name, needle))
        assert writers == [], writers

    def test_the_removed_counts_are_gone(self):
        from harkeniq_cc.db import repos

        assert not hasattr(repos.AgentProposalRepo, "count_since")
        assert not hasattr(repos.AgentProposalRepo, "count_in_flight")
        assert not hasattr(repos.AgentPreflightRepo, "count_executions")

    def test_permissions_and_ceiling_are_untouched(self):
        from harkeniq_cc.machine_identity import MACHINE_PRINCIPAL_CEILING

        assert set(MACHINE_PRINCIPAL_CEILING) == {
            "fleet.view", "incident.view", "proposal.submit",
        }
