"""A30.32 (A6-4B1): governed capability and parameter discovery.

ONE route, `GET /api/operational-agents/{agent_id}/discovery`, answering an
Operational Agent about ITSELF with eight facts per action class -- exists,
implemented, addressable, bound, in effective scope, governance, approval
required, currently operable -- composed from sources that already exist and
never collapsed into an authority boolean.

Everything behavioural here runs on the PRODUCTION stack: the real
`get_scope`, persisted agent grants, STRICT enforcement (the S3 sentinel
estate, `tests/unit/cc/s3_estate.py`). A fixture that synthesised a scope
from the principal's permissions would agree with the code it tests
whatever the code did -- A26.11's lesson -- so none is used.

The estate, as discovery sees it:

    A  s3-alpha    server  redfish, permits SEL_CLEAR BMC_RESET FAN_RESET
                           COLLECT_DIAGNOSTICS IDENTIFY_LED (NOT POWER_CYCLE)
                           suppressed fault domain fault-A
    B  s3-bravo    switch  gnmi, permits INTERFACE_ENABLE INTERFACE_DISABLE
    C  s3-charlie  server  redfish, permits everything it implements;
                           SEL_CLEAR DROPPED BACK, BMC_RESET budget SPENT,
                           SM stop switch ON -- the hidden site
    D  s3-delta    server  UNDECLARED; never reported

The tenant runs at autonomy level 2, so SEL_CLEAR and BMC_RESET are the
budget-granted classes -- and site C's drop-back folds SEL_CLEAR back to a
human TENANT-WIDE at admission, which is exactly the pre-S3-E1 hidden state
D3 exists for.
"""

from __future__ import annotations

import ast
import inspect
import json
import pathlib
import re
import textwrap
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest
import sqlalchemy as sa

import harkeniq_cc
from harkeniq.capabilities import (
    action_facts, declare, parameter_contract, resolve_action_params,
)
from harkeniq.models import ActionType
from harkeniq_cc import discovery as D
from harkeniq_cc import ingress_limits
from harkeniq_cc.autonomy import (
    ACTOR_AGENT, ACTOR_CAMPAIGN, ACTOR_HUMAN, ACTOR_SPECIES, build_autonomy,
)
from harkeniq_cc.db.base import Base
from harkeniq_cc.db.models import (
    CCAgentCapability, CCAgentProposal, CCAgentReadWindow, CCAgentSubmission,
    CCCapabilityCatalogue, CCFleetCache, CCOperationalAgent,
    CCOutcomeHistory, CCSafetyState, CCScopeGrant,
)
from harkeniq_cc.db.repos import StopSwitchRepo
from harkeniq_cc.ingress_limits import READ_MAX_PER_WINDOW, read_window_start
from harkeniq_cc.machine_identity import (
    MACHINE_PRINCIPAL_CEILING, READ_BINDING_PERMISSIONS,
)
from harkeniq_cc.operational_agent import READ_CAPABILITIES, REQUIRED_READS
from harkeniq_cc.route_contract import (
    JOB_AUTONOMY, JOB_METER, JOBS_WITHOUT_A_BINDING, MACHINE_JOBS,
    MACHINE_ONLY_ROUTES, MACHINE_SURFACE, METER_READ, READ_SCOPED,
    ROUTE_CONTRACT, SURFACE_MACHINE, machine_meter, meter_census,
)

from tests.unit.cc import s3_estate as E
from tests.unit.cc.s3_estate import ALL, SITES

PREFIX = "/api/operational-agents"
ROUTE = ("GET", "/api/operational-agents/{agent_id}/discovery")
OPERATOR = "stop-switch-operator@example.com"

#: What each device permits (its node allow list). `None` = undeclared.
ALLOW: dict[str, tuple[str, list[str]] | None] = {
    "A": ("redfish", ["SEL_CLEAR", "BMC_RESET", "FAN_RESET",
                      "COLLECT_DIAGNOSTICS", "IDENTIFY_LED"]),
    "B": ("gnmi", ["INTERFACE_ENABLE", "INTERFACE_DISABLE"]),
    "C": ("redfish", ["SEL_CLEAR", "BMC_RESET", "FAN_RESET", "POWER_CYCLE",
                      "POWER_CAP_ADJUST", "CONFIG_RESTORE", "COLLECT_DIAGNOSTICS",
                      "IDENTIFY_LED", "FIRMWARE_UPDATE", "FIRMWARE_ROLLBACK"]),
    "D": None,
}


# ---------------------------------------------------------------------------
# The estate
# ---------------------------------------------------------------------------


async def _estate(sites=ALL, **kw):
    """The S3 production stack, with node capability declarations."""
    stack = await E.build(sites, **kw)
    async with stack.sessionmaker() as session:
        for key in sites:
            row = (await session.execute(
                sa.select(CCFleetCache).where(
                    CCFleetCache.agent_id == stack.device(key))
            )).scalar_one()
            spec = ALLOW[key]
            row.capabilities = (
                None if spec is None
                else declare(spec[0], spec[1], SITES[key].device_class)
            )
        await session.commit()
    return stack


async def _agent(stack, agent_id, classes=("SEL_CLEAR",), *, ceiling=2,
                 approval_always=False, status="active", budget=0, per_day=25,
                 paused="", reads=REQUIRED_READS):
    async with stack.sessionmaker() as session:
        session.add(CCOperationalAgent(
            id=agent_id, tenant_id=stack.tenant, name=agent_id, status=status,
            version=1, activated_version=1, autonomy_ceiling=ceiling,
            require_approval_always=approval_always,
            max_proposals_per_day=per_day, execution_budget=budget,
            budget_period="daily", paused_reason=paused,
            created_by="builder@example.com", activated_by="activator@example.com",
        ))
        # The bindings reference the agent row; PostgreSQL enforces that
        # foreign key where sqlite does not, so the agent is written first.
        await session.flush()
        for ref in reads:
            session.add(CCAgentCapability(
                agent_id=agent_id, tenant_id=stack.tenant, kind="read",
                capability_ref=ref,
            ))
        for name in classes:
            session.add(CCAgentCapability(
                agent_id=agent_id, tenant_id=stack.tenant, kind="action_class",
                capability_ref=name,
            ))
        await session.commit()
    return agent_id


async def _grant(stack, agent_id, scope_type, key=""):
    """A persisted AGENT grant, exactly as `add_scope` writes one."""
    if scope_type == "site":
        ref = stack.site(key)
    elif scope_type == "org_unit":
        ref = stack.regions[key]
    elif scope_type == "device":
        ref = stack.device(key)
    else:
        ref = key
    return await stack.grant(
        agent_id, scope_type, ref, role="", principal_type="agent",
    )


async def _read(stack, agent_id, *, as_agent=None):
    async with stack.as_machine(as_agent or agent_id).client() as c:
        return await c.get(f"{PREFIX}/{agent_id}/discovery")


async def _discover(stack, agent_id) -> dict:
    res = await _read(stack, agent_id)
    assert res.status_code == 200, res.text
    return res.json()


def klass(body: dict, name: str) -> dict:
    return next(r for r in body["action_classes"] if r["action_type"] == name)


async def _persona(stack, name: str, classes=("SEL_CLEAR",), **kw) -> dict:
    """One machine persona of the S3 matrix, as an AGENT with persisted grants."""
    agent_id = f"b1-{name}"[:32]
    await _agent(stack, agent_id, classes, **kw)
    grants = {
        "tenant": [("tenant", "")],
        "org_ab": [("org_unit", "ab")],
        "site_a": [("site", "A")],
        "site_d": [("site", "D")],
        "site_c": [("site", "C")],
        "site_b": [("site", "B")],
        "device_c": [("device", "C")],
        "class_switch": [("device_class", "switch")],
        "empty": [],
    }[name]
    for scope_type, key in grants:
        await _grant(stack, agent_id, scope_type, key)
    return await _discover(stack, agent_id)


def _keys_and_values(node, keys: set, values: list) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            keys.add(key)
            _keys_and_values(value, keys, values)
    elif isinstance(node, list):
        for item in node:
            _keys_and_values(item, keys, values)
    else:
        values.append(node)


def walk(payload) -> tuple[set, list]:
    keys: set = set()
    values: list = []
    _keys_and_values(payload, keys, values)
    return keys, values


async def _table_counts(stack) -> dict:
    async with stack.sessionmaker() as session:
        return {
            table.name: (await session.execute(
                sa.select(sa.func.count()).select_from(table))).scalar_one()
            for table in Base.metadata.sorted_tables
        }


async def _reads(stack, agent_id) -> int:
    async with stack.sessionmaker() as session:
        return int((await session.execute(
            sa.select(sa.func.coalesce(sa.func.sum(CCAgentReadWindow.reads), 0))
            .where(CCAgentReadWindow.agent_id == agent_id)
        )).scalar_one())


# ---------------------------------------------------------------------------
# 1. The plane: D1, D6, B0c
# ---------------------------------------------------------------------------


class TestThePlane:
    def test_exactly_the_ratified_route(self):
        assert ROUTE_CONTRACT[ROUTE] == ("fleet.view", READ_SCOPED, False)
        assert MACHINE_SURFACE[ROUTE] == (SURFACE_MACHINE, JOB_AUTONOMY)
        assert ROUTE in MACHINE_ONLY_ROUTES

    def test_the_job_is_the_existing_autonomy_binding(self):
        """F5 closed: the mandatory binding now names a real route."""
        assert JOB_AUTONOMY == "autonomy"
        assert JOB_AUTONOMY in READ_CAPABILITIES
        assert JOB_AUTONOMY in REQUIRED_READS
        assert JOB_AUTONOMY not in JOBS_WITHOUT_A_BINDING
        assert READ_BINDING_PERMISSIONS[JOB_AUTONOMY] == frozenset({"fleet.view"})
        declared = [r for r, (_s, j) in MACHINE_SURFACE.items() if j == JOB_AUTONOMY]
        assert declared == [ROUTE]

    def test_the_builder_catalogue_names_the_route_it_reaches(self):
        text = READ_CAPABILITIES[JOB_AUTONOMY]
        assert "/discovery" in text
        assert "/api/autonomy)" not in text

    def test_metered_by_the_existing_read_meter(self):
        from harkeniq_cc.metrics import MACHINE_READ_METER_LABELS

        assert JOB_METER[JOB_AUTONOMY] == METER_READ
        assert machine_meter(*ROUTE) == METER_READ
        # The per-job counter label is DERIVED from the declaration (A30.31):
        # nobody had to remember to add it.
        assert JOB_AUTONOMY in MACHINE_READ_METER_LABELS

    def test_the_counts_moved_by_exactly_one_route(self):
        """Verified, not forced (A30.32 D1)."""
        assert len(ROUTE_CONTRACT) == 99
        assert len(MACHINE_SURFACE) == 14
        assert len(MACHINE_ONLY_ROUTES) == 4
        assert len(MACHINE_JOBS) == 5
        assert MACHINE_JOBS == frozenset(
            {"self", "attention", "incidents", "proposals", "autonomy"})

    def test_no_ceiling_and_no_permission_moved(self):
        assert MACHINE_PRINCIPAL_CEILING == frozenset(
            {"fleet.view", "incident.view", "proposal.submit"})
        assert ROUTE_CONTRACT[ROUTE][0] in MACHINE_PRINCIPAL_CEILING

    async def test_the_census_is_clean(self):
        stack = await _estate(("A",))
        assert meter_census(stack.app) == []

    def test_the_handler_meters_nothing_and_asks_the_self_rule_first(self):
        from harkeniq_cc.api import operational_agents as oa

        fn = ast.parse(textwrap.dedent(inspect.getsource(oa.agent_discovery))).body[0]
        line = {}
        for n in ast.walk(fn):
            if isinstance(n, ast.Call):
                name = getattr(n.func, "id", getattr(n.func, "attr", ""))
                line.setdefault(name, n.lineno)
        assert not {"meter_machine_read", "_charge_machine_read", "admit_read"} & set(line)
        # The self rule before the agent is loaded: no existence oracle.
        assert line["_machine_read_gate"] < line["_require_agent"]
        # Loaded by identity, never by the scope-visibility helper (A30.20).
        assert "_require_visible_agent" not in line


# ---------------------------------------------------------------------------
# 2. Human refusal, self-only, metering, throttle
# ---------------------------------------------------------------------------


class TestMachineOnlyAndSelfOnly:
    async def test_a_person_is_refused_and_never_charged(self):
        """D6. The route surface refuses before any handler line."""
        stack = await _estate(("A",))
        await _persona(stack, "site_a")
        stack.as_person()
        async with stack.client() as c:
            res = await c.get(f"{PREFIX}/b1-site_a/discovery")
        assert res.status_code == 403
        assert "machine-principal surface" in res.text
        async with stack.sessionmaker() as session:
            owner_rows = (await session.execute(
                sa.select(sa.func.count()).select_from(CCAgentReadWindow)
                .where(CCAgentReadWindow.agent_id == E.OWNER))).scalar_one()
        assert owner_rows == 0

    async def test_another_agent_is_refused_and_the_caller_is_charged(self):
        stack = await _estate(("A",))
        await _persona(stack, "site_a")
        await _agent(stack, "b1-other")
        await _grant(stack, "b1-other", "site", "A")
        before_caller = await _reads(stack, "b1-site_a")
        before_target = await _reads(stack, "b1-other")
        res = await _read(stack, "b1-other", as_agent="b1-site_a")
        assert res.status_code == 403
        assert "its own agent and no other" in res.text
        assert await _reads(stack, "b1-site_a") == before_caller + 1
        assert await _reads(stack, "b1-other") == before_target

    async def test_a_nonexistent_id_is_refused_exactly_like_another_agents(self):
        """Self rule FIRST: no 404-versus-403 existence oracle."""
        stack = await _estate(("A",))
        await _persona(stack, "site_a")
        before = await _reads(stack, "b1-site_a")
        res = await _read(stack, "no-such-agent-000", as_agent="b1-site_a")
        assert res.status_code == 403
        assert await _reads(stack, "b1-site_a") == before + 1

    async def test_an_agent_without_the_autonomy_binding_is_refused(self):
        """The BINDING decides (A29.6), and the refusal is charged."""
        stack = await _estate(("A",))
        await _agent(stack, "b1-noauto", reads=("attention",))
        await _grant(stack, "b1-noauto", "site", "A")
        from harkeniq_cc.api.deps import get_current_user
        from harkeniq_cc.auth import UserContext

        async def _user():
            return UserContext(
                user_id="b1-noauto", email="op-agent:b1-noauto@v1",
                tenant_id=stack.tenant, role="",
                permissions=["fleet.view", "incident.view"], species="agent",
                identity_id="id-noauto", machine_jobs=frozenset({"self", "attention"}),
            )

        stack.app.dependency_overrides[get_current_user] = _user
        async with stack.client() as c:
            res = await c.get(f"{PREFIX}/b1-noauto/discovery")
        assert res.status_code == 403
        assert "autonomy" in res.text and "binding" in res.text
        assert await _reads(stack, "b1-noauto") == 1

    async def test_a_served_read_costs_exactly_one(self):
        """The guard is declared twice; the meter charges once (A30.31)."""
        stack = await _estate(("A",))
        await _persona(stack, "site_a")
        before = await _reads(stack, "b1-site_a")
        for _ in range(3):
            assert (await _read(stack, "b1-site_a")).status_code == 200
        assert await _reads(stack, "b1-site_a") == before + 3

    async def test_it_is_throttled_by_the_one_shared_window(self, monkeypatch):
        from harkeniq_cc import provenance

        window = read_window_start(datetime.now(timezone.utc)) + timedelta(days=1)
        for module in (ingress_limits, provenance):
            monkeypatch.setattr(module, "read_window_start", lambda now=None: window)
        stack = await _estate(("A",))
        await _persona(stack, "site_a")
        async with stack.sessionmaker() as session:
            # The persona's own first read already opened this window.
            row = (await session.execute(sa.select(CCAgentReadWindow).where(
                CCAgentReadWindow.agent_id == "b1-site_a",
                CCAgentReadWindow.window_start == window,
            ))).scalar_one()
            row.reads = READ_MAX_PER_WINDOW - 1
            await session.commit()
        assert (await _read(stack, "b1-site_a")).status_code == 200
        over = await _read(stack, "b1-site_a")
        assert over.status_code == 429
        # The allowance is SHARED: discovery spent it for attention too.
        async with stack.as_machine("b1-site_a").client() as c:
            assert (await c.get("/api/attention/")).status_code == 429

    async def test_served_no_store_and_never_cacheable(self):
        stack = await _estate(("A",))
        await _persona(stack, "site_a")
        res = await _read(stack, "b1-site_a")
        assert res.headers["cache-control"] == "no-store"
        assert "etag" not in {k.lower() for k in res.headers}


# ---------------------------------------------------------------------------
# 3. Self-scope: native reach, never construction (A29.9, A30.5)
# ---------------------------------------------------------------------------

#: What A29.9 withholds from a machine's view of its own scope.
CONSTRUCTION = {
    "rules", "grants", "scope_type", "scope_ref", "permission_subset",
    "org_unit_paths", "unit_paths", "site_unit_paths", "contextual_unit_ids",
    "inert", "inert_reason", "inert_grants", "previously_granted",
    "synthesis", "administered", "role", "granted_by", "expires_at",
    "revoked_at",
}


class TestSelfScope:
    async def test_tenant_wide(self):
        stack = await _estate()
        body = await _persona(stack, "tenant")
        reach = body["scope"]["reach"]
        assert reach["tenant_wide"] is True
        assert reach["site_ids"] == sorted(stack.site(k) for k in ALL)
        assert set(reach["org_unit_ids"]) >= set(stack.regions.values())
        assert reach["device_ids"] == [] and reach["device_classes"] == []
        assert body["scope"]["devices_in_reach"] == 4
        assert body["governance_basis"]["discovery_composed_over"] == "tenant"
        assert body["governance_basis"]["matches_admission"] is True

    async def test_tenant_wide_through_the_production_grant_route(self):
        """The shape the live gate uses: an agent created with no scope,
        then granted the tenant by an owner through `POST /api/scope-grants/`."""
        stack = await _estate(("A",))
        await _agent(stack, "b1-granted")
        stack.as_person()
        async with stack.client() as c:
            res = await c.post("/api/scope-grants/", json={
                "principal_type": "agent", "principal_ref": "b1-granted",
                "scope_type": "tenant", "scope_ref": "",
            })
        assert res.status_code == 201, res.text
        body = await _discover(stack, "b1-granted")
        assert body["scope"]["reach"]["tenant_wide"] is True
        assert body["governance_basis"]["matches_admission"] is True

    async def test_org_unit(self):
        stack = await _estate()
        body = await _persona(stack, "org_ab")
        reach = body["scope"]["reach"]
        assert reach["tenant_wide"] is False
        # The unit it holds, never its ancestor (context is not authority).
        assert reach["org_unit_ids"] == [stack.regions["ab"]]
        assert reach["site_ids"] == sorted([stack.site("A"), stack.site("B")])
        assert body["scope"]["devices_in_reach"] == 2
        assert body["governance_basis"]["sites_in_composition"] == 2

    async def test_site(self):
        stack = await _estate()
        body = await _persona(stack, "site_a")
        reach = body["scope"]["reach"]
        assert reach == {
            "tenant_wide": False, "org_unit_ids": [],
            "site_ids": [stack.site("A")], "device_ids": [], "device_classes": [],
        }
        assert body["scope"]["devices_in_reach"] == 1
        assert body["governance_basis"]["discovery_composed_over"] == "authorized_sites"
        assert body["governance_basis"]["matches_admission"] is False

    async def test_device_is_native_and_synthesizes_no_containing_site(self):
        """B0b preserved: a device grant reaches its device BY IDENTITY.
        Its site is not listed, not composed over, and confers nothing."""
        stack = await _estate()
        body = await _persona(stack, "device_c")
        reach = body["scope"]["reach"]
        assert reach["device_ids"] == [stack.device("C")]
        assert reach["site_ids"] == [] and reach["org_unit_ids"] == []
        assert body["scope"]["devices_in_reach"] == 1
        # Composed over ZERO sites: its own site's safety is not its to read.
        assert body["governance_basis"]["sites_in_composition"] == 0
        assert body["governance_basis"]["safety_reported"] is False
        _keys, values = walk(body)
        text = json.dumps(values)
        for marker in ("s3-site-c", "s3-charlie", "SECRET-C", "SECRET-REASON-C",
                       "SECRET-SIGNAL-C"):
            assert marker not in text, marker
        # The reach its devices are counted over is the device, and only it.
        assert klass(body, "SEL_CLEAR")["in_effective_scope"]["devices_in_scope"] == 1

    async def test_device_class_is_native(self):
        stack = await _estate()
        body = await _persona(stack, "class_switch")
        reach = body["scope"]["reach"]
        assert reach["device_classes"] == ["switch"]
        assert reach["site_ids"] == [] and reach["device_ids"] == []
        assert body["scope"]["devices_in_reach"] == 1
        # The switch's protocol is gNMI: interface work is in reach there.
        assert klass(body, "INTERFACE_DISABLE")["in_effective_scope"]["state"] == "available"
        assert klass(body, "SEL_CLEAR")["in_effective_scope"]["state"] == "no_effective_reach"
        assert "s3-site-b" not in json.dumps(body)

    async def test_empty_reach(self):
        stack = await _estate()
        body = await _persona(stack, "empty")
        assert body["scope"]["empty"] is True
        assert body["scope"]["reach"] == {
            "tenant_wide": False, "org_unit_ids": [], "site_ids": [],
            "device_ids": [], "device_classes": [],
        }
        assert body["scope"]["devices_in_reach"] == 0
        row = klass(body, "SEL_CLEAR")
        assert row["in_effective_scope"]["state"] == "no_devices_in_scope"
        assert row["currently_operable"]["state"] == "not_operable"
        assert "no_devices_in_scope" in row["currently_operable"]["blocked_by"]

    @pytest.mark.parametrize("how", ["expired", "revoked"])
    async def test_lapsed_grants_answer_200_with_nothing_covered(self, how):
        """A30.20's alignment: the self rule answers, with empty reach --
        never the lapsed grant's reach, and never a 404 for its own record."""
        stack = await _estate()
        await _agent(stack, "b1-lapsed")
        grant_id = await _grant(stack, "b1-lapsed", "site", "A")
        live = await _discover(stack, "b1-lapsed")
        assert live["scope"]["reach"]["site_ids"] == [stack.site("A")]
        await stack.lapse(grant_id, how=how)
        body = await _discover(stack, "b1-lapsed")
        assert body["scope"]["empty"] is True
        assert body["scope"]["reach"]["site_ids"] == []
        assert body["scope"]["devices_in_reach"] == 0
        assert "s3-site-a" not in json.dumps(body)
        # The machine agent view still refuses a lapsed agent its record --
        # recorded in A30.20 and deliberately unchanged here.
        async with stack.as_machine("b1-lapsed").client() as c:
            assert (await c.get(f"{PREFIX}/b1-lapsed")).status_code == 404

    async def test_readable_reach_under_reports_and_never_over_reports(self):
        """G6, recorded and not fixed here: an agent grant narrowed away from
        `fleet.view` still widens its WHERE-only operational reach. Discovery
        is measured over READABLE reach (D2), so it reports LESS than the
        evaluator may act on there -- never more."""
        stack = await _estate(("A", "B"))
        await _agent(stack, "b1-g6")
        await _grant(stack, "b1-g6", "site", "A")
        await stack.grant("b1-g6", "site", stack.site("B"), role="",
                          subset=["incident.view"], principal_type="agent")
        body = await _discover(stack, "b1-g6")
        assert body["scope"]["reach"]["site_ids"] == [stack.site("A")]
        assert body["scope"]["devices_in_reach"] == 1
        assert stack.site("B") not in json.dumps(body)

    async def test_no_grant_construction_reaches_the_machine(self):
        stack = await _estate()
        for name in ("tenant", "org_ab", "site_a", "device_c", "class_switch"):
            body = await _persona(stack, name)
            keys, _values = walk(body)
            assert not keys & CONSTRUCTION, (name, keys & CONSTRUCTION)


# ---------------------------------------------------------------------------
# 4. The eight facts, each from its own source
# ---------------------------------------------------------------------------


class TestTheEightFactsStaySeparate:
    async def test_every_governed_class_is_described_once(self):
        stack = await _estate(("A",))
        body = await _persona(stack, "site_a")
        names = [r["action_type"] for r in body["action_classes"]]
        assert names == sorted(a.value for a in ActionType)
        for row in body["action_classes"]:
            assert set(row) == {
                "action_type", "exists", "risk", "reversibility",
                "inverse_action", "implemented", "addressable", "bound",
                "in_effective_scope", "parameters", "governance",
                "approval_required", "currently_operable",
            }

    async def test_exists_and_implemented_come_from_the_platform(self):
        stack = await _estate(("A",))
        body = await _persona(stack, "site_a")
        facts = action_facts()
        for row in body["action_classes"]:
            fact = facts[row["action_type"]]
            assert row["exists"] is True
            assert row["implemented"] == {
                "value": fact["implemented"], "by": fact["implemented_by"]}
            assert row["risk"] == fact["risk"]
            assert row["reversibility"] == fact["reversibility"]
        assert klass(body, "INTERFACE_RESET")["implemented"]["value"] is False
        assert klass(body, "CLEAR_COUNTERS")["implemented"]["value"] is False

    async def test_a_bound_class_the_platform_does_not_govern(self):
        """exists=false is a real answer: a stale binding row."""
        stack = await _estate(("A",))
        await _agent(stack, "b1-stale", ("SEL_CLEAR", "RETIRED_CLASS"))
        await _grant(stack, "b1-stale", "site", "A")
        row = klass(await _discover(stack, "b1-stale"), "RETIRED_CLASS")
        assert row["exists"] is False and row["bound"] is True
        assert row["implemented"] == {"value": False, "by": []}
        assert row["addressable"]["value"] is False
        assert row["governance"] is None
        assert row["approval_required"] == {
            "state": "not_applicable", "basis": ["not_governed"]}
        assert row["currently_operable"]["state"] == "not_operable"
        assert "not_governed" in row["currently_operable"]["blocked_by"]
        assert row["parameters"]["resolvable"] is False

    async def test_addressable_means_the_catalogue_or_campaigns_and_nothing_else(self):
        """D2: never `reachable`, never in-scope, never permitted."""
        stack = await _estate(("A",))
        body = await _persona(stack, "site_a")
        keys, _ = walk(body)
        assert "reachable" not in keys
        sel = klass(body, "SEL_CLEAR")["addressable"]
        assert sel == {"value": True, "path": "condition_catalogue", "conditions": ["log"]}
        for name in ("FIRMWARE_UPDATE", "FIRMWARE_ROLLBACK"):
            assert klass(body, name)["addressable"] == {
                "value": True, "path": "campaign_only", "conditions": []}
        for name in ("CLEAR_COUNTERS", "INTERFACE_RESET"):
            assert klass(body, name)["addressable"]["path"] == "none"
        # Addressable and in-scope are different facts: INTERFACE_DISABLE is
        # addressable (the interface subsystem) and reaches no server.
        disable = klass(body, "INTERFACE_DISABLE")
        assert disable["addressable"]["value"] is True
        assert disable["in_effective_scope"]["state"] == "no_effective_reach"

    async def test_a_disabled_catalogue_row_is_not_addressable(self):
        stack = await _estate(("A",))
        await _persona(stack, "site_a")          # seeds nothing: discovery rolls back
        async with stack.sessionmaker() as session:
            from harkeniq_cc.db.repos import CapabilityCatalogueRepo

            rows = await CapabilityCatalogueRepo(session).list_for_tenant(stack.tenant)
            for row in rows:
                if row.action_type == "SEL_CLEAR":
                    row.enabled = False
            await session.commit()
        row = klass(await _discover(stack, "b1-site_a"), "SEL_CLEAR")
        assert row["addressable"] == {"value": False, "path": "none", "conditions": []}
        assert "not_addressable" in row["currently_operable"]["blocked_by"]

    async def test_bound_is_the_bindings(self):
        stack = await _estate(("A",))
        body = await _persona(stack, "site_a", ("SEL_CLEAR", "IDENTIFY_LED"))
        bound = sorted(r["action_type"] for r in body["action_classes"] if r["bound"])
        assert bound == ["IDENTIFY_LED", "SEL_CLEAR"]

    async def test_an_unbound_class(self):
        stack = await _estate(("A",))
        row = klass(await _persona(stack, "site_a"), "BMC_RESET")
        assert row["bound"] is False
        assert row["governance"] is None
        assert row["approval_required"] == {
            "state": "not_applicable", "basis": ["not_bound"]}
        assert row["currently_operable"]["state"] == "not_operable"
        assert "not_bound" in row["currently_operable"]["blocked_by"]
        # Its other facts are still answered: they are not about binding.
        assert row["implemented"]["value"] is True
        assert row["in_effective_scope"]["state"] == "available"

    async def test_an_unimplemented_class_bound_before_a17(self):
        stack = await _estate(("A",))
        await _agent(stack, "b1-pre-a17", ("INTERFACE_RESET",))
        await _grant(stack, "b1-pre-a17", "site", "A")
        row = klass(await _discover(stack, "b1-pre-a17"), "INTERFACE_RESET")
        assert row["implemented"] == {"value": False, "by": []}
        assert row["in_effective_scope"]["state"] == "unimplemented"
        assert row["governance"]["conclusion"] == "denied"
        blocked = row["currently_operable"]["blocked_by"]
        assert "not_implemented" in blocked and "not_addressable" in blocked

    async def test_a_campaign_only_class(self):
        stack = await _estate(("C",))
        row = klass(await _persona(stack, "site_c", ("FIRMWARE_UPDATE",)),
                    "FIRMWARE_UPDATE")
        assert row["addressable"]["path"] == "campaign_only"
        assert row["in_effective_scope"]["state"] == "available"
        assert row["governance"]["conclusion"] == "denied"
        assert row["governance"]["never_budget_grantable"] is True
        assert row["approval_required"] == {
            "state": "not_applicable", "basis": ["governance_denied"]}
        assert row["currently_operable"] == {
            "state": "not_operable",
            "blocked_by": ["campaign_only", "parameters_unavailable",
                           "governance_denied"],
            "unknown": [],
        }

    async def test_a_node_that_does_not_permit_the_class(self):
        """A17.7: policy is REPORTED as a blocker; it never refused a binding."""
        stack = await _estate(("A",))
        row = klass(await _persona(stack, "site_a", ("POWER_CYCLE",)), "POWER_CYCLE")
        scope = row["in_effective_scope"]
        assert scope == {
            "state": "not_permitted_on_any_node", "devices_in_scope": 1,
            "implementing_devices": 1, "permitting_devices": 0,
            "undeclared_devices": 0,
        }
        assert row["currently_operable"]["blocked_by"] == ["not_permitted_on_any_node"]
        assert row["approval_required"]["state"] == "required"

    async def test_unresolved_executor_reach_is_unknown_never_zero(self):
        stack = await _estate(("D",))
        row = klass(await _persona(stack, "site_d", ("IDENTIFY_LED",)), "IDENTIFY_LED")
        assert row["in_effective_scope"]["state"] == "unknown"
        assert row["in_effective_scope"]["undeclared_devices"] == 1
        assert row["currently_operable"] == {
            "state": "unknown", "blocked_by": [],
            "unknown": ["executor_reach_undeclared"],
        }


# ---------------------------------------------------------------------------
# 5. Parameters (D4a)
# ---------------------------------------------------------------------------


class TestParameters:
    async def test_resolvable_parameters_are_the_canonical_contract(self):
        stack = await _estate(("A",))
        body = await _persona(stack, "site_a", ("IDENTIFY_LED", "SEL_CLEAR"))
        led = klass(body, "IDENTIFY_LED")["parameters"]
        assert led["required"] == ["target"] and led["resolvable"] is True
        assert led["unresolvable_reason"] == ""
        contract = parameter_contract("IDENTIFY_LED")
        assert [i["name"] for i in led["items"]] == [p["name"] for p in contract["parameters"]]
        target = led["items"][0]
        assert target == {
            "name": "target", "type": "string", "required": True,
            "source": "component",
            "constraint": "a drive identifier the device's protocol exposes",
            "missing_input": "",
        }
        assert klass(body, "SEL_CLEAR")["parameters"]["required"] == []

    async def test_the_unresolvable_firmware_parameter_is_named(self):
        stack = await _estate(("C",))
        fw = klass(await _persona(stack, "site_c", ("FIRMWARE_UPDATE",)),
                   "FIRMWARE_UPDATE")["parameters"]
        assert fw["resolvable"] is False
        assert "target_version" in fw["unresolvable_reason"]
        assert "campaign orchestration" in fw["unresolvable_reason"]

    async def test_no_default_no_enum_no_range_is_published(self):
        """Executor defaults are server-side; nothing is invented (G8)."""
        stack = await _estate(("A",))
        body = await _persona(stack, "site_a")
        for row in body["action_classes"]:
            for item in row["parameters"]["items"]:
                assert set(item) == {
                    "name", "type", "required", "source", "constraint",
                    "missing_input",
                }
        _keys, values = walk(body)
        assert "ForceRestart" not in values        # POWER_CYCLE's default
        keys, _ = walk(body)
        assert not keys & {"default", "enum", "minimum", "maximum", "range", "choices"}

    def test_the_contract_agrees_with_the_resolver_for_every_class(self):
        """D4a as an equivalence, over all fourteen and an unknown name."""
        for name in [a.value for a in ActionType] + ["NOT_A_CLASS"]:
            contract = parameter_contract(name)
            params, why = resolve_action_params(name, component="comp-1", reason="r")
            assert contract["agent_resolvable"] == (params is not None), (name, why)
            assert bool(contract["unsatisfiable_reason"]) == (params is None), name
        assert parameter_contract("FIRMWARE_UPDATE")["agent_resolvable"] is False
        assert parameter_contract("FIRMWARE_ROLLBACK")["agent_resolvable"] is True


# ---------------------------------------------------------------------------
# 6. Governance, approval and operability (D3)
# ---------------------------------------------------------------------------


async def _clear_safety(stack, keys):
    """No suppression, no drop-back, budgets unspent: a clean estate."""
    async with stack.sessionmaker() as session:
        for key in keys:
            row = await session.get(CCSafetyState, stack.site(key))
            if row is None:
                continue
            row.suppressions = []
            row.error_budgets = []
            row.site_budgets = {"SEL_CLEAR": 5, "BMC_RESET": 5}
            row.sm_stop_switch = False
        await session.commit()


class TestGovernance:
    async def test_autonomous_and_definitively_not_required(self):
        """A tenant-wide agent reads what admission reads -- so the one
        definitive `not_required` the table allows."""
        stack = await _estate(("A",))
        await _clear_safety(stack, ("A",))
        row = klass(await _persona(stack, "tenant"), "SEL_CLEAR")
        assert row["governance"]["conclusion"] == "autonomous"
        assert row["governance"]["granted_at_level"] == 2
        assert row["governance"]["budget_mapped"] is True
        assert row["approval_required"] == {
            "state": "not_required", "basis": ["governance_autonomous"]}
        assert row["currently_operable"] == {
            "state": "operable", "blocked_by": [], "unknown": []}

    async def test_requires_approval(self):
        stack = await _estate(("A",))
        row = klass(await _persona(stack, "tenant", ("IDENTIFY_LED",)), "IDENTIFY_LED")
        assert row["governance"]["conclusion"] == "requires_approval"
        assert {"code": "not_budget_mapped", "scope": "tenant"} in row["governance"]["reason_codes"]
        assert row["approval_required"] == {
            "state": "required", "basis": ["governance_requires_approval"]}
        assert row["currently_operable"]["state"] == "operable"

    async def test_denied_by_risk(self):
        stack = await _estate(("B",))
        row = klass(await _persona(stack, "site_b", ("INTERFACE_DISABLE",)),
                    "INTERFACE_DISABLE")
        assert row["governance"]["conclusion"] == "denied"
        assert {"code": "never_budget_grantable", "scope": "tenant"} in row["governance"]["reason_codes"]
        assert row["approval_required"] == {
            "state": "not_applicable", "basis": ["governance_denied"]}
        assert row["currently_operable"]["blocked_by"] == ["governance_denied"]

    async def test_denied_by_the_tenant_stop_switch_names_no_operator(self):
        stack = await _estate(("A",))
        async with stack.sessionmaker() as session:
            await StopSwitchRepo(session).set(stack.tenant, True, changed_by=OPERATOR)
            await session.commit()
        body = await _persona(stack, "tenant", ("SEL_CLEAR", "IDENTIFY_LED"))
        assert body["governance_basis"]["tenant_stop_switch"] is True
        for name in ("SEL_CLEAR", "IDENTIFY_LED"):
            row = klass(body, name)
            assert row["governance"]["conclusion"] == "denied", name
            assert {"code": "stop_switch_active", "scope": "tenant"} in row["governance"]["reason_codes"]
            assert row["approval_required"]["state"] == "not_applicable"
            assert "governance_denied" in row["currently_operable"]["blocked_by"]
        keys, values = walk(body)
        assert "changed_by" not in keys
        assert OPERATOR not in json.dumps(values)

    async def test_the_agent_ceiling_and_always_approve_only_tighten(self):
        stack = await _estate(("A",))
        await _clear_safety(stack, ("A",))
        row = klass(await _persona(stack, "tenant", ceiling=1), "SEL_CLEAR")
        assert row["governance"]["conclusion"] == "requires_approval"
        assert {"code": "agent_ceiling_below_grant", "scope": "tenant"} in row["governance"]["reason_codes"]
        assert row["approval_required"]["state"] == "required"

    async def test_non_tenant_wide_autonomous_is_unknown_before_e1(self):
        """D3's amendment. Composed over site A alone SEL_CLEAR is
        autonomous -- and admission, which folds the WHOLE tenant, sees site
        C's drop-back and sends it to a human. So discovery may not say
        `not_required`, and may not say `operable`."""
        stack = await _estate()
        row = klass(await _persona(stack, "site_a"), "SEL_CLEAR")
        assert row["governance"]["conclusion"] == "autonomous"
        assert row["approval_required"]["state"] == "unknown"
        assert "admission_beyond_reach" in row["approval_required"]["basis"]
        assert row["currently_operable"] == {
            "state": "unknown", "blocked_by": [],
            "unknown": ["admission_beyond_reach"],
        }
        # ...and real admission proves `unknown` was the honest answer: the
        # dry-run reasons exactly as the runtime does (A22.6).
        await E.seed_incident(stack, "A")
        async with stack.as_machine("b1-site_a").client() as c:
            dry = (await c.get(f"{PREFIX}/b1-site_a/dry-run")).json()
        would = [p for p in dry["would_propose"] if p["action_type"] == "SEL_CLEAR"]
        assert would and would[0]["disposition"] == "requires_approval"
        assert would[0]["requires_human"] is True

    async def test_non_tenant_wide_requires_approval_is_definitive(self):
        """Hidden state can only ADD a restriction, so `required` and
        `operable` stay definitive for a class that already needs a human."""
        stack = await _estate()
        row = klass(await _persona(stack, "site_a", ("IDENTIFY_LED",)), "IDENTIFY_LED")
        assert row["approval_required"] == {
            "state": "required", "basis": ["governance_requires_approval"]}
        assert row["currently_operable"] == {
            "state": "operable", "blocked_by": [], "unknown": []}

    async def test_a_suppressed_in_reach_site_makes_approval_target_dependent(self):
        """`govern_proposal` sends a target at a suppressed site to a human,
        so a class-level `not_required` would over-promise there. It still
        progresses either way, so operability is not what is unknown."""
        stack = await _estate(("A",))
        await _clear_safety(stack, ())          # keep fault-A suppressed at A
        async with stack.sessionmaker() as session:
            row = await session.get(CCSafetyState, stack.site("A"))
            row.error_budgets = []
            await session.commit()
        cls = klass(await _persona(stack, "tenant"), "SEL_CLEAR")
        assert cls["governance"]["conclusion"] == "autonomous"
        assert {"code": "domain_suppressed", "scope": "domain",
                "site_id": stack.site("A")} in cls["governance"]["reason_codes"]
        assert cls["approval_required"] == {
            "state": "unknown", "basis": ["depends_on_target_site"]}
        assert cls["currently_operable"]["state"] == "operable"

    async def test_a_spent_execution_budget_returns_work_to_a_human(self):
        """A19 D2: autonomous work is withheld to a human at dispatch."""
        stack = await _estate(("A",))
        await _clear_safety(stack, ("A",))
        await _agent(stack, "b1-budget", budget=1)
        await _grant(stack, "b1-budget", "tenant")
        async with stack.sessionmaker() as session:
            session.add(CCOutcomeHistory(
                site_id=stack.site("A"), action_id="b1-exec-1",
                action_type="SEL_CLEAR", device_agent_id=stack.device("A"),
                vendor="Dell", model="R750", outcome="SUCCESS",
                actor="op-agent:b1-budget@v1",
                recorded_at=datetime.now(timezone.utc),
            ))
            await session.commit()
        body = await _discover(stack, "b1-budget")
        assert body["agent"]["execution_budget"] == {
            "limit": 1, "used": 1, "remaining": 0, "period": "daily",
            "exhausted": True,
        }
        row = klass(body, "SEL_CLEAR")
        assert row["governance"]["conclusion"] == "autonomous"
        assert row["approval_required"] == {
            "state": "required", "basis": ["execution_budget_exhausted"]}
        assert row["currently_operable"]["state"] == "operable"

    @pytest.mark.parametrize("state,blocker", [
        ({"status": "draft"}, "agent_not_active"),
        ({"paused": "operator paused it"}, "agent_paused"),
        ({"per_day": 0}, "proposal_budget_exhausted"),
    ])
    async def test_agent_level_blockers(self, state, blocker):
        stack = await _estate(("A",))
        row = klass(await _persona(stack, "site_a", **state), "SEL_CLEAR")
        assert blocker in row["currently_operable"]["blocked_by"]
        assert row["currently_operable"]["state"] == "not_operable"

    async def test_reason_codes_carry_no_text_no_domain_and_no_hidden_site(self):
        stack = await _estate()
        body = await _persona(stack, "tenant", ("SEL_CLEAR", "BMC_RESET"))
        for name in ("SEL_CLEAR", "BMC_RESET"):
            for code in klass(body, name)["governance"]["reason_codes"]:
                assert set(code) <= {"code", "scope", "site_id"}
                assert code["code"] in D.GOVERNANCE_REASON_CODES
        text = json.dumps(body)
        for marker in ("fault-A", "reason-A", "SECRET-C", "SECRET-REASON-C"):
            assert marker not in text, marker


class TestTheClosedVocabularies:
    async def test_every_state_and_code_is_declared(self):
        stack = await _estate()
        for name in ("tenant", "site_a", "device_c", "empty"):
            body = await _persona(stack, name, ("SEL_CLEAR", "IDENTIFY_LED",
                                                "FIRMWARE_UPDATE", "POWER_CYCLE"))
            for row in body["action_classes"]:
                approval = row["approval_required"]
                assert approval["state"] in D.APPROVAL_STATES
                assert set(approval["basis"]) <= D.APPROVAL_BASES
                operable = row["currently_operable"]
                assert operable["state"] in D.OPERABILITY_STATES
                assert set(operable["blocked_by"]) <= set(D.BLOCKERS)
                assert set(operable["unknown"]) <= set(D.UNKNOWNS)
                if row["governance"] is not None:
                    assert row["governance"]["conclusion"] in {
                        "autonomous", "requires_approval", "denied"}

    def test_the_reason_code_allow_list_is_exactly_what_the_contract_emits(self):
        """No code invented, none missed: every `"code": <literal>` the
        contract and `effective_disposition` write is on the allow-list,
        except `govern_proposal`'s per-TARGET `site_suppressed`."""
        root = pathlib.Path(harkeniq_cc.__file__).parent
        emitted: set[str] = set()
        for module in ("autonomy.py", "operational_agent.py"):
            for node in ast.walk(ast.parse((root / module).read_text())):
                if not isinstance(node, ast.Dict):
                    continue
                keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
                if not {"code", "scope"} <= keys:
                    continue  # a blocking condition carries both
                for k, v in zip(node.keys, node.values):
                    if isinstance(k, ast.Constant) and k.value == "code":
                        assert isinstance(v, ast.Constant), (module, node.lineno)
                        emitted.add(v.value)
        assert emitted - {"site_suppressed"} == D.GOVERNANCE_REASON_CODES
        assert "GLOBAL_SAFETY_CONSTRAINT" not in D.GOVERNANCE_REASON_CODES

    def test_an_unrecognised_code_is_reported_as_other_never_passed_through(self):
        codes = D._reason_codes([
            {"code": "free text a person wrote", "scope": "tenant"},
            {"code": "level_below_grant", "scope": "tenant", "detail": "prose"},
        ])
        assert codes == [
            {"code": "level_below_grant", "scope": "tenant"},
            {"code": "other", "scope": "tenant"},
        ]


# ---------------------------------------------------------------------------
# 7. No authority, and nothing accepted back
# ---------------------------------------------------------------------------

AUTHORITY_KEY = re.compile(
    r"^(allowed|authori[sz]ed|authorization|can_.*|may_.*|safe_to_.*|"
    r"executable|permitted|is_allowed|is_authori[sz]ed|execution_authori[sz]ed)$"
)


class TestNoAuthorityToken:
    async def test_no_authority_boolean_anywhere(self):
        stack = await _estate()
        for name in ("tenant", "site_a", "device_c"):
            keys, _ = walk(await _persona(stack, name))
            assert not [k for k in keys if AUTHORITY_KEY.match(k)], name

    async def test_nothing_that_could_be_presented_back(self):
        stack = await _estate(("A",))
        keys, _ = walk(await _persona(stack, "site_a"))
        assert not keys & {
            "candidate_ref", "digest", "signature", "etag", "token",
            "subject_ref", "dedupe_key", "operation_key", "proof",
        }

    async def test_the_submit_model_refuses_every_discovery_field(self):
        """A24.2's `extra="forbid"`, against the whole discovery vocabulary:
        422, and nothing is written."""
        stack = await _estate(("A",))
        body = await _persona(stack, "site_a", ("SEL_CLEAR",))
        vocabulary = set(body) | set(body["action_classes"][0]) \
            | set(body["action_classes"][0]["currently_operable"]) \
            | set(body["governance_basis"]) | set(body["scope"])
        from harkeniq_cc.api.operational_agents import SubmitProposal

        # Disjoint by construction: no discovery field is a submit field.
        assert not vocabulary & set(SubmitProposal.model_fields)
        before = await _table_counts(stack)
        async with stack.as_machine("b1-site_a").client() as c:
            # The model forbids every field NAME; a placeholder value keeps
            # the body small so the name, not the size, is what is refused.
            for field in sorted(vocabulary):
                res = await c.post(f"{PREFIX}/b1-site_a/proposals", json={
                    "candidate_ref": "cand_0000000000000000",
                    "idempotency_key": f"b1-{field}"[:120].ljust(8, "x"),
                    field: True,
                })
                assert res.status_code == 422, (field, res.status_code)
            # And the whole response, replayed: refused at the 16 KiB
            # pre-parse ceiling when that large (A24), else by the model.
            whole = dict(body, candidate_ref="cand_0000000000000000",
                         idempotency_key="b1-whole-response")
            res = await c.post(f"{PREFIX}/b1-site_a/proposals", json=whole)
            assert res.status_code in (413, 422), res.status_code
        after = await _table_counts(stack)
        assert before["cc_agent_proposals"] == after["cc_agent_proposals"]
        assert before["cc_agent_submissions"] == after["cc_agent_submissions"]

    def test_only_the_router_composes_discovery(self):
        root = pathlib.Path(harkeniq_cc.__file__).parent
        importers = set()
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "harkeniq_cc.discovery":
                    importers.add(str(path.relative_to(root)))
        assert importers == {"api/operational_agents.py"}

    def test_the_composer_is_pure(self):
        source = inspect.getsource(D)
        tree = ast.parse(source)
        modules = {
            node.module for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert not any(m and m.startswith("harkeniq_cc.db") for m in modules), modules
        assert "sqlalchemy" not in source


# ---------------------------------------------------------------------------
# 8. S3: deletion equivalence and no hidden topology; S4: nothing learned
# ---------------------------------------------------------------------------


def _normalised(body: dict, stack) -> dict:
    out = json.loads(json.dumps(body))
    out.pop("generated_at", None)
    names = {unit_id: f"<region {name}>" for name, unit_id in stack.regions.items()}
    reach = out["scope"]["reach"]
    reach["org_unit_ids"] = sorted(names.get(u, u) for u in reach["org_unit_ids"])
    return out


class TestS3:
    @pytest.mark.parametrize("persona,holds", [
        ("site_a", ("A",)), ("org_ab", ("A", "B")), ("site_d", ("D",)),
        ("device_c", ("C",)), ("class_switch", ("B",)),
    ])
    async def test_deletion_equivalence(self, persona, holds):
        """A hidden site has no influence: discovery over the full estate is
        byte-for-byte discovery over an estate where the hidden sites do not
        exist (A30.26's oracle, over the agent's authorized sites)."""
        classes = ("SEL_CLEAR", "BMC_RESET", "IDENTIFY_LED", "INTERFACE_ENABLE")
        full = await _estate(ALL)
        alone = await _estate(holds)
        a = _normalised(await _persona(full, persona, classes), full)
        b = _normalised(await _persona(alone, persona, classes), alone)
        assert a == b

    @pytest.mark.parametrize("persona,holds", [
        ("site_a", ("A",)), ("org_ab", ("A", "B")), ("site_d", ("D",)),
        ("tenant", ALL),
    ])
    async def test_the_sentinel_finds_nothing_from_a_hidden_site(self, persona, holds):
        stack = await _estate()
        body = await _persona(stack, persona, ("SEL_CLEAR", "BMC_RESET", "IDENTIFY_LED"))
        assert E.leaks(body, holds) == []

    async def test_no_device_inventory_and_no_site_names(self):
        stack = await _estate()
        body = await _persona(stack, "tenant")
        keys, _ = walk(body)
        assert not keys & {
            "devices", "effective_devices", "effective_sites", "agent_name",
            "site_name", "sites", "vendor", "model", "health", "observation",
        }
        text = json.dumps(body)
        for key in ALL:
            assert SITES[key].name not in text


#: S4-protected payload: learned knowledge and generated text (A30.28/29),
#: and the contract's advancement story (A29.7/A30.6).
S4_KEYS = {
    "learning", "learned_signals", "evidence", "outcome_evidence",
    "advancement", "statement", "confidence", "patterns", "pattern",
    "generated", "yaml_text", "explanation", "diagnosis", "suggested_action",
    "cycles", "signal_id", "signal_key", "rationale",
}


class TestS4:
    async def test_no_learned_or_generated_content_by_key_or_value(self):
        stack = await _estate()
        for persona in ("tenant", "site_a"):
            keys, values = walk(await _persona(
                stack, persona, ("SEL_CLEAR", "BMC_RESET")))
            assert not keys & S4_KEYS, keys & S4_KEYS
            text = json.dumps(values)
            for statement in ("cohort-knowledge", "signal-A", "signal-B",
                              "SECRET-SIGNAL-C"):
                assert statement not in text, statement

    def test_the_composer_never_names_the_contracts_learning(self):
        tree = ast.parse(inspect.getsource(D))
        constants = {
            node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        assert not constants & {"learning", "evidence", "advancement",
                                "learned_signals"}


# ---------------------------------------------------------------------------
# 9. Writes nothing
# ---------------------------------------------------------------------------


class TestWritesNothing:
    async def test_accounting_is_the_only_row_it_moves(self):
        """Even for a tenant whose catalogue was never seeded: the lazy
        seed runs inside the request and is rolled back (the dry-run's
        discipline, A22.7)."""
        stack = await _estate()
        await _agent(stack, "b1-nowrite", ("SEL_CLEAR",))
        await _grant(stack, "b1-nowrite", "site", "A")
        before = await _table_counts(stack)
        assert before["cc_capability_catalogue"] == 0
        body = await _discover(stack, "b1-nowrite")
        # It still ANSWERED from the catalogue the runtime would seed.
        assert klass(body, "SEL_CLEAR")["addressable"]["path"] == "condition_catalogue"
        after = await _table_counts(stack)
        changed = {k: (before[k], after[k]) for k in after if before[k] != after[k]}
        assert set(changed) <= {"cc_agent_read_windows"}, changed
        assert after["cc_capability_catalogue"] == 0


# ---------------------------------------------------------------------------
# 10. D4b and the shared reach derivation
# ---------------------------------------------------------------------------


class TestD4b:
    def test_the_agent_view_reads_implemented_from_the_platform(self):
        from harkeniq_cc.operational_agent import agent_view

        view = agent_view(
            agent=NS(id="ag", tenant_id="t", name="ag", description="",
                     status="active", version=1, activated_version=1,
                     autonomy_ceiling=0, require_approval_always=True,
                     max_proposals_per_day=25, created_by="", created_at=None,
                     activated_by="", activated_at=None, last_evaluated_at=None),
            scopes=[], capabilities=[
                NS(kind="action_class", capability_ref="INTERFACE_RESET"),
                NS(kind="action_class", capability_ref="SEL_CLEAR"),
            ],
            devices=[], autonomy_contract=build_autonomy(
                tenant_id="t", actor_id="a", actor_species=ACTOR_AGENT,
                permissions=[], budgets=[], stop_switch=None, outcomes=[],
                safety_rows=[], sites=[],
            ),
        )
        rows = {r["action_type"]: r for r in view["capabilities"]["action_classes"]}
        assert rows["INTERFACE_RESET"]["capability"]["implemented"] is False
        assert rows["INTERFACE_RESET"]["capability"]["reach"] == "unimplemented"
        assert rows["SEL_CLEAR"]["capability"]["implemented"] is True

    def test_one_reach_derivation_serves_both(self):
        from harkeniq_cc import operational_agent

        view_src = inspect.getsource(operational_agent._capability_view)
        disc_src = inspect.getsource(D._in_effective_scope)
        assert "reach_state(" in view_src and "reach_state(" in disc_src
        assert '"no_devices_in_scope"' not in view_src

    def test_the_firmware_preflight_is_truthful(self):
        from harkeniq_cc.agent_activation import BLOCKED, WARN, check_capabilities

        reach = {"implemented": {"FIRMWARE_UPDATE", "IDENTIFY_LED"}, "unknown": False}
        rows = {"FIRMWARE_UPDATE": {}, "IDENTIFY_LED": {}}
        mixed = check_capabilities({"FIRMWARE_UPDATE", "IDENTIFY_LED"}, rows, reach)
        assert mixed["verdict"] == WARN and "FIRMWARE_UPDATE" in mixed["detail"]
        alone = check_capabilities({"FIRMWARE_UPDATE"}, rows, reach)
        assert alone["verdict"] == BLOCKED


# ---------------------------------------------------------------------------
# 11. D5: the actor-species declaration
# ---------------------------------------------------------------------------


class TestActorSpecies:
    def test_the_declaration(self):
        assert (ACTOR_HUMAN, ACTOR_AGENT, ACTOR_CAMPAIGN) == ("human", "agent", "campaign")
        assert ACTOR_SPECIES == frozenset({"human", "agent", "campaign"})

    @pytest.mark.parametrize("bad", ["user", "machine", "", "Human", "AGENT"])
    def test_build_autonomy_refuses_anything_else(self, bad):
        with pytest.raises(ValueError, match="A30.32"):
            build_autonomy(
                tenant_id="t", actor_id="a", actor_species=bad, permissions=[],
                budgets=[], stop_switch=None, outcomes=[], safety_rows=[], sites=[],
            )

    def test_the_one_mapping(self):
        from harkeniq_cc.autonomy import actor_species_of
        from harkeniq_cc.auth import UserContext

        person = UserContext(user_id="u", email="", tenant_id="t", role="viewer")
        machine = UserContext(user_id="a", email="", tenant_id="t", role="",
                              species="agent")
        assert actor_species_of(person) == ACTOR_HUMAN
        assert actor_species_of(machine) == ACTOR_AGENT
        with pytest.raises(ValueError):
            actor_species_of(NS(species="robot"))
        with pytest.raises(ValueError):
            actor_species_of(object())

    def test_user_context_species_is_unchanged(self):
        from harkeniq_cc.auth import UserContext
        from harkeniq_cc.machine_identity import SPECIES_AGENT, SPECIES_USER

        assert (SPECIES_USER, SPECIES_AGENT) == ("user", "agent")
        assert UserContext(user_id="u", email="", tenant_id="t", role="").species == "user"

    def test_no_literal_species_is_written_anywhere(self):
        """Structural: every `actor_species=` is a name, never a string, and
        every `"species"` payload field is a name, never a string."""
        root = pathlib.Path(harkeniq_cc.__file__).parent
        offenders = []
        for path in sorted(root.rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.Call):
                    for kw in node.keywords:
                        if kw.arg == "actor_species" and isinstance(kw.value, ast.Constant):
                            offenders.append(f"{path.name}:{node.lineno} actor_species")
                if isinstance(node, ast.Dict):
                    for k, v in zip(node.keys, node.values):
                        if isinstance(k, ast.Constant) and k.value == "species" \
                                and isinstance(v, ast.Constant):
                            offenders.append(f"{path.name}:{k.lineno} species")
        assert offenders == [], offenders

    async def test_the_values_did_not_change_on_the_wire(self):
        stack = await _estate(("A",))
        body = await _persona(stack, "site_a")
        assert body["agent"]["species"] == "agent"
        stack.as_person()
        async with stack.client() as c:
            contract = (await c.get("/api/autonomy/")).json()
        assert contract["actor"]["species"] == "human"


# ---------------------------------------------------------------------------
# 12. S3-E1 is not implemented
# ---------------------------------------------------------------------------


class TestE1IsNotImplemented:
    def test_no_global_gate_and_no_site_local_assessment_exist(self):
        root = pathlib.Path(harkeniq_cc.__file__).parent
        for path in root.rglob("*.py"):
            text = path.read_text()
            for name in ("GLOBAL_SAFETY_CONSTRAINT", "global_safety_gate",
                         "site_local_assessment", "SiteLocalAssessment"):
                assert name not in text, (path.name, name)

    def test_admission_still_composes_over_the_tenant(self):
        assert D.ADMISSION_COMPOSED_OVER == "tenant"
        assert D.admission_reads_beyond(NS(tenant_wide=False)) is True
        assert D.admission_reads_beyond(NS(tenant_wide=True)) is False
        from tests.unit.cc.test_a30_26_autonomy_scope_isolation import (
            INTERNAL_DECISIONS, _loader_calls,
        )
        whole_tenant = {
            (module, fn) for module, fn, reach in _loader_calls()
            if isinstance(reach, ast.Constant) and reach.value is None
        }
        assert whole_tenant == INTERNAL_DECISIONS
        assert ("api/operational_agents.py", "agent_discovery") not in whole_tenant


# ---------------------------------------------------------------------------
# 13. G12: recorded by A30.32, fixed by A30.33
# ---------------------------------------------------------------------------


class TestRecordedNotFixed:
    # G12 (A30.32, pre-existing) was pinned here by a strict xfail: an agent
    # holding a tenant-scope grant could not be administered, because
    # _agent_scope_rules rebuilt its rows as ScopeRule models whose
    # scope_ref had to be non-empty. A30.33 fixed it; this is now plain
    # regression coverage, and tests/unit/cc/test_a30_33_g12_tenant_scope_
    # admin.py carries the full proof.
    async def test_an_agent_with_a_tenant_grant_can_be_administered(self):
        stack = await _estate(("A",))
        await _agent(stack, "b1-g12")
        await _grant(stack, "b1-g12", "tenant")
        stack.as_person()
        async with stack.client() as c:
            res = await c.patch(f"{PREFIX}/b1-g12", json={"description": "g12"})
        assert res.status_code == 200, res.status_code

    async def test_discovery_is_unaffected_by_it(self):
        """The same agent, the same grant: discovery answers truthfully."""
        stack = await _estate(("A",))
        await _agent(stack, "b1-g12")
        await _grant(stack, "b1-g12", "tenant")
        body = await _discover(stack, "b1-g12")
        assert body["scope"]["reach"]["tenant_wide"] is True
