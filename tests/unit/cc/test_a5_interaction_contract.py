"""A5: the canonical governed agent interaction contract (spec A22).

Three things are pinned here, and the negatives carry most of the weight.

  1. THE PARAMETER CONTRACT (A22.2/A22.3/A22.5). A4 made five classes
     addressable whose executors require a parameter the evaluator had
     never supplied -- every proposal carried ``params={"reason": ...}``
     and was refused at the node. The tests assert the resolution AND the
     refusals, because a contract that silently invents a value would
     pass the happy path and be worse than the defect.

  2. THE FIVE DEFECTS (A22.9-A22.13). Each was reachable only because A5
     introduces the richer interaction contract, so each is asserted at
     the layer that actually decides, never at a pure function alone.

  3. DRY-RUN (A22.7/A22.8). It writes nothing -- proven by table
     snapshot, not by reading the code -- and it reasons through the same
     `govern_proposal` the runtime uses, so a preview cannot disagree
     with what happens.
"""

from __future__ import annotations

import httpx
import pytest

from harkeniq.autonomy.preconditions import (
    ACTION_PARAMETERS,
    SRC_COMPONENT,
    SRC_UNAVAILABLE,
    validate_param_names,
)
from harkeniq.capabilities import (
    action_facts,
    parameter_contract,
    resolve_action_params,
    validate_action_params,
)
from harkeniq.models import ActionType
from harkeniq_cc import agent_runtime
from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.app import create_app
from harkeniq_cc.auth import ROLE_PERMISSIONS, UserContext, configure_auth
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.models import (
    CCAgentIdentity,
    CCAutonomyBudget,
    CCFleetCache,
    CCIncident,
    CCScopeGrant,
    CCSite,
)
from harkeniq_cc.db.repos import AgentProposalRepo
from harkeniq_cc.governance import load_agent_scope, load_attention
from harkeniq_cc.machine_identity import MACHINE_PRINCIPAL_CEILING
from harkeniq_cc.runtime import AppState
from harkeniq_cc.scope import SCOPE_ONLY_MARKER, ScopeError

from tests.unit.cc.conftest import seed_legacy, seed_tenant_admin

TENANT = "t1"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class FakeSM:
    dispatches: list = []

    def __init__(self, *_a, **_kw):
        pass

    async def dispatch_action(self, endpoint, token, **kw):
        FakeSM.dispatches.append(kw)
        return {"accepted": True, "directive_id": "d1", "reason": ""}

    async def route_approval(self, **kw):
        FakeSM.dispatches.append(kw)
        return {"accepted": True, "delivered": True, "reason": ""}


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    FakeSM.dispatches = []
    monkeypatch.setattr(agent_runtime, "SMClient", FakeSM)
    yield


class Stack:
    def __init__(self, app, state):
        self.app, self.state = app, state
        self.sessionmaker = state.sessionmaker
        self.persona = ("kc-owner", "owner@example.com", "tenant_owner")
        self.machine = None

    def as_person(self, sub, email, role="tenant_owner"):
        self.persona, self.machine = (sub, email, role), None
        return self

    def as_agent(self, agent_id: str, permissions=None):
        """An authenticated machine principal, as `auth.py` builds one."""
        self.machine = (agent_id, list(
            permissions if permissions is not None else MACHINE_PRINCIPAL_CEILING
        ))
        return self

    def client(self):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test",
        )


async def _stack() -> Stack:
    config = CCConfig(tenant_id=TENANT, insecure=True)
    configure_auth("", "", "", insecure=True)
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    state = AppState(
        config=config, engine=engine, sessionmaker=make_sessionmaker(engine),
    )
    app = create_app(state)
    stack = Stack(app, state)

    # A23-5: a rowless tenant is STRICT now (A23.11). The default
    # persona is the tenant's founding administrator, granted the
    # way tenant birth grants one (A23.14 D4) rather than being
    # tenant-wide by the synthesis a missing row used to give.
    await seed_tenant_admin(state.sessionmaker, TENANT, "kc-owner")

    async def _fake():
        if stack.machine is not None:
            agent_id, perms = stack.machine
            return UserContext(
                user_id=agent_id, email=f"op-agent:{agent_id}@v1",
                tenant_id=TENANT, role="", permissions=perms,
                species="agent", identity_id="id-1",
            )
        sub, email, role = stack.persona
        return UserContext(
            user_id=sub, email=email, tenant_id=TENANT, role=role,
            permissions=list(ROLE_PERMISSIONS[role]),
        )

    app.dependency_overrides[get_current_user] = _fake
    return stack


async def _seed(stack, *, level=2, sites=("DC-1",)) -> list[str]:
    """One or two sites, each with a declared device and a disk incident."""
    ids: list[str] = []
    async with stack.sessionmaker() as session:
        session.add(CCAutonomyBudget(
            tenant_id=TENANT, device_type="*", level=level,
            budget_limit=100, budget_period="daily",
        ))
        for n, name in enumerate(sites, start=1):
            site = CCSite(tenant_id=TENANT, site_name=name,
                          sm_endpoint="sm:50051", sm_token="tok")
            session.add(site)
            await session.flush()
            ids.append(site.id)
            session.add(CCFleetCache(
                site_id=site.id, agent_id=f"node-{n}", agent_name=f"n{n}",
                vendor="Dell", model="R750", device_class="server",
                observation="observed", health="Critical",
                capabilities={
                    "reach_known": True,
                    "implemented": ["SEL_CLEAR", "COLLECT_DIAGNOSTICS",
                                    "IDENTIFY_LED", "POWER_CAP_ADJUST"],
                    "allowed": ["SEL_CLEAR", "COLLECT_DIAGNOSTICS",
                                "IDENTIFY_LED"],
                    "effective": ["SEL_CLEAR", "COLLECT_DIAGNOSTICS",
                                  "IDENTIFY_LED"],
                },
            ))
            session.add(CCIncident(
                incident_id=f"inc-{n}", tenant_id=TENANT, site_id=site.id,
                kind="device", status="open", title=f"disk failing on n{n}",
                device_agent_id=f"node-{n}", subsystem="disk", confidence=0.9,
                components=[{"component": f"Disk.Bay.{n}",
                             "severity": "CRITICAL"}],
            ))
        await session.commit()
    return ids


async def _agent(client, site_id, **kw) -> str:
    body = {
        "name": "Night Shift",
        "scopes": [{"scope_type": "site", "scope_ref": site_id}],
        "capabilities": [{"kind": "action_class", "capability_ref": "IDENTIFY_LED"}],
    }
    body.update(kw)
    res = await client.post("/api/operational-agents/", json=body)
    assert res.status_code == 201, res.text
    return res.json()["id"]


async def _activate_row(stack, agent_id):
    from harkeniq_cc.db.models import CCOperationalAgent

    async with stack.sessionmaker() as session:
        agent = await session.get(CCOperationalAgent, agent_id)
        agent.status = "active"
        agent.activated_version = agent.version
        await session.commit()


# ---------------------------------------------------------------------------
# 1. The parameter contract (A22.2, A22.3, A22.5)
# ---------------------------------------------------------------------------


class TestTheParameterContract:
    def test_every_governed_class_declares_its_parameters(self):
        """A class with no entry is indistinguishable from one taking none."""
        missing = [a.value for a in ActionType if a not in ACTION_PARAMETERS]
        assert missing == []

    def test_the_declaration_matches_what_the_executor_actually_reads(self):
        """The contract is worthless if it disagrees with the executor.

        These four are the A4 defect itself: the executor raises without
        them and the evaluator supplied none of them.
        """
        required = {
            a.value: {s.name for s in specs if s.required}
            for a, specs in ACTION_PARAMETERS.items()
        }
        assert required["IDENTIFY_LED"] == {"target"}
        assert required["CONFIG_RESTORE"] == {"attributes_json"}
        assert required["POWER_CAP_ADJUST"] == {"target_watts"}
        assert required["INTERFACE_DISABLE"] == {"interface"}
        assert required["INTERFACE_ENABLE"] == {"interface"}
        # Whole-device classes take none, and that is a real answer.
        for name in ("SEL_CLEAR", "BMC_RESET", "FAN_RESET",
                     "COLLECT_DIAGNOSTICS"):
            assert required[name] == set()

    def test_a_component_addressed_class_resolves_from_reported_evidence(self):
        params, why = resolve_action_params(
            "IDENTIFY_LED", component="Disk.Bay.3", reason="failed",
        )
        assert why == ""
        assert params == {"reason": "failed", "target": "Disk.Bay.3"}

    def test_it_refuses_rather_than_guessing_a_component(self):
        """The whole point. A guessed drive bay lights the wrong LED."""
        params, why = resolve_action_params("IDENTIFY_LED", reason="failed")
        assert params is None
        assert "no component was reported" in why

    def test_an_unsatisfiable_class_names_the_missing_input(self):
        """A22.5: naming what is missing is the deliverable."""
        for name, phrase in (
            ("POWER_CAP_ADJUST", "no power policy exists"),
            ("CONFIG_RESTORE", "computed agent-side"),
        ):
            params, why = resolve_action_params(name, component="x", reason="r")
            assert params is None, name
            assert phrase in why, why

    def test_firmware_is_refused_with_its_own_reason(self):
        """A21.10 unchanged: a fault does not imply a firmware update."""
        params, why = resolve_action_params("FIRMWARE_UPDATE", component="x")
        assert params is None
        assert "campaign orchestration" in why

    def test_validation_rejects_an_undeclared_parameter(self):
        ok, why = validate_action_params("IDENTIFY_LED", {"target": "d", "x": 1})
        assert not ok and "'x'" in why

    def test_validation_matches_the_executors_own_json_rule(self):
        """CONFIG_RESTORE's executor demands a non-empty JSON object."""
        for bad in ("", "[]", "{}", "not json"):
            ok, _ = validate_action_params(
                "CONFIG_RESTORE", {"attributes_json": bad},
            )
            assert not ok, bad
        ok, _ = validate_action_params(
            "CONFIG_RESTORE", {"attributes_json": '{"K": "v"}'},
        )
        assert ok

    def test_reason_is_accepted_everywhere_and_required_nowhere(self):
        """Existing skill YAML and every node action already carry it."""
        for action in ActionType:
            contract = parameter_contract(action.value)
            names = {p["name"] for p in contract["parameters"]}
            assert "reason" in names, action.value
            assert "reason" not in contract["required"], action.value

    def test_the_capability_read_reports_satisfiability(self):
        """Addressable is not executable, and the read says which."""
        facts = action_facts()
        assert facts["IDENTIFY_LED"]["agent_resolvable"] is True
        assert facts["POWER_CAP_ADJUST"]["agent_resolvable"] is False
        assert facts["POWER_CAP_ADJUST"]["unsatisfiable_reason"]
        # Unimplemented classes still declare, so "no executor" and "takes
        # no parameters" stay distinguishable (A21.9).
        assert facts["INTERFACE_RESET"]["implemented"] is False
        assert facts["INTERFACE_RESET"]["required"] == ["interface"]

    def test_the_registry_read_carries_the_same_contract(self):
        """A22.2: the Registry derives from it, it does not restate it.

        The class row hand-picks fields from `action_facts`, so a field
        added upstream does not appear here by itself -- which is exactly
        how the compose gate caught this one missing.
        """
        from harkeniq_cc.capabilities import build_capability_registry

        registry = build_capability_registry(
            tenant_id=TENANT, devices=[], sites=[],
        )
        rows = {r["action_type"]: r for r in registry["classes"]}
        assert rows["IDENTIFY_LED"]["required_parameters"] == ["target"]
        assert rows["IDENTIFY_LED"]["parameters_resolvable"] is True
        assert rows["POWER_CAP_ADJUST"]["parameters_resolvable"] is False
        assert rows["POWER_CAP_ADJUST"]["parameter_reason"]
        # Beside `reach`, never merged into it (A17.7's rule, fourth use).
        assert "reach" in rows["POWER_CAP_ADJUST"]

    def test_a_skill_cannot_declare_its_own_parameter_vocabulary(self):
        """A22.2: the YAML block is a consumer, never a peer."""
        ok, why = validate_param_names(ActionType.SEL_CLEAR, ["bogus"])
        assert not ok and "does not declare" in why
        ok, why = validate_param_names(ActionType.IDENTIFY_LED, ["reason"])
        assert not ok and "requires 'target'" in why
        ok, _ = validate_param_names(
            ActionType.IDENTIFY_LED, ["target", "reason"],
        )
        assert ok

    def test_the_shipped_skills_satisfy_the_declaration(self):
        """The five shipped skills predate the contract by four releases."""
        import pathlib

        import yaml

        from harkeniq.skills.loader import parse_skill

        files = sorted(pathlib.Path("skills").glob("*.yaml"))
        assert files, "no shipped skills found"
        for path in files:
            parse_skill(yaml.safe_load(path.read_text()), source=str(path))

    def test_no_source_is_left_undeclared(self):
        """A parameter nothing can supply must SAY nothing can supply it."""
        for action, specs in ACTION_PARAMETERS.items():
            for spec in specs:
                if spec.source == SRC_UNAVAILABLE:
                    assert spec.missing_input, f"{action.value}.{spec.name}"
                if spec.source == SRC_COMPONENT:
                    assert spec.required, f"{action.value}.{spec.name}"


# ---------------------------------------------------------------------------
# The evaluator no longer promises what it cannot deliver
# ---------------------------------------------------------------------------


class TestTheEvaluatorSuppliesRealParameters:
    @pytest.mark.asyncio
    async def test_a_proposal_carries_the_component_not_a_generic_reason(self):
        stack = await _stack()
        (site_id,) = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _agent(c, site_id)
        await _activate_row(stack, agent_id)

        created = await agent_runtime.evaluate_agents(stack.state, TENANT)
        assert len(created) == 1
        assert created[0].action_type == "IDENTIFY_LED"
        # Before A5 this was {"reason": ...} and the node raised
        # "IDENTIFY_LED requires a 'target' param".
        assert created[0].params["target"] == "Disk.Bay.1"
        assert created[0].evidence["component"] == "Disk.Bay.1"

    @pytest.mark.asyncio
    async def test_no_component_reported_means_no_proposal_at_all(self):
        """Fail closed. A refusal here is better than one at the node."""
        stack = await _stack()
        (site_id,) = await _seed(stack)
        async with stack.sessionmaker() as session:
            inc = await session.get(CCIncident, "inc-1")
            inc.components = None          # the SM has not reported
            await session.commit()
        async with stack.client() as c:
            agent_id = await _agent(c, site_id)
        await _activate_row(stack, agent_id)

        assert await agent_runtime.evaluate_agents(stack.state, TENANT) == []

    @pytest.mark.asyncio
    async def test_the_worst_component_is_the_one_addressed(self):
        stack = await _stack()
        (site_id,) = await _seed(stack)
        async with stack.sessionmaker() as session:
            inc = await session.get(CCIncident, "inc-1")
            inc.components = [
                {"component": "Disk.Bay.9", "severity": "WARNING", "at": "1"},
                {"component": "Disk.Bay.4", "severity": "CRITICAL", "at": "2"},
            ]
            await session.commit()
        async with stack.client() as c:
            agent_id = await _agent(c, site_id)
        await _activate_row(stack, agent_id)

        created = await agent_runtime.evaluate_agents(stack.state, TENANT)
        assert created[0].params["target"] == "Disk.Bay.4"
        # The others are not lost -- an operator needs to know there were two.
        assert len(created[0].evidence["components_reported"]) == 2


class TestThePreflightTellsTheTruthAboutSatisfiability:
    """A22.5 reaches the one place a customer reads 'is it ready'."""

    @pytest.mark.asyncio
    async def test_a_binding_that_can_never_fire_is_reported(self):
        stack = await _stack()
        (site_id,) = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _agent(c, site_id, capabilities=[
                {"kind": "action_class", "capability_ref": "IDENTIFY_LED"},
                {"kind": "action_class", "capability_ref": "POWER_CAP_ADJUST"},
            ])
            res = await c.post(f"/api/operational-agents/{agent_id}/preflight")
        caps = next(d for d in res.json()["dimensions"]
                    if d["dimension"] == "capabilities")
        assert caps["verdict"] == "warn"
        assert caps["unsatisfiable"] == ["POWER_CAP_ADJUST"]
        assert "no power policy exists" in caps["detail"]

    @pytest.mark.asyncio
    async def test_an_agent_that_could_only_ever_do_nothing_is_blocked(self):
        stack = await _stack()
        (site_id,) = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _agent(c, site_id, capabilities=[
                {"kind": "action_class", "capability_ref": "POWER_CAP_ADJUST"},
            ])
            res = await c.post(f"/api/operational-agents/{agent_id}/preflight")
        caps = next(d for d in res.json()["dimensions"]
                    if d["dimension"] == "capabilities")
        assert caps["verdict"] == "blocked"
        assert "would propose nothing" in caps["detail"]

    @pytest.mark.asyncio
    async def test_a_satisfiable_binding_is_still_ready(self):
        """The check must not turn every agent into a warning."""
        stack = await _stack()
        (site_id,) = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _agent(c, site_id)
            res = await c.post(f"/api/operational-agents/{agent_id}/preflight")
        caps = next(d for d in res.json()["dimensions"]
                    if d["dimension"] == "capabilities")
        assert caps["verdict"] == "ready"


# ---------------------------------------------------------------------------
# 2. The five defects (A22.9 - A22.13)
# ---------------------------------------------------------------------------


class TestD1AttentionIsScoped:
    @pytest.mark.asyncio
    async def test_a_site_scoped_principal_reads_only_its_own_site(self):
        """It was declared READ_SCOPED and filtered nothing."""
        stack = await _stack()
        site_a, site_b = await _seed(stack, sites=("DC-1", "DC-2"))
        async with stack.sessionmaker() as session:
            session.add(CCScopeGrant(
                tenant_id=TENANT, principal_type="user", principal_ref="kc-a",
                realm="", scope_type="site", scope_ref=site_a,
                role="site_admin", granted_by="owner",
            ))
            await session.commit()

        async with stack.client() as c:
            stack.as_person("kc-owner", "owner@example.com", "tenant_owner")
            both = (await c.get("/api/attention/")).json()
            assert {i["agent_id"] for i in both["items"]} == {"node-1", "node-2"}

            stack.as_person("kc-a", "a@example.com", "site_admin")
            mine = (await c.get("/api/attention/")).json()
            assert {i["agent_id"] for i in mine["items"]} == {"node-1"}


class TestD3OneAttentionComposer:
    @pytest.mark.asyncio
    async def test_http_and_in_process_rank_identically(self):
        stack = await _stack()
        await _seed(stack, sites=("DC-1", "DC-2"))
        async with stack.client() as c:
            over_http = (await c.get("/api/attention/")).json()
        async with stack.sessionmaker() as session:
            in_process = await load_attention(session, tenant_id=TENANT)
        assert [(i["agent_id"], i["rank"]) for i in over_http["items"]] == \
               [(i["agent_id"], i["rank"]) for i in in_process["items"]]

    @pytest.mark.asyncio
    async def test_the_band_filter_does_not_renumber_rank(self):
        """It filtered BEFORE ranking, and rank decides budget spend."""
        stack = await _stack()
        await _seed(stack, sites=("DC-1", "DC-2"))
        async with stack.client() as c:
            everything = (await c.get("/api/attention/")).json()["items"]
            ranks = {i["agent_id"]: i["rank"] for i in everything}
            for band in {i["band"] for i in everything}:
                filtered = (await c.get(f"/api/attention/?band={band}")).json()
                for item in filtered["items"]:
                    assert item["rank"] == ranks[item["agent_id"]], band

    def test_the_router_no_longer_carries_its_own_copy(self):
        """Structural, not behavioural: a copy would drift again."""
        import inspect

        from harkeniq_cc.api import attention as router

        source = inspect.getsource(router)
        assert "build_attention" not in source
        assert "load_attention" in source


class TestD4DispatchRechecksLifecycle:
    async def _approved(self, stack, site_id, agent_id):
        from harkeniq_cc.db.models import CCAgentProposal

        async with stack.sessionmaker() as session:
            row = CCAgentProposal(
                tenant_id=TENANT, agent_id=agent_id,
                actor=f"op-agent:{agent_id}@v1", agent_version=1,
                site_id=site_id, device_agent_id="node-1",
                # A24.15: dispatch now revalidates CURRENT reach, so this
                # must name a class the agent is actually bound to. It
                # said SEL_CLEAR while `_agent` binds IDENTIFY_LED -- a
                # proposal no configuration could have produced. Fixed
                # here rather than by weakening the gate, the way A2
                # fixed the stale E0.1 fixture.
                action_type="IDENTIFY_LED", params={"target": "Disk.Bay.1"},
                rationale="r",
                evidence={}, disposition="requires_approval",
                authorization_basis="human_approval", status="approved",
                decided_by="owner@example.com", dedupe_key="k1",
            )
            session.add(row)
            await session.commit()

    @pytest.mark.asyncio
    async def test_an_active_agent_dispatches(self):
        stack = await _stack()
        (site_id,) = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _agent(c, site_id)
        await _activate_row(stack, agent_id)
        await self._approved(stack, site_id, agent_id)
        assert len(await agent_runtime.dispatch_decided(stack.state, TENANT)) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["paused", "retired", "draft"])
    async def test_a_non_running_agent_cannot_dispatch(self, status):
        """A19 D3: an approved proposal is never a guarantee of execution.

        The human-approved path asked NOTHING before A5, so a proposal
        approved yesterday still ran today for an agent since retired.
        """
        from harkeniq_cc.db.models import CCOperationalAgent

        stack = await _stack()
        (site_id,) = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _agent(c, site_id)
        await _activate_row(stack, agent_id)
        await self._approved(stack, site_id, agent_id)
        async with stack.sessionmaker() as session:
            agent = await session.get(CCOperationalAgent, agent_id)
            agent.status = status
            await session.commit()

        assert await agent_runtime.dispatch_decided(stack.state, TENANT) == []
        assert FakeSM.dispatches == []

    @pytest.mark.asyncio
    async def test_a_paused_agent_cannot_dispatch(self):
        from harkeniq_cc.db.models import CCOperationalAgent

        stack = await _stack()
        (site_id,) = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _agent(c, site_id)
        await _activate_row(stack, agent_id)
        await self._approved(stack, site_id, agent_id)
        async with stack.sessionmaker() as session:
            agent = await session.get(CCOperationalAgent, agent_id)
            agent.paused_reason = "operator paused it"
            await session.commit()

        assert await agent_runtime.dispatch_decided(stack.state, TENANT) == []

    @pytest.mark.asyncio
    async def test_a_refusal_does_not_erase_the_humans_approval(self):
        """The decision stands; only the dispatch is refused.

        `withhold_unattended` clears `decided_by`/`decided_at`, which is
        right when an AUTONOMOUS grant is withdrawn and wrong here: a
        named person approved this work, and a lifecycle refusal must not
        remove them from the record.
        """
        from harkeniq_cc.db.models import CCOperationalAgent

        stack = await _stack()
        (site_id,) = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _agent(c, site_id)
        await _activate_row(stack, agent_id)
        await self._approved(stack, site_id, agent_id)
        async with stack.sessionmaker() as session:
            agent = await session.get(CCOperationalAgent, agent_id)
            agent.status = "retired"
            await session.commit()

        assert await agent_runtime.dispatch_decided(stack.state, TENANT) == []
        async with stack.sessionmaker() as session:
            row = (await AgentProposalRepo(session).list_for_agent(
                TENANT, agent_id,
            ))[0]
            assert row.decided_by == "owner@example.com"
            assert row.status == "approved"
            assert "retired" in row.dispatch_reason

    @pytest.mark.asyncio
    async def test_a_revoked_identity_cannot_dispatch(self):
        """A3: revocation beats an already-approved decision."""
        stack = await _stack()
        (site_id,) = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _agent(c, site_id)
        await _activate_row(stack, agent_id)
        await self._approved(stack, site_id, agent_id)
        async with stack.sessionmaker() as session:
            session.add(CCAgentIdentity(
                tenant_id=TENANT, agent_id=agent_id, realm="tenant-demo",
                keycloak_sub="sub-1", keycloak_client_id="c1", status="revoked",
            ))
            await session.commit()

        assert await agent_runtime.dispatch_decided(stack.state, TENANT) == []


class TestD5NoWildcardPermission:
    @pytest.mark.asyncio
    async def test_an_agent_scope_cannot_answer_a_permission_question(self):
        """It answered `permits("action.approve")` with True."""
        stack = await _stack()
        (site_id,) = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _agent(c, site_id)
        async with stack.sessionmaker() as session:
            scope = await load_agent_scope(
                session, tenant_id=TENANT, agent_id=agent_id,
            )
        assert scope.scope_only is True
        with pytest.raises(ScopeError):
            scope.permits("action.approve")
        # WHERE still works -- that is the whole reason this scope exists.
        assert scope.site_ids == frozenset({site_id})

    @pytest.mark.asyncio
    async def test_reach_is_unchanged_by_removing_the_wildcard(self):
        """A narrowed grant must survive exactly as it did under "*"."""
        stack = await _stack()
        site_a, site_b = await _seed(stack, sites=("DC-1", "DC-2"))
        async with stack.client() as c:
            agent_id = await _agent(c, site_a)
        async with stack.sessionmaker() as session:
            session.add(CCScopeGrant(
                tenant_id=TENANT, principal_type="agent",
                principal_ref=agent_id, realm="", scope_type="site",
                scope_ref=site_b, permission_subset=["fleet.view"],
                granted_by="owner",
            ))
            await session.commit()
            scope = await load_agent_scope(
                session, tenant_id=TENANT, agent_id=agent_id,
            )
        assert site_b in scope.site_ids

    def test_the_marker_is_not_a_permission_and_is_not_a_wildcard(self):
        assert SCOPE_ONLY_MARKER != "*"
        assert SCOPE_ONLY_MARKER not in MACHINE_PRINCIPAL_CEILING


class TestD2EnforcementIsReportedBeforeItIsEnforced:
    @pytest.mark.asyncio
    async def test_the_impact_report_names_a_scopeless_agent(self):
        """The population that actually changes under enforcement.

        An agent's scope rows ARE `cc_scope_grants` rows (E1.2 merged
        `cc_agent_scopes` in), so a scoped agent already holds grants. The
        one that matters is the SCOPELESS agent: A0 says no rows means no
        devices, and `legacy_open` then synthesizes a tenant-wide grant
        and hands it the entire estate. That inversion is D2, and this
        report is how an admin finds it before enforcement bites.
        """
        stack = await _stack()
        (site_id,) = await _seed(stack)
        # A23-5: this report exists for an admin deciding whether to
        # FLIP, so the tenant it describes is a legacy one -- pinned the
        # way migration 0021 pins a pre-A23-5 tenant (A23.11). Left
        # rowless the tenant is already strict and there is nothing to
        # report in advance of.
        await seed_legacy(stack.sessionmaker, TENANT)
        async with stack.client() as c:
            scoped = await _agent(c, site_id)
            scopeless = await _agent(c, site_id, name="Loose", scopes=[])
            report = (await c.get(
                "/api/tenant-settings/scope-enforcement/impact"
            )).json()
        at_risk = [a["agent_id"] for a in report["agents_without_grant"]]
        assert report["enforced"] is False
        assert scopeless in at_risk
        assert scoped not in at_risk
        assert "no grant" in report["invariant"]

    @pytest.mark.asyncio
    async def test_a_scopeless_agent_reaches_nothing_under_any_posture(self):
        """The defect this test used to assert as PRESENT, now closed.

        Until A23-4, `legacy_open` synthesized tenant-wide reach for a
        grantless principal of either kind, so "no scope rows = no
        devices" (A0) was inverted for an agent. A23.10: an Operational
        Agent receives no synthesis under any posture. The impact
        report above still lists it -- reporting stayed; the reach went.
        """
        stack = await _stack()
        site_a, site_b = await _seed(stack, sites=("DC-1", "DC-2"))
        async with stack.client() as c:
            scopeless = await _agent(c, site_a, name="Loose", scopes=[])
        async with stack.sessionmaker() as session:
            scope = await load_agent_scope(
                session, tenant_id=TENANT, agent_id=scopeless,
            )
        assert scope.tenant_wide is False
        assert scope.site_ids == frozenset()
        assert not any(g.synthesized for g in scope.grants)
        assert scope.synthesis == "agent"
        assert scope.is_empty()

    @pytest.mark.asyncio
    async def test_the_report_admits_it_cannot_enumerate_principals(self):
        """Honesty about the limit is what makes the list actionable."""
        stack = await _stack()
        await _seed(stack)
        async with stack.client() as c:
            report = (await c.get(
                "/api/tenant-settings/scope-enforcement/impact"
            )).json()
        assert report["enumerable"] is False
        assert "never acted will not appear" in report["enumerable_note"]

    @pytest.mark.asyncio
    async def test_reporting_changes_no_behaviour(self):
        """Report-before-enforce means the report enforces NOTHING."""
        stack = await _stack()
        (site_id,) = await _seed(stack)
        async with stack.client() as c:
            before = (await c.get("/api/fleet/")).json()
            await c.get("/api/tenant-settings/scope-enforcement/impact")
            after = (await c.get("/api/fleet/")).json()
        assert before == after


# ---------------------------------------------------------------------------
# 3. Dry-run (A22.7, A22.8)
# ---------------------------------------------------------------------------


async def _table_snapshot(stack) -> dict:
    from sqlalchemy import func, select

    from harkeniq_cc.db.base import Base

    counts = {}
    async with stack.sessionmaker() as session:
        for table in Base.metadata.sorted_tables:
            counts[table.name] = (
                await session.execute(select(func.count()).select_from(table))
            ).scalar_one()
    return counts


class TestDryRunWritesNothing:
    @pytest.mark.asyncio
    async def test_it_returns_what_it_would_propose(self):
        stack = await _stack()
        (site_id,) = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _agent(c, site_id)
            await _activate_row(stack, agent_id)
            result = (await c.get(
                f"/api/operational-agents/{agent_id}/dry-run"
            )).json()
        assert result["dry_run"] is True
        assert len(result["would_propose"]) == 1
        proposed = result["would_propose"][0]
        assert proposed["action_type"] == "IDENTIFY_LED"
        assert proposed["params"]["target"] == "Disk.Bay.1"
        assert proposed["requires_human"] is True

    @pytest.mark.asyncio
    async def test_it_writes_absolutely_nothing(self):
        """Proven by table snapshot, never by reading the handler."""
        stack = await _stack()
        (site_id,) = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _agent(c, site_id)
            await _activate_row(stack, agent_id)
            before = await _table_snapshot(stack)
            for _ in range(3):
                res = await c.get(
                    f"/api/operational-agents/{agent_id}/dry-run"
                )
                assert res.status_code == 200
            after = await _table_snapshot(stack)
        assert before == after
        assert FakeSM.dispatches == []

    @pytest.mark.asyncio
    async def test_it_reasons_exactly_as_the_runtime_does(self):
        """A preview that disagreed with the runtime is worse than none."""
        stack = await _stack()
        (site_id,) = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _agent(c, site_id)
            await _activate_row(stack, agent_id)
            preview = (await c.get(
                f"/api/operational-agents/{agent_id}/dry-run"
            )).json()["would_propose"]
        real = await agent_runtime.evaluate_agents(stack.state, TENANT)
        assert [(p["device_agent_id"], p["action_type"], p["params"],
                 p["disposition"]) for p in preview] == \
               [(r.device_agent_id, r.action_type, r.params, r.disposition)
                for r in real]

    @pytest.mark.asyncio
    async def test_it_reports_what_it_withheld_and_why(self):
        """The other half of the answer: what did you NOT propose?"""
        stack = await _stack()
        (site_id,) = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _agent(c, site_id, capabilities=[
                {"kind": "action_class", "capability_ref": "IDENTIFY_LED"},
                {"kind": "action_class", "capability_ref": "POWER_CAP_ADJUST"},
            ])
            await _activate_row(stack, agent_id)
            async with stack.sessionmaker() as session:
                inc = await session.get(CCIncident, "inc-1")
                inc.subsystem = "thermal"
                inc.components = [{"component": "Inlet", "severity": "CRITICAL"}]
                await session.commit()
            result = (await c.get(
                f"/api/operational-agents/{agent_id}/dry-run"
            )).json()
        codes = {w["code"] for w in result["withheld"]}
        assert "parameters_unresolvable" in codes
        reason = next(w["reason"] for w in result["withheld"]
                      if w["code"] == "parameters_unresolvable")
        assert "no power policy exists" in reason

    @pytest.mark.asyncio
    async def test_an_operator_cannot_preview_an_agent_beyond_their_reach(self):
        """A preview shows the agent's WHOLE reach, so the caller needs it.

        Narrowing the answer to the caller would be worse than refusing:
        a partial preview is not what the agent would do.
        """
        stack = await _stack()
        site_a, site_b = await _seed(stack, sites=("DC-1", "DC-2"))
        async with stack.client() as c:
            wide = await _agent(c, site_a, name="Wide", scopes=[
                {"scope_type": "site", "scope_ref": site_a},
                {"scope_type": "site", "scope_ref": site_b},
            ])
        async with stack.sessionmaker() as session:
            session.add(CCScopeGrant(
                tenant_id=TENANT, principal_type="user", principal_ref="kc-a",
                realm="", scope_type="site", scope_ref=site_a,
                role="site_admin", granted_by="owner",
            ))
            await session.commit()
        async with stack.client() as c:
            stack.as_person("kc-a", "a@example.com", "site_admin")
            res = await c.get(f"/api/operational-agents/{wide}/dry-run")
        assert res.status_code == 403
        assert "outside your authorized scope" in res.text

    @pytest.mark.asyncio
    async def test_a_machine_identity_may_dry_run_its_own_agent(self):
        """A22.8: no ceiling change -- fleet.view already carries it."""
        stack = await _stack()
        (site_id,) = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _agent(c, site_id)
            await _activate_row(stack, agent_id)
            stack.as_agent(agent_id)
            res = await c.get(f"/api/operational-agents/{agent_id}/dry-run")
        assert res.status_code == 200
        assert res.json()["agent_id"] == agent_id

    @pytest.mark.asyncio
    async def test_a_machine_identity_may_not_dry_run_another_agent(self):
        stack = await _stack()
        (site_id,) = await _seed(stack)
        async with stack.client() as c:
            mine = await _agent(c, site_id)
            theirs = await _agent(c, site_id, name="Day Shift")
            await _activate_row(stack, theirs)
            stack.as_agent(mine)
            res = await c.get(f"/api/operational-agents/{theirs}/dry-run")
        assert res.status_code == 403
        assert "its own agent and no other" in res.text

    @pytest.mark.asyncio
    async def test_dry_run_confers_no_authority(self):
        """Discovery is not decision is not execution (A22.14)."""
        stack = await _stack()
        (site_id,) = await _seed(stack)
        async with stack.client() as c:
            agent_id = await _agent(c, site_id)
            await _activate_row(stack, agent_id)
            stack.as_agent(agent_id)
            await c.get(f"/api/operational-agents/{agent_id}/dry-run")
            # Reading what it would do grants nothing at all.
            for method, path in (
                ("post", f"/api/operational-agents/{agent_id}/activate"),
                ("post", "/api/approvals/batch"),
            ):
                res = await getattr(c, method)(path, json={})
                assert res.status_code == 403, path
