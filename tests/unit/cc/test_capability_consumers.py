"""Capability Registry consumers: the Operational Agent may not act on
capability the fleet does not have.

The Registry is a read; these are the two places where reading it
CHANGES an outcome, which is where it earns its keep:

  binding    an agent may not be bound to a class no executor implements,
             or to one no device in its own scope can execute
  proposal   an agent may not propose a class its target device cannot
             run, even when the tenant contract grants it

Both refusals are precise about which of the six questions failed. A
class refused here is refused for CAPABILITY reasons -- not permission,
not scope, not autonomy, not approval -- and the message says so, so an
operator is sent to the right fix.

And both keep the same discipline as the composer: unknown never
refuses. Only provable zero reach does.
"""

from __future__ import annotations

import httpx
import pytest

from harkeniq.capabilities import declare
from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.app import create_app
from harkeniq_cc.auth import ROLE_PERMISSIONS, UserContext, configure_auth
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.models import CCFleetCache, CCSite
from harkeniq_cc.runtime import AppState

TENANT = "t1"

SERVER_DECL = declare(
    "redfish", ["IDENTIFY_LED", "COLLECT_DIAGNOSTICS", "SEL_CLEAR"], "server"
)
SWITCH_DECL = declare("gnmi", ["INTERFACE_DISABLE", "INTERFACE_ENABLE"], "switch")


async def _stack(role: str = "tenant_owner"):
    config = CCConfig(tenant_id=TENANT, insecure=True)
    configure_auth("", "", "", insecure=True)
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sessionmaker = make_sessionmaker(engine)
    app = create_app(
        AppState(config=config, engine=engine, sessionmaker=sessionmaker)
    )

    async def _fake():
        return UserContext(
            user_id=f"kc-{role}", email=f"{role}@example.com",
            tenant_id=TENANT, role=role,
            permissions=list(ROLE_PERMISSIONS.get(role, ROLE_PERMISSIONS["viewer"])),
        )

    app.dependency_overrides[get_current_user] = _fake
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ), sessionmaker


async def _seed(sessionmaker, devices):
    """devices: list of (agent_id, device_class, declaration|None)."""
    async with sessionmaker() as session:
        site = CCSite(
            tenant_id=TENANT, site_name="DC-1",
            sm_endpoint="sm:50051", sm_token="tok",
        )
        session.add(site)
        await session.flush()
        for agent_id, device_class, declaration in devices:
            session.add(CCFleetCache(
                site_id=site.id, agent_id=agent_id, agent_name=agent_id,
                vendor="Dell", model="R750", device_class=device_class,
                observation="observed", health="OK", capabilities=declaration,
            ))
        await session.commit()
        return site.id


def _body(site_id, refs, name="Night Shift"):
    return {
        "name": name,
        "description": "capability test",
        "scopes": [{"scope_type": "site", "scope_ref": site_id}],
        "capabilities": [
            {"kind": "action_class", "capability_ref": r} for r in refs
        ],
    }


class TestBindingRefusesUnimplementedClasses:
    """D1's consumer half. A platform fact: no database read needed."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["INTERFACE_RESET", "CLEAR_COUNTERS"])
    async def test_refused_with_a_reason_naming_the_missing_executor(self, action):
        client, sm = await _stack()
        site_id = await _seed(sm, [("sw-1", "switch", SWITCH_DECL)])
        r = await client.post(
            "/api/operational-agents/", json=_body(site_id, [action])
        )
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert "no executor in this platform implements" in detail
        assert action in detail
        await client.aclose()

    @pytest.mark.asyncio
    async def test_the_refusal_says_the_class_is_not_being_removed(self):
        """D1: this is a capability truth problem, not a reason to drop
        governed vocabulary, and the operator is told so."""
        client, sm = await _stack()
        site_id = await _seed(sm, [("sw-1", "switch", SWITCH_DECL)])
        r = await client.post(
            "/api/operational-agents/", json=_body(site_id, ["INTERFACE_RESET"])
        )
        detail = r.json()["detail"]
        assert "stays in the vocabulary" in detail
        assert "risk level, preconditions" in detail
        await client.aclose()

    @pytest.mark.asyncio
    async def test_no_agent_is_left_behind_by_the_refusal(self):
        client, sm = await _stack()
        site_id = await _seed(sm, [("sw-1", "switch", SWITCH_DECL)])
        await client.post(
            "/api/operational-agents/", json=_body(site_id, ["INTERFACE_RESET"])
        )
        listing = (await client.get("/api/operational-agents/")).json()
        assert listing["agents"] == []
        await client.aclose()

    @pytest.mark.asyncio
    async def test_an_implemented_class_still_binds(self):
        client, sm = await _stack()
        site_id = await _seed(sm, [("srv-1", "server", SERVER_DECL)])
        r = await client.post(
            "/api/operational-agents/", json=_body(site_id, ["SEL_CLEAR"])
        )
        assert r.status_code == 201, r.text
        await client.aclose()


class TestBindingRefusesZeroReachInScope:
    @pytest.mark.asyncio
    async def test_class_no_in_scope_device_can_run_is_refused(self):
        """Implemented by gnmi, but this agent only reaches servers."""
        client, sm = await _stack()
        site_id = await _seed(sm, [("srv-1", "server", SERVER_DECL)])
        r = await client.post(
            "/api/operational-agents/",
            json=_body(site_id, ["INTERFACE_DISABLE"]),
        )
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert "no device in this agent's scope can execute" in detail
        assert "INTERFACE_DISABLE" in detail
        await client.aclose()

    @pytest.mark.asyncio
    async def test_the_refusal_offers_the_real_fixes(self):
        client, sm = await _stack()
        site_id = await _seed(sm, [("srv-1", "server", SERVER_DECL)])
        detail = (await client.post(
            "/api/operational-agents/",
            json=_body(site_id, ["INTERFACE_DISABLE"]),
        )).json()["detail"]
        assert "Widen the agent's scope" in detail
        # And it must say why an allow-list change would NOT help, since
        # that is the first thing an operator would otherwise try.
        assert "no allow-list change could make it runnable" in detail
        await client.aclose()

    @pytest.mark.asyncio
    async def test_a_reachable_class_binds_on_a_mixed_fleet(self):
        client, sm = await _stack()
        site_id = await _seed(sm, [
            ("srv-1", "server", SERVER_DECL),
            ("sw-1", "switch", SWITCH_DECL),
        ])
        r = await client.post(
            "/api/operational-agents/",
            json=_body(site_id, ["INTERFACE_DISABLE", "SEL_CLEAR"]),
        )
        assert r.status_code == 201, r.text
        await client.aclose()

    @pytest.mark.asyncio
    async def test_update_is_refused_the_same_way_as_create(self):
        """Otherwise the check is a formality anyone can edit around."""
        client, sm = await _stack()
        site_id = await _seed(sm, [("srv-1", "server", SERVER_DECL)])
        created = await client.post(
            "/api/operational-agents/", json=_body(site_id, ["SEL_CLEAR"])
        )
        agent_id = created.json()["id"]
        r = await client.put(
            f"/api/operational-agents/{agent_id}/bindings",
            json=_body(site_id, ["INTERFACE_DISABLE"]),
        )
        assert r.status_code == 400
        assert "no device in this agent's scope" in r.json()["detail"]
        await client.aclose()

    @pytest.mark.asyncio
    async def test_a_refused_update_does_not_change_the_agent(self):
        client, sm = await _stack()
        site_id = await _seed(sm, [("srv-1", "server", SERVER_DECL)])
        created = await client.post(
            "/api/operational-agents/", json=_body(site_id, ["SEL_CLEAR"])
        )
        agent_id = created.json()["id"]
        before = (await client.get(f"/api/operational-agents/{agent_id}")).json()
        await client.put(
            f"/api/operational-agents/{agent_id}/bindings",
            json=_body(site_id, ["INTERFACE_DISABLE"], name="Renamed"),
        )
        after = (await client.get(f"/api/operational-agents/{agent_id}")).json()
        assert after["agent"]["name"] == before["agent"]["name"]
        assert after["agent"]["version"] == before["agent"]["version"]
        await client.aclose()


class TestScopeIsResolvedTheSameWayTheEvaluatorDoes:
    """Found on the live stack: the first version of the zero-reach check
    used the E1.2 repository read filter, which is SITE-based. A
    `device_class` or `device` scope therefore returned no devices
    through it, the check read that as "no fleet yet" and waved the
    binding through -- while the evaluator, which uses `resolve_scope`,
    would never act on it. Two notions of "in scope" is the exact
    divergence this codebase keeps paying for, so both ends use one."""

    @pytest.mark.asyncio
    async def test_a_device_class_scope_is_checked_not_skipped(self):
        """A switch fleet bound to SEL_CLEAR: gNMI has no such code, and
        the scope is expressed by device class, which the repository's
        site-based filter cannot see."""
        client, sm = await _stack()
        await _seed(sm, [("sw-1", "switch", SWITCH_DECL)])
        r = await client.post(
            "/api/operational-agents/",
            json={
                "name": "class-scoped",
                "description": "capability test",
                "scopes": [{"scope_type": "device_class", "scope_ref": "switch"}],
                "capabilities": [
                    {"kind": "action_class", "capability_ref": "SEL_CLEAR"}
                ],
            },
        )
        assert r.status_code == 400, r.text
        assert "no device in this agent's scope" in r.json()["detail"]
        await client.aclose()

    @pytest.mark.asyncio
    async def test_a_device_scope_is_checked_not_skipped(self):
        client, sm = await _stack()
        await _seed(sm, [("srv-1", "server", SERVER_DECL)])
        r = await client.post(
            "/api/operational-agents/",
            json={
                "name": "device-scoped",
                "description": "capability test",
                "scopes": [{"scope_type": "device", "scope_ref": "srv-1"}],
                "capabilities": [
                    {"kind": "action_class", "capability_ref": "INTERFACE_DISABLE"}
                ],
            },
        )
        assert r.status_code == 400, r.text
        await client.aclose()

    @pytest.mark.asyncio
    async def test_a_device_class_scope_still_accepts_what_it_can_run(self):
        client, sm = await _stack()
        await _seed(sm, [("sw-1", "switch", SWITCH_DECL)])
        r = await client.post(
            "/api/operational-agents/",
            json={
                "name": "class-scoped-ok",
                "description": "capability test",
                "scopes": [{"scope_type": "device_class", "scope_ref": "switch"}],
                "capabilities": [
                    {"kind": "action_class", "capability_ref": "INTERFACE_DISABLE"}
                ],
            },
        )
        assert r.status_code == 201, r.text
        await client.aclose()

    @pytest.mark.asyncio
    async def test_a_scope_matching_nothing_still_binds(self):
        """No devices reached is 'not yet', not 'never'. Preserved."""
        client, sm = await _stack()
        await _seed(sm, [("srv-1", "server", SERVER_DECL)])
        r = await client.post(
            "/api/operational-agents/",
            json={
                "name": "empty-class-scope",
                "description": "capability test",
                "scopes": [{"scope_type": "device_class", "scope_ref": "switch"}],
                "capabilities": [
                    {"kind": "action_class", "capability_ref": "SEL_CLEAR"}
                ],
            },
        )
        assert r.status_code == 201, r.text
        await client.aclose()


class TestUnknownNeverRefusesABinding:
    """The rule that keeps a fleet working through an upgrade."""

    @pytest.mark.asyncio
    async def test_an_undeclared_fleet_binds_anything_implemented(self):
        client, sm = await _stack()
        site_id = await _seed(sm, [("srv-1", "server", None)])
        r = await client.post(
            "/api/operational-agents/",
            json=_body(site_id, ["INTERFACE_DISABLE"]),
        )
        assert r.status_code == 201, r.text
        await client.aclose()

    @pytest.mark.asyncio
    async def test_one_undeclared_device_suspends_the_refusal(self):
        """Partial knowledge is not proof: the undeclared device could
        be exactly the one that can run the class."""
        client, sm = await _stack()
        site_id = await _seed(sm, [
            ("srv-1", "server", SERVER_DECL),
            ("srv-2", "server", None),
        ])
        r = await client.post(
            "/api/operational-agents/",
            json=_body(site_id, ["INTERFACE_DISABLE"]),
        )
        assert r.status_code == 201, r.text
        await client.aclose()

    @pytest.mark.asyncio
    async def test_but_an_unimplemented_class_is_still_refused(self):
        """Unknown reach cannot rescue a class with no code anywhere."""
        client, sm = await _stack()
        site_id = await _seed(sm, [("srv-1", "server", None)])
        r = await client.post(
            "/api/operational-agents/",
            json=_body(site_id, ["INTERFACE_RESET"]),
        )
        assert r.status_code == 400
        await client.aclose()

    @pytest.mark.asyncio
    async def test_an_agent_scoped_to_an_empty_site_still_binds(self):
        """An agent built before its fleet arrives is legitimate."""
        client, sm = await _stack()
        site_id = await _seed(sm, [])
        r = await client.post(
            "/api/operational-agents/", json=_body(site_id, ["SEL_CLEAR"])
        )
        assert r.status_code == 201, r.text
        await client.aclose()


# ---------------------------------------------------------------------------
# Proposal guard
# ---------------------------------------------------------------------------

from datetime import datetime, timezone  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from harkeniq_cc.autonomy import build_autonomy  # noqa: E402
from harkeniq_cc.operational_agent import agent_view, evaluate  # noqa: E402

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


def _agent_obj(**kw):
    base = dict(
        id="ag1", tenant_id="t1", name="Night Shift", description="",
        status="active", version=1, autonomy_ceiling=0,
        require_approval_always=True, max_proposals_per_day=25,
        created_by="op@example.com", created_at=NOW, updated_at=NOW,
        activated_by="op@example.com", activated_at=NOW,
        last_evaluated_at=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _cap(kind, ref):
    return SimpleNamespace(kind=kind, capability_ref=ref)


def _dev(agent_id, capabilities=None, device_class="server"):
    return SimpleNamespace(
        agent_id=agent_id, agent_name=agent_id, site_id="s1",
        device_class=device_class, health="OK", observation="observed",
        vendor="Dell", model="R750", capabilities=capabilities,
    )


def _contract(level=2):
    return build_autonomy(
        tenant_id="t1", actor_id="op-agent:ag1@v1", actor_species="agent",
        permissions=["fleet.view"],
        budgets=[SimpleNamespace(
            device_type="*", level=level, budget_limit=10,
            budget_period="daily", actions_used=0,
        )],
        stop_switch=SimpleNamespace(active=False, changed_by="", updated_at=NOW),
        outcomes=[], safety_rows=[],
        sites=[SimpleNamespace(id="s1", site_name="DC-1")],
        learned_signals=[], approval_policies=[],
    )


def _run(caps, devices, incidents, **kw):
    return evaluate(
        agent=_agent_obj(),
        scopes=[SimpleNamespace(scope_type="site", scope_ref="s1")],
        capabilities=caps,
        devices=devices,
        incidents_by_device=incidents,
        autonomy_contract=_contract(),
        now=NOW,
        **kw,
    )


SEL_INCIDENT = {"incident_id": "i1", "subsystem": "log", "title": "SEL full"}


class TestProposalGuard:
    def test_a_declared_capable_device_is_still_proposed_for(self):
        got = _run(
            [_cap("action_class", "SEL_CLEAR")],
            [_dev("d1", capabilities=SERVER_DECL)],
            {"d1": [SEL_INCIDENT]},
        )
        assert [p["action_type"] for p in got] == ["SEL_CLEAR"]

    def test_a_declared_incapable_device_is_never_proposed_for(self):
        """The switch's node cannot run SEL_CLEAR. Before the Registry
        this proposal was made, approved by a human, dispatched, and
        refused at the node -- every single time."""
        got = _run(
            [_cap("action_class", "SEL_CLEAR")],
            [_dev("sw1", capabilities=SWITCH_DECL, device_class="switch")],
            {"sw1": [SEL_INCIDENT]},
        )
        assert got == []

    def test_an_undeclared_device_is_still_proposed_for(self):
        """Unknown never blocks; the node's allow list is still the
        final authority if the guess is wrong."""
        got = _run(
            [_cap("action_class", "SEL_CLEAR")],
            [_dev("d1", capabilities=None)],
            {"d1": [SEL_INCIDENT]},
        )
        assert [p["action_type"] for p in got] == ["SEL_CLEAR"]

    def test_the_capable_device_is_chosen_over_the_incapable_one(self):
        got = _run(
            [_cap("action_class", "SEL_CLEAR")],
            [
                _dev("sw1", capabilities=SWITCH_DECL, device_class="switch"),
                _dev("d1", capabilities=SERVER_DECL),
            ],
            {"sw1": [SEL_INCIDENT], "d1": [SEL_INCIDENT]},
        )
        assert [p["device_agent_id"] for p in got] == ["d1"]

    def test_a_node_that_does_not_permit_the_class_is_STILL_proposed_for(self):
        """Policy is not capability, and the node owns policy.

        redfish implements SEL_CLEAR; this node's allow list does not
        carry it. The proposal is still made: the node's refusal is the
        ratified final authority and becomes attributed evidence in the
        error budget, which is how an operator learns the policy is
        wrong. Withholding it here would hide that, and would also make
        a mutable node setting silently disable a binding.

        The compose gate depends on exactly this: A0+A1 binds SEL_CLEAR
        to a demo node that does not permit it, on purpose."""
        narrow = declare("redfish", ["IDENTIFY_LED"], "server")
        got = _run(
            [_cap("action_class", "SEL_CLEAR")],
            [_dev("d1", capabilities=narrow)],
            {"d1": [SEL_INCIDENT]},
        )
        assert [p["action_type"] for p in got] == ["SEL_CLEAR"]

    def test_a_protocol_that_cannot_do_it_is_never_proposed_for(self):
        """The capability half, which DOES withhold the proposal."""
        got = _run(
            [_cap("action_class", "SEL_CLEAR")],
            [_dev("sw1", capabilities=SWITCH_DECL, device_class="switch")],
            {"sw1": [SEL_INCIDENT]},
        )
        assert got == []


class TestAgentViewConsumesTheRegistry:
    """The Operational Agents page must READ capability truth, never
    carry a capability contract of its own."""

    def _view(self, devices, caps=None):
        return agent_view(
            agent=_agent_obj(),
            scopes=[SimpleNamespace(scope_type="site", scope_ref="s1")],
            capabilities=caps or [_cap("action_class", "SEL_CLEAR")],
            devices=devices,
            autonomy_contract=_contract(),
            now=NOW,
        )

    def test_capability_is_reported_beside_the_disposition(self):
        view = self._view([_dev("d1", capabilities=SERVER_DECL)])
        row = next(r for r in view["capabilities"]["action_classes"] if r["action_type"] == "SEL_CLEAR")
        assert row["capability"]["reach"] == "available"
        assert row["capability"]["reachable_devices"] == 1

    def test_capability_never_overwrites_the_disposition(self):
        """A class the tenant grants but no device can run is 'granted
        and unrunnable', not 'denied' -- telling an operator the second
        sends them to the wrong page."""
        view = self._view(
            [_dev("sw1", capabilities=SWITCH_DECL, device_class="switch")]
        )
        row = next(r for r in view["capabilities"]["action_classes"] if r["action_type"] == "SEL_CLEAR")
        assert row["capability"]["reach"] == "no_effective_reach"
        assert row["capability"]["reachable_devices"] == 0
        # The autonomy disposition is untouched by capability.
        assert row["disposition"] in ("autonomous", "requires_approval", "denied")
        assert "capability" not in (row["disposition_reason"] or "")

    def test_an_undeclared_fleet_reads_unknown_not_zero(self):
        view = self._view([_dev("d1", capabilities=None)])
        row = next(r for r in view["capabilities"]["action_classes"] if r["action_type"] == "SEL_CLEAR")
        assert row["capability"]["reach"] == "unknown"
        assert row["capability"]["undeclared_devices"] == 1

    def test_an_agent_with_no_devices_says_so(self):
        view = self._view([])
        row = next(r for r in view["capabilities"]["action_classes"] if r["action_type"] == "SEL_CLEAR")
        assert row["capability"]["reach"] == "no_devices_in_scope"


class TestCapabilityIsRefusedOnPolicyIsNot:
    """The boundary the compose gate corrected, pinned in both directions.

    `implemented` is a CAPABILITY fact: immutable for a build and a
    device, and the only ground a refusal may stand on. `allow_list` is
    POLICY: an operator can change it this afternoon, and the node
    enforces it as the ratified final execution authority. A Registry
    that refuses on policy has answered question six, which belongs to
    the node -- and in practice makes it impossible to configure an agent
    ahead of a config rollout.

    The A0+A1 compose gate binds SEL_CLEAR to a demo node whose allow
    list does not carry it, deliberately, to prove the node's refusal is
    final and becomes attributed evidence. That binding must succeed.
    """

    @pytest.mark.asyncio
    async def test_a_class_no_node_permits_still_binds(self):
        client, sm = await _stack()
        narrow = declare("redfish", ["IDENTIFY_LED"], "server")
        site_id = await _seed(sm, [("srv-1", "server", narrow)])
        r = await client.post(
            "/api/operational-agents/", json=_body(site_id, ["SEL_CLEAR"])
        )
        assert r.status_code == 201, r.text
        await client.aclose()

    @pytest.mark.asyncio
    async def test_the_gate_binding_is_accepted(self):
        """The exact shape scripts/e2e-compose-gate.sh creates."""
        client, sm = await _stack()
        narrow = declare(
            "redfish", ["IDENTIFY_LED", "COLLECT_DIAGNOSTICS", "FAN_RESET"],
            "server",
        )
        site_id = await _seed(sm, [("node-1", "server", narrow)])
        r = await client.post(
            "/api/operational-agents/",
            json=_body(site_id, ["SEL_CLEAR", "COLLECT_DIAGNOSTICS"]),
        )
        assert r.status_code == 201, r.text
        await client.aclose()

    @pytest.mark.asyncio
    async def test_a_class_no_protocol_implements_is_still_refused(self):
        """The capability half is untouched by the correction."""
        client, sm = await _stack()
        site_id = await _seed(sm, [("srv-1", "server", SERVER_DECL)])
        r = await client.post(
            "/api/operational-agents/",
            json=_body(site_id, ["INTERFACE_DISABLE"]),
        )
        assert r.status_code == 400, r.text
        await client.aclose()

    @pytest.mark.asyncio
    async def test_the_platform_refusal_is_untouched(self):
        client, sm = await _stack()
        site_id = await _seed(sm, [("srv-1", "server", SERVER_DECL)])
        for action in ("INTERFACE_RESET", "CLEAR_COUNTERS"):
            r = await client.post(
                "/api/operational-agents/", json=_body(site_id, [action])
            )
            assert r.status_code == 400, (action, r.text)
        await client.aclose()

    def test_the_two_sets_are_reported_separately(self):
        from harkeniq_cc.capabilities import reachable_action_classes

        narrow = declare("redfish", ["IDENTIFY_LED"], "server")
        reach = reachable_action_classes([_dev("d1", capabilities=narrow)])
        assert "SEL_CLEAR" in reach["implemented"]
        assert "SEL_CLEAR" not in reach["effective"]
        assert reach["unknown"] is False

    def test_agent_view_names_the_policy_obstacle(self):
        """It no longer blocks, so this view is where an operator finds
        out. 'Bound, capable, and nowhere permitted' gets its own name."""
        narrow = declare("redfish", ["IDENTIFY_LED"], "server")
        view = agent_view(
            agent=_agent_obj(),
            scopes=[SimpleNamespace(scope_type="site", scope_ref="s1")],
            capabilities=[_cap("action_class", "SEL_CLEAR")],
            devices=[_dev("d1", capabilities=narrow)],
            autonomy_contract=_contract(),
            now=NOW,
        )
        row = next(
            r for r in view["capabilities"]["action_classes"]
            if r["action_type"] == "SEL_CLEAR"
        )
        assert row["capability"]["reach"] == "not_permitted_on_any_node"
        assert row["capability"]["capable_devices"] == 1
        assert row["capability"]["reachable_devices"] == 0
