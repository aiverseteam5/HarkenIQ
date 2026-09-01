"""A4: capability expansion is about ADDRESSABILITY, not authority (A21).

The platform implements 12 of its 14 action types. An agent could propose
6, and one of those six had no executor at all -- so seven implemented,
governed, node-executable capabilities were invisible to every agent. Not
forbidden, not fenced, not denied. Unreachable, because nothing mapped a
condition to them.

The tests that matter most here are the NEGATIVE ones. Making a capability
addressable must not make it permitted, in scope, autonomous, approved or
executable, and each of those is asserted separately -- a slice that
widened one of them while claiming to widen none would otherwise look
exactly like this one.
"""

from __future__ import annotations

import httpx
import pytest

from harkeniq.capabilities import action_facts
from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.app import create_app
from harkeniq_cc.auth import ROLE_PERMISSIONS, UserContext, configure_auth
from harkeniq_cc.autonomy import LEVEL_2_ACTIONS, LEVEL_3_ACTIONS
from harkeniq_cc.capability_catalogue import (
    CAMPAIGN_ONLY_CLASSES,
    SEED,
    SUBSYSTEM_UNREACHABLE,
    candidates_for,
    catalogue_view,
    validate_entry,
)
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.models import CCAutonomyBudget, CCFleetCache, CCSite
from harkeniq_cc.db.repos import CapabilityCatalogueRepo
from harkeniq_cc.runtime import AppState

TENANT = "t1"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class Stack:
    def __init__(self, app, state):
        self.app, self.state = app, state
        self.sessionmaker = state.sessionmaker
        self.persona = ("kc-owner", "owner@example.com", "tenant_owner")

    def as_person(self, sub, email, role="tenant_owner"):
        self.persona = (sub, email, role)
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
    sessionmaker = make_sessionmaker(engine)
    state = AppState(config=config, engine=engine, sessionmaker=sessionmaker)
    app = create_app(state)
    stack = Stack(app, state)

    async def _fake():
        sub, email, role = stack.persona
        return UserContext(
            user_id=sub, email=email, tenant_id=TENANT, role=role,
            permissions=list(ROLE_PERMISSIONS[role]),
        )

    app.dependency_overrides[get_current_user] = _fake
    return stack


async def _seed(stack, *, device_class="server", level=2) -> str:
    async with stack.sessionmaker() as session:
        site = CCSite(tenant_id=TENANT, site_name="DC-1",
                      sm_endpoint="sm:50051", sm_token="tok")
        session.add(site)
        await session.flush()
        session.add(CCFleetCache(
            site_id=site.id, agent_id="node-1", agent_name="n1",
            vendor="Dell", model="R750", device_class=device_class,
            observation="observed", health="OK",
            capabilities={
                "reach_known": True,
                "implemented": ["SEL_CLEAR", "COLLECT_DIAGNOSTICS",
                                "POWER_CAP_ADJUST", "INTERFACE_DISABLE",
                                "INTERFACE_ENABLE", "CONFIG_RESTORE"],
                "allowed": ["SEL_CLEAR", "COLLECT_DIAGNOSTICS"],
                "effective": ["SEL_CLEAR", "COLLECT_DIAGNOSTICS"],
            },
        ))
        session.add(CCAutonomyBudget(
            tenant_id=TENANT, device_type="*", level=level,
            budget_limit=100, budget_period="daily",
        ))
        await session.commit()
        return site.id


class _Row:
    def __init__(self, subsystem, action_type, enabled=True):
        self.subsystem, self.action_type = subsystem, action_type
        self.because, self.provenance, self.enabled = "b", "p", enabled


# ---------------------------------------------------------------------------
# The gap A4 exists to close
# ---------------------------------------------------------------------------


class TestTheGapIsClosed:
    def test_every_seed_entry_names_an_implemented_class(self):
        """A catalogue entry naming an unimplemented class is a dead map.

        That is precisely what the `interface` subsystem was: it named
        CLEAR_COUNTERS, which no executor implements, so A17's zero-reach
        rule refused the binding and a switch-scoped agent had no
        proposable action at all.
        """
        facts = action_facts()
        for entry in SEED:
            fact = facts.get(entry["action_type"])
            assert fact is not None, entry
            assert fact["implemented"], (
                f"{entry['action_type']} is mapped to "
                f"{entry['subsystem']} but no executor implements it"
            )

    def test_the_interface_subsystem_is_no_longer_dead(self):
        rows = [_Row(e["subsystem"], e["action_type"]) for e in SEED]
        got = {c["action_type"] for c in candidates_for(rows, "interface")}
        assert "CLEAR_COUNTERS" not in got, (
            "the subsystem still maps to a class no executor implements"
        )
        assert got == {"INTERFACE_DISABLE", "INTERFACE_ENABLE"}, got

    def test_previously_invisible_implemented_classes_are_addressable(self):
        """The seven-row gap, closed where a real condition exists."""
        mapped = {e["action_type"] for e in SEED}
        for newly in ("POWER_CAP_ADJUST", "POWER_CYCLE", "CONFIG_RESTORE",
                      "INTERFACE_ENABLE", "INTERFACE_DISABLE"):
            assert newly in mapped, newly

    def test_firmware_stays_campaign_only(self):
        """A21.10: an agent does not propose a firmware update on a fault.

        Inventing a condition for these would be inventing a remediation
        model nobody asked for. They stay reachable through S6.
        """
        mapped = {e["action_type"] for e in SEED}
        assert not (CAMPAIGN_ONLY_CLASSES & mapped), CAMPAIGN_ONLY_CLASSES
        for cls in CAMPAIGN_ONLY_CLASSES:
            assert action_facts()[cls]["implemented"], (
                f"{cls} must stay implemented -- it is reachable, just not "
                f"by an agent responding to a condition"
            )

    def test_every_seed_subsystem_is_a_condition_the_runtime_produces(self):
        """Keying on a condition that never occurs recreates the bug.

        These are `health_summary` keys from the shipped skills, the
        `sensor_id` prefixes the agent emits, and the synthesized
        unreachable-controller condition.
        """
        real = {
            "disk", "fan", "memory", "psu", "thermal", "interface",  # skills
            "log", "config", "os",                                   # sensor ids
            SUBSYSTEM_UNREACHABLE,                                   # synthesized
        }
        for entry in SEED:
            assert entry["subsystem"] in real, entry["subsystem"]

    def test_every_entry_explains_itself(self):
        for entry in SEED:
            assert entry["because"], entry
            assert entry["provenance"], entry


# ---------------------------------------------------------------------------
# Addressable is NOT any of the other five
# ---------------------------------------------------------------------------


class TestAddressableIsNotAuthority:
    def test_a4_maps_NOTHING_new_into_the_autonomy_ladder(self):
        """A21.5, and the decision behind it.

        Evidence of effectiveness is not authority to execute unattended.
        A slice that quietly promoted COLLECT_DIAGNOSTICS while claiming
        to expand addressability would look exactly like this one.
        """
        granted = set(LEVEL_2_ACTIONS) | set(LEVEL_3_ACTIONS)
        assert granted == {
            "SEL_CLEAR", "BMC_RESET",
            "CONFIG_RESTORE", "POWER_CAP_ADJUST", "POWER_CYCLE",
        }, granted
        for never in ("COLLECT_DIAGNOSTICS", "IDENTIFY_LED", "FAN_RESET",
                      "INTERFACE_ENABLE", "INTERFACE_DISABLE",
                      "CLEAR_COUNTERS", "INTERFACE_RESET"):
            assert never not in granted, (
                f"{never} was mapped into the ladder; A4 must not widen autonomy"
            )

    def test_newly_addressable_classes_are_not_budget_mapped(self):
        """So they require a named human, however effective they are."""
        granted = set(LEVEL_2_ACTIONS) | set(LEVEL_3_ACTIONS)
        for newly in ("INTERFACE_ENABLE", "INTERFACE_DISABLE"):
            assert newly not in granted, newly

    def test_the_catalogue_states_it_confers_nothing(self):
        rows = [_Row(e["subsystem"], e["action_type"]) for e in SEED]
        view = catalogue_view(rows, None)
        assert "grants nothing" in view["contract"]["authority"]
        assert "only authority" in view["contract"]["registry"]
        assert "not being autonomous" in view["contract"]["autonomy"]


# ---------------------------------------------------------------------------
# Validation: refuse on CAPABILITY, never on policy
# ---------------------------------------------------------------------------


class TestCatalogueValidation:
    def _facts(self):
        facts = action_facts()
        return set(facts), {k for k, v in facts.items() if v["implemented"]}

    def test_an_unimplemented_class_cannot_be_mapped(self):
        known, impl = self._facts()
        for cls in ("CLEAR_COUNTERS", "INTERFACE_RESET"):
            ok, why = validate_entry("interface", cls, known=known,
                                     implemented=impl)
            assert ok is False
            assert "no executor" in why, why

    def test_an_unknown_class_is_refused(self):
        known, impl = self._facts()
        ok, why = validate_entry("fan", "REBOOT_THE_RACK", known=known,
                                 implemented=impl)
        assert ok is False and "governs" in why

    def test_a_campaign_only_class_is_refused_with_its_reason(self):
        known, impl = self._facts()
        ok, why = validate_entry("fan", "FIRMWARE_UPDATE", known=known,
                                 implemented=impl)
        assert ok is False
        assert "campaigns" in why, why

    def test_policy_does_NOT_refuse_a_mapping(self):
        """A17.7's boundary, held.

        An allow list is mutable operator policy. Refusing on it would
        make it impossible to configure a catalogue ahead of a config
        rollout, and would promote a node setting into a hard Central
        Command constraint.
        """
        known, impl = self._facts()
        # POWER_CYCLE is implemented and on no demo node's allow list.
        ok, why = validate_entry("bmc", "POWER_CYCLE", known=known,
                                 implemented=impl)
        assert ok is True, why

    def test_a_missing_subsystem_is_refused(self):
        known, impl = self._facts()
        ok, why = validate_entry("", "SEL_CLEAR", known=known, implemented=impl)
        assert ok is False and "subsystem" in why

    def test_an_unmapped_subsystem_yields_no_candidate(self):
        rows = [_Row(e["subsystem"], e["action_type"]) for e in SEED]
        assert candidates_for(rows, "nothing-maps-here") == []

    def test_a_disabled_entry_yields_no_candidate(self):
        rows = [_Row("log", "SEL_CLEAR", enabled=False)]
        assert candidates_for(rows, "log") == []


# ---------------------------------------------------------------------------
# The wired surface
# ---------------------------------------------------------------------------


class TestCatalogueAPI:
    @pytest.mark.asyncio
    async def test_a_tenant_is_seeded_on_first_read(self):
        stack = await _stack()
        await _seed(stack)
        async with stack.client() as c:
            got = (await c.get("/api/capabilities/catalogue")).json()
            subs = {s["subsystem"] for s in got["subsystems"]}
            assert "interface" in subs and "log" in subs
            iface = next(s for s in got["subsystems"]
                         if s["subsystem"] == "interface")
            assert {e["action_type"] for e in iface["candidates"]} == {
                "INTERFACE_DISABLE", "INTERFACE_ENABLE"
            }

    @pytest.mark.asyncio
    async def test_registry_reach_is_joined_BESIDE_the_mapping(self):
        """A17.7 for a third consumer: capability is not folded in.

        "This condition maps to this action" and "an executor can
        currently perform it here" are different facts, and an operator
        debugging a silent agent needs to see which one is missing.
        """
        stack = await _stack()
        await _seed(stack)
        async with stack.client() as c:
            got = (await c.get("/api/capabilities/catalogue")).json()
            entry = next(
                e for s in got["subsystems"] for e in s["candidates"]
                if e["action_type"] == "SEL_CLEAR"
            )
            assert "capability" in entry
            assert entry["action_type"] == "SEL_CLEAR"
            assert entry["because"] and entry["provenance"]

    @pytest.mark.asyncio
    async def test_a_replacement_refuses_an_unimplemented_class(self):
        stack = await _stack()
        await _seed(stack)
        async with stack.client() as c:
            res = await c.put("/api/capabilities/catalogue", json={
                "entries": [{"subsystem": "interface",
                             "action_type": "CLEAR_COUNTERS"}],
            })
            assert res.status_code == 400
            assert "no executor" in res.json()["detail"]

    @pytest.mark.asyncio
    async def test_a_replacement_refuses_a_duplicate_mapping(self):
        stack = await _stack()
        await _seed(stack)
        async with stack.client() as c:
            res = await c.put("/api/capabilities/catalogue", json={
                "entries": [
                    {"subsystem": "log", "action_type": "SEL_CLEAR"},
                    {"subsystem": "log", "action_type": "SEL_CLEAR"},
                ],
            })
            assert res.status_code == 400
            assert "twice" in res.json()["detail"]

    @pytest.mark.asyncio
    async def test_a_replacement_is_audited_and_the_chain_verifies(self):
        stack = await _stack()
        await _seed(stack)
        async with stack.client() as c:
            res = await c.put("/api/capabilities/catalogue", json={
                "entries": [{"subsystem": "log", "action_type": "SEL_CLEAR",
                             "because": "b", "provenance": "p"}],
            })
            assert res.status_code == 200, res.text
            assert res.json()["entries"] == 1
            entries = (await c.get(
                "/api/audit/?action=capability_catalogue.replaced"
            )).json()["entries"]
            assert entries and entries[0]["actor"]
            assert (await c.get("/api/audit/verify")).json()["valid"] is True

    @pytest.mark.asyncio
    async def test_the_read_is_fleet_view_and_the_write_is_site_manage(self):
        stack = await _stack()
        await _seed(stack)
        async with stack.client() as c:
            stack.as_person("kc-v", "v@example.com", "viewer")
            assert (
                await c.get("/api/capabilities/catalogue")
            ).status_code == 200
            assert (
                await c.put("/api/capabilities/catalogue",
                            json={"entries": []})
            ).status_code == 403

    @pytest.mark.asyncio
    async def test_an_auditor_reads_and_cannot_write(self):
        stack = await _stack()
        await _seed(stack)
        async with stack.client() as c:
            stack.as_person("kc-au", "au@example.com", "auditor")
            assert (
                await c.get("/api/capabilities/catalogue")
            ).status_code == 200
            assert (
                await c.put("/api/capabilities/catalogue",
                            json={"entries": []})
            ).status_code == 403

    @pytest.mark.asyncio
    async def test_one_tenants_catalogue_is_not_anothers(self):
        stack = await _stack()
        await _seed(stack)
        async with stack.sessionmaker() as session:
            repo = CapabilityCatalogueRepo(session)
            await repo.replace(
                "other-tenant",
                [{"subsystem": "log", "action_type": "SEL_CLEAR"}],
                "someone",
            )
            await session.commit()
            mine = await repo.list_for_tenant(TENANT)
            theirs = await repo.list_for_tenant("other-tenant")
        assert len(theirs) == 1
        assert len(mine) == len(SEED)


# ---------------------------------------------------------------------------
# The proposable surface follows the catalogue
# ---------------------------------------------------------------------------


class TestProposableFollowsTheCatalogue:
    @pytest.mark.asyncio
    async def test_the_agent_catalogue_reports_the_new_conditions(self):
        stack = await _stack()
        await _seed(stack)
        async with stack.client() as c:
            cat = (await c.get("/api/operational-agents/catalogue")).json()
            by = {x["action_type"]: x for x in cat["action_classes"]}
            assert by["POWER_CAP_ADJUST"]["proposable"] is True
            assert "thermal" in by["POWER_CAP_ADJUST"]["observed_conditions"]
            assert by["INTERFACE_DISABLE"]["proposable"] is True
            # Deliberate exclusions stay excluded, and say so.
            assert by["FIRMWARE_UPDATE"]["proposable"] is False
            assert by["CLEAR_COUNTERS"]["proposable"] is False
            assert by["CLEAR_COUNTERS"]["note"]

    @pytest.mark.asyncio
    async def test_editing_the_catalogue_changes_what_is_proposable(self):
        """The point of moving it out of a constant."""
        stack = await _stack()
        await _seed(stack)
        async with stack.client() as c:
            await c.put("/api/capabilities/catalogue", json={
                "entries": [{"subsystem": "log", "action_type": "SEL_CLEAR"}],
            })
            cat = (await c.get("/api/operational-agents/catalogue")).json()
            by = {x["action_type"]: x for x in cat["action_classes"]}
            assert by["SEL_CLEAR"]["proposable"] is True
            # Everything the operator removed stops being proposable.
            assert by["POWER_CAP_ADJUST"]["proposable"] is False
            assert by["INTERFACE_DISABLE"]["proposable"] is False


# ---------------------------------------------------------------------------
# The execution gate becomes the runtime path (A21.6)
# ---------------------------------------------------------------------------


class TestExecutionGateConsolidation:
    """`execution_permitted()` had NO production caller.

    E1.3 shipped a ten-input fail-closed model and A17.8 recorded that its
    `capability` slot was unsupplied. The broader truth: nothing called
    the function at all -- the runtime used hand-written sequential checks
    alongside it. Two statements of one rule that could drift.
    """

    def _sm(self):
        import sys
        sys.path.insert(0, "services/site_manager/src")
        from harkeniq_sm import stopswitch

        return stopswitch

    def test_the_three_stages_partition_the_inputs_exactly(self):
        """No input may be dropped by the split, or evaluated by nobody."""
        sm = self._sm()
        stages = set(sm.CC_INPUTS) | set(sm.SM_DISPATCH_INPUTS) | set(sm.NODE_INPUTS)
        assert stages == set(sm.DECISION_INPUTS), (
            stages ^ set(sm.DECISION_INPUTS)
        )
        # And no input is claimed by two stages.
        total = len(sm.CC_INPUTS) + len(sm.SM_DISPATCH_INPUTS) + len(sm.NODE_INPUTS)
        assert total == len(sm.DECISION_INPUTS), "an input is owned twice"

    def test_capability_is_one_of_the_site_managers_inputs(self):
        """A17.8's reserved slot, finally supplied by a real stage."""
        sm = self._sm()
        assert "capability" in sm.SM_DISPATCH_INPUTS

    def test_an_unevaluated_input_still_refuses_inside_a_stage(self):
        """Narrowing `required` must not weaken the rule within it."""
        sm = self._sm()
        d = sm.execution_permitted(
            required=sm.SM_DISPATCH_INPUTS,
            tenant_stop=True, site_stop=True, manager_halt=True,
            agent_scope=True, autonomy=True,   # capability omitted
        )
        assert d.permitted is False
        assert d.refused_by == "capability"
        assert "never evaluated" in d.reason

    def test_the_full_chain_meaning_is_unchanged(self):
        """The default is still all ten, so nothing else moved."""
        sm = self._sm()
        d = sm.execution_permitted(tenant_stop=True)
        assert d.permitted is False, "a partial full-chain call must refuse"
        allc = {name: True for name in sm.DECISION_INPUTS}
        assert sm.execution_permitted(**allc).permitted is True

    def test_a_refusing_input_names_itself(self):
        sm = self._sm()
        d = sm.execution_permitted(
            required=sm.SM_DISPATCH_INPUTS,
            tenant_stop=True, site_stop="site halted for maintenance",
            manager_halt=True, agent_scope=True, capability=True, autonomy=True,
        )
        assert d.permitted is False
        assert d.refused_by == "site_stop"
        assert d.reason == "site halted for maintenance"

    def test_the_site_manager_dispatch_actually_calls_it(self):
        """The whole point: model and runtime become one thing.

        Asserted over the SOURCE, because a behavioural test would pass
        just as well against the hand-written checks it replaced.
        """
        import inspect
        import sys
        sys.path.insert(0, "services/site_manager/src")
        from harkeniq_sm.grpc_server import SiteManagerServiceServicer

        src = inspect.getsource(SiteManagerServiceServicer._sm_execution_decision)
        assert "execution_permitted(" in src
        assert "SM_DISPATCH_INPUTS" in src
        # And DispatchAction defers to it rather than re-checking inline.
        dispatch = inspect.getsource(SiteManagerServiceServicer.DispatchAction)
        assert "_sm_execution_decision(" in dispatch
        assert "decision.permitted" in dispatch

    def test_an_undeclared_device_does_not_refuse_on_capability(self):
        """UNKNOWN is not incapable. A fleet mid-upgrade is undeclared."""
        from harkeniq.capabilities import implemented_actions

        assert implemented_actions(None) is None
        assert implemented_actions({"reach_known": False}) is None

    def test_a_declared_and_absent_class_is_refusable(self):
        from harkeniq.capabilities import implemented_actions

        impl = implemented_actions({
            "reach_known": True, "implemented": ["SEL_CLEAR"],
        })
        assert impl == frozenset({"SEL_CLEAR"})
        assert "POWER_CYCLE" not in impl

    def test_implemented_actions_has_exactly_one_implementation(self):
        """Three services ask it; a second copy would be a second answer."""
        from harkeniq.capabilities import implemented_actions as shared
        from harkeniq_cc.capabilities import implemented_actions as cc

        assert shared is cc
