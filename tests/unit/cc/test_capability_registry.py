"""Capability Registry at Central Command: composer, API, and consumers.

The node declares (tests/unit/test_capability_declaration.py anchors
that to reality); this file proves the platform REFLECTS the declaration
faithfully and refuses to act on capability it does not have.

Three properties are load-bearing and every test here defends one:

  1. The Registry declares nothing. Risk, reversibility, implementation
     and per-device reach all come from their single sources.
  2. Unknown is not zero. A device that has not declared can still be
     capable, and treating unknown as "no" would break every fleet that
     upgrades Central Command before its agents.
  3. Capability confers nothing. `available` is not permission, scope,
     autonomy, approval or execution authority.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from harkeniq.autonomy.preconditions import ACTION_REVERSIBILITY, ACTION_RISK
from harkeniq.capabilities import action_facts, declare
from harkeniq.models import ActionType
from harkeniq_cc.app import create_app
from harkeniq_cc.auth import configure_auth
from harkeniq_cc.capabilities import (
    REACH_AVAILABLE,
    REACH_NONE,
    REACH_UNIMPLEMENTED,
    REACH_UNKNOWN,
    WHY_ALLOW_LIST,
    WHY_PROTOCOL,
    WHY_UNDECLARED,
    WHY_UNIMPLEMENTED,
    build_capability_registry,
    reachable_action_classes,
)
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.repos import FleetCacheRepo, SiteRepo
from harkeniq_cc.runtime import AppState

TENANT = "test-tenant"

SWITCH_DECL = declare("gnmi", ["INTERFACE_DISABLE", "INTERFACE_ENABLE"], "switch")
#: A switch whose node permits only ENABLE -- implemented, not permitted.
SWITCH_NARROW = declare("gnmi", ["INTERFACE_ENABLE"], "switch")
SERVER_DECL = declare(
    "redfish", ["IDENTIFY_LED", "COLLECT_DIAGNOSTICS", "SEL_CLEAR"], "server"
)
IPMI_DECL = declare("ipmi", ["IDENTIFY_LED", "SEL_CLEAR", "POWER_CYCLE"], "server")


class _Device:
    def __init__(self, agent_id, site_id="s1", capabilities=None, **kw):
        self.agent_id = agent_id
        self.agent_name = kw.get("agent_name", agent_id)
        self.site_id = site_id
        self.vendor = kw.get("vendor", "Dell")
        self.model = kw.get("model", "R750")
        self.device_class = kw.get("device_class", "server")
        self.capabilities = capabilities


class _Site:
    def __init__(self, id, name):
        self.id = id
        self.name = name


def _classes(registry) -> dict:
    return {c["action_type"]: c for c in registry["classes"]}


class TestTheRegistryDeclaresNothing:
    def test_risk_and_reversibility_come_from_their_single_sources(self):
        rows = _classes(build_capability_registry(tenant_id=TENANT, devices=[]))
        for action in ActionType:
            row = rows[action.value]
            assert row["risk"] == ACTION_RISK[action]
            assert row["reversibility"] == ACTION_REVERSIBILITY[action]

    def test_implementation_comes_from_the_protocols(self):
        rows = _classes(build_capability_registry(tenant_id=TENANT, devices=[]))
        facts = action_facts()
        for name, row in rows.items():
            assert row["implemented"] == facts[name]["implemented"]
            assert row["implemented_by"] == facts[name]["implemented_by"]

    def test_every_governed_class_is_reported_including_unrunnable_ones(self):
        """Filtering unimplemented classes away to make the output tidy
        would delete the entire point of this endpoint."""
        rows = _classes(build_capability_registry(tenant_id=TENANT, devices=[]))
        assert set(rows) == {a.value for a in ActionType}


class TestD1AndTheUnimplementedClasses:
    def test_unimplemented_classes_report_unimplemented_reach(self):
        devices = [_Device("sw1", capabilities=SWITCH_DECL, device_class="switch")]
        rows = _classes(build_capability_registry(tenant_id=TENANT, devices=devices))
        for name in ("INTERFACE_RESET", "CLEAR_COUNTERS"):
            assert rows[name]["reach"] == REACH_UNIMPLEMENTED
            assert rows[name]["effective_device_count"] == 0
            assert rows[name]["blocked_by"] == [
                {"reason": WHY_UNIMPLEMENTED, "device_count": 1}
            ]

    def test_the_reason_names_the_real_problem(self):
        rows = _classes(build_capability_registry(tenant_id=TENANT, devices=[]))
        reason = rows["INTERFACE_RESET"]["reason"]
        assert "no implementation" in reason
        assert "risk level, preconditions and blast-radius" in reason

    def test_governed_semantics_are_still_reported(self):
        """D1: the class keeps everything except an executor."""
        rows = _classes(build_capability_registry(tenant_id=TENANT, devices=[]))
        assert rows["INTERFACE_RESET"]["risk"] == "high"
        assert rows["INTERFACE_RESET"]["reversibility"] is not None


class TestEffectiveReach:
    def test_a_capable_device_makes_a_class_available(self):
        devices = [_Device("sw1", capabilities=SWITCH_DECL, device_class="switch")]
        rows = _classes(build_capability_registry(tenant_id=TENANT, devices=devices))
        assert rows["INTERFACE_DISABLE"]["reach"] == REACH_AVAILABLE
        assert rows["INTERFACE_DISABLE"]["effective_device_count"] == 1
        assert rows["INTERFACE_DISABLE"]["effective_devices"][0]["agent_id"] == "sw1"

    def test_no_code_and_not_permitted_are_different_reasons(self):
        """The whole reason three sets are carried instead of one."""
        devices = [
            # implemented by gnmi, but this node does not permit DISABLE
            _Device("sw-narrow", capabilities=SWITCH_NARROW, device_class="switch"),
            # redfish does not implement INTERFACE_DISABLE at all
            _Device("srv1", capabilities=SERVER_DECL),
        ]
        rows = _classes(build_capability_registry(tenant_id=TENANT, devices=devices))
        blocked = {b["reason"]: b["device_count"] for b in rows["INTERFACE_DISABLE"]["blocked_by"]}
        assert blocked == {WHY_ALLOW_LIST: 1, WHY_PROTOCOL: 1}
        assert rows["INTERFACE_DISABLE"]["reach"] == REACH_NONE

    def test_effective_sites_carry_id_and_name(self):
        devices = [
            _Device("sw1", site_id="s1", capabilities=SWITCH_DECL, device_class="switch")
        ]
        registry = build_capability_registry(
            tenant_id=TENANT, devices=devices, sites=[_Site("s1", "dc-blr-1")]
        )
        row = _classes(registry)["INTERFACE_DISABLE"]
        assert row["effective_sites"] == [{"id": "s1", "name": "dc-blr-1"}]

    def test_device_lists_are_capped_and_say_so(self):
        devices = [
            _Device(f"srv{i}", capabilities=SERVER_DECL) for i in range(40)
        ]
        row = _classes(
            build_capability_registry(tenant_id=TENANT, devices=devices)
        )["IDENTIFY_LED"]
        assert row["effective_device_count"] == 40
        assert len(row["effective_devices"]) == 25
        assert row["effective_devices_truncated"] is True

    def test_site_filter_narrows_and_never_widens(self):
        devices = [
            _Device("sw1", site_id="s1", capabilities=SWITCH_DECL, device_class="switch"),
            _Device("sw2", site_id="s2", capabilities=SWITCH_DECL, device_class="switch"),
        ]
        row = _classes(
            build_capability_registry(
                tenant_id=TENANT, devices=devices, site_id="s1"
            )
        )["INTERFACE_DISABLE"]
        assert row["effective_device_count"] == 1
        assert row["devices_in_view"] == 1


class TestUnknownIsNotZero:
    def test_an_undeclared_device_reads_unknown(self):
        devices = [_Device("srv1", capabilities=None)]
        rows = _classes(build_capability_registry(tenant_id=TENANT, devices=devices))
        assert rows["IDENTIFY_LED"]["reach"] == REACH_UNKNOWN
        assert rows["IDENTIFY_LED"]["undeclared_device_count"] == 1
        assert "not zero" in rows["IDENTIFY_LED"]["reason"]

    def test_unknown_is_never_reported_as_incapable(self):
        rows = _classes(
            build_capability_registry(
                tenant_id=TENANT, devices=[_Device("srv1", capabilities=None)]
            )
        )
        assert rows["IDENTIFY_LED"]["reach"] != REACH_NONE

    def test_a_malformed_declaration_reads_unknown_not_empty(self):
        devices = [_Device("srv1", capabilities={"garbage": True})]
        rows = _classes(build_capability_registry(tenant_id=TENANT, devices=devices))
        assert rows["IDENTIFY_LED"]["reach"] == REACH_UNKNOWN

    def test_no_devices_in_view_is_unknown_not_a_denial(self):
        rows = _classes(build_capability_registry(tenant_id=TENANT, devices=[]))
        assert rows["IDENTIFY_LED"]["reach"] == REACH_UNKNOWN
        assert rows["IDENTIFY_LED"]["devices_in_view"] == 0

    def test_reachable_action_classes_reports_unknown_separately(self):
        mixed = reachable_action_classes([
            _Device("srv1", capabilities=SERVER_DECL),
            _Device("srv2", capabilities=None),
        ])
        assert "IDENTIFY_LED" in mixed["effective"]
        assert mixed["unknown"] is True
        assert mixed["devices"] == 2

        all_declared = reachable_action_classes([
            _Device("srv1", capabilities=SERVER_DECL)
        ])
        assert all_declared["unknown"] is False

    def test_declared_but_empty_is_a_proven_no(self):
        empty = declare("redfish", [])
        result = reachable_action_classes([_Device("srv1", capabilities=empty)])
        assert result["effective"] == set()
        assert result["unknown"] is False


class TestUnionAcrossProtocols:
    def test_reach_is_the_union_not_the_intersection(self):
        devices = [
            _Device("srv1", capabilities=SERVER_DECL),
            _Device("sw1", capabilities=SWITCH_DECL, device_class="switch"),
        ]
        rows = _classes(build_capability_registry(tenant_id=TENANT, devices=devices))
        assert rows["SEL_CLEAR"]["reach"] == REACH_AVAILABLE
        assert rows["INTERFACE_DISABLE"]["reach"] == REACH_AVAILABLE

    def test_ipmi_narrower_reach_is_reported_honestly(self):
        devices = [_Device("ipmi1", capabilities=IPMI_DECL)]
        rows = _classes(build_capability_registry(tenant_id=TENANT, devices=devices))
        # POWER_CYCLE is on the allow list but IPMI does not implement it.
        assert rows["POWER_CYCLE"]["reach"] == REACH_NONE
        assert rows["POWER_CYCLE"]["blocked_by"] == [
            {"reason": WHY_PROTOCOL, "device_count": 1}
        ]
        assert rows["SEL_CLEAR"]["reach"] == REACH_AVAILABLE


class TestContractStatesItConfersNothing:
    def test_the_authority_note_is_part_of_the_contract(self):
        contract = build_capability_registry(tenant_id=TENANT, devices=[])["contract"]
        assert "not permission" in contract["authority"]
        assert "final execution authority" in contract["authority"]

    def test_the_unknown_semantics_are_part_of_the_contract(self):
        contract = build_capability_registry(tenant_id=TENANT, devices=[])["contract"]
        assert "never capable and never incapable" in contract["unknown"]

    def test_implemented_is_declared_as_capability_existence(self):
        contract = build_capability_registry(tenant_id=TENANT, devices=[])["contract"]
        assert "CAPABILITY EXISTENCE" in contract["implemented"]
        assert "ONLY ground" in contract["implemented"]

    def test_effective_is_declared_a_projection_not_a_definition(self):
        """A consumer reading `effective` as capability existence would
        refuse work the platform can perform. The contract has to say so
        in its own words, not only in ours."""
        contract = build_capability_registry(tenant_id=TENANT, devices=[])["contract"]
        assert "CONFIGURATION/READINESS PROJECTION" in contract["effective"]
        assert "not a definition" in contract["effective"]
        assert "MUST still bind" in contract["effective"]

    def test_the_refusal_ground_is_stated(self):
        contract = build_capability_registry(tenant_id=TENANT, devices=[])["contract"]
        assert "only for absence of implementation" in contract["refusal"]
        assert "never refused because a node does not currently permit" in (
            contract["refusal"]
        )

    def test_every_effective_field_is_covered_by_the_stated_meaning(self):
        """If a new effective_* field appears, the contract sentence that
        defines them all must still be the one that describes it."""
        row = build_capability_registry(
            tenant_id=TENANT,
            devices=[_Device("srv1", capabilities=SERVER_DECL)],
        )["classes"][0]
        effective_fields = {k for k in row if k.startswith("effective")}
        assert effective_fields == {
            "effective_device_count",
            "effective_sites",
            "effective_devices",
            "effective_devices_truncated",
        }, effective_fields


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@pytest.fixture
async def client():
    config = CCConfig(tenant_id=TENANT, insecure=True)
    configure_auth("", "", "", insecure=True)
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sessionmaker = make_sessionmaker(engine)
    app = create_app(
        AppState(config=config, engine=engine, sessionmaker=sessionmaker)
    )
    async with sessionmaker() as session:
        site = await SiteRepo(session).upsert(
            TENANT, "dc-blr-1", "https://sm1.lab:50051"
        )
        cache = FleetCacheRepo(session)
        await cache.upsert_device(
            site_id=site.id, agent_id="sw-01", agent_name="tor-01",
            vendor="sonic", model="AS7326", device_class="switch",
            capabilities=SWITCH_DECL,
        )
        await cache.upsert_device(
            site_id=site.id, agent_id="srv-01", agent_name="node-01",
            vendor="Dell", model="R750", device_class="server",
            capabilities=SERVER_DECL,
        )
        await cache.upsert_device(
            site_id=site.id, agent_id="srv-02", agent_name="node-02",
            vendor="Dell", model="R750", device_class="server",
        )
        await session.commit()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await engine.dispose()


class TestCapabilityAPI:
    async def test_registry_lists_every_class(self, client):
        r = await client.get("/api/capabilities/")
        assert r.status_code == 200
        body = r.json()
        assert {c["action_type"] for c in body["classes"]} == {
            a.value for a in ActionType
        }

    async def test_fleet_summary_counts_declared_and_undeclared(self, client):
        body = (await client.get("/api/capabilities/")).json()
        assert body["fleet"] == {
            "devices_in_view": 3,
            "declared": 2,
            "undeclared": 1,
            "protocols": ["gnmi", "redfish"],
        }

    async def test_unimplemented_classes_are_visible_over_the_wire(self, client):
        rows = _classes((await client.get("/api/capabilities/")).json())
        assert rows["INTERFACE_RESET"]["implemented"] is False
        assert rows["CLEAR_COUNTERS"]["implemented"] is False

    async def test_action_type_filter(self, client):
        body = (
            await client.get("/api/capabilities/?action_type=SEL_CLEAR")
        ).json()
        assert [c["action_type"] for c in body["classes"]] == ["SEL_CLEAR"]

    async def test_device_detail_explains_each_class(self, client):
        listing = (await client.get("/api/fleet/")).json()
        device = next(
            d for d in listing["devices"] if d["agent_id"] == "sw-01"
        )
        r = await client.get(f"/api/capabilities/devices/{device['id']}")
        assert r.status_code == 200
        body = r.json()
        assert body["declared"] is True
        assert body["device"]["device_class"] == "switch"
        rows = {c["action_type"]: c for c in body["classes"]}
        assert rows["INTERFACE_DISABLE"]["can_execute"] is True
        assert rows["INTERFACE_DISABLE"]["blocked_by"] is None
        assert rows["INTERFACE_RESET"]["can_execute"] is False
        assert rows["INTERFACE_RESET"]["blocked_by"] == WHY_UNIMPLEMENTED
        assert rows["SEL_CLEAR"]["can_execute"] is False
        assert rows["SEL_CLEAR"]["blocked_by"] == WHY_PROTOCOL

    async def test_an_undeclared_device_says_so_rather_than_denying(self, client):
        listing = (await client.get("/api/fleet/")).json()
        device = next(
            d for d in listing["devices"] if d["agent_id"] == "srv-02"
        )
        body = (
            await client.get(f"/api/capabilities/devices/{device['id']}")
        ).json()
        assert body["declared"] is False
        assert body["declaration"] is None
        rows = {c["action_type"]: c for c in body["classes"]}
        assert rows["IDENTIFY_LED"]["blocked_by"] == WHY_UNDECLARED
        # An unimplemented class is unimplemented regardless of declaration.
        assert rows["INTERFACE_RESET"]["blocked_by"] == WHY_UNIMPLEMENTED

    async def test_unknown_device_is_404(self, client):
        r = await client.get("/api/capabilities/devices/does-not-exist")
        assert r.status_code == 404

    async def test_the_registry_adds_no_mutation(self, client):
        for method in ("post", "put", "delete", "patch"):
            r = await getattr(client, method)("/api/capabilities/")
            assert r.status_code == 405
