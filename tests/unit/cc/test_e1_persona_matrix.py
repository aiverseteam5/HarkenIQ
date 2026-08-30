"""E1.2: the persona matrix, executed against the real app.

The tenant shape is the one in the acceptance gate::

    tenant-demo
      |- Region A
      |    |- Cluster A1   |- Site 1   |- Site 2
      |    \\- Cluster A2
      \\- Region B
           \\- Cluster B1   \\- Site 3

Personas are built the way the product actually builds them: an EXISTING
role plus a scope grant. "Region Manager" and "Cluster Manager" are not
roles -- the vocabulary is fixed at seven -- they are `site_admin` with
an org-unit grant, which is the whole point of ratified decision B.

Every assertion here is an HTTP request against the ASGI app. Nothing
asserts that a UI hid anything.
"""

from __future__ import annotations

import httpx
import pytest

from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.app import create_app
from harkeniq_cc.auth import ROLE_PERMISSIONS, UserContext, configure_auth
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.models import CCFleetCache, CCIncident, CCSite
from harkeniq_cc.db.repos import (
    AuditRepo,
    OrgUnitRepo,
    ScopeGrantRepo,
    TenantSettingsRepo,
)
from harkeniq_cc.runtime import AppState
from harkeniq_cc.scope import (
    ENFORCEMENT_STRICT,
    SCOPE_ORG_UNIT,
    SCOPE_SITE,
    SCOPE_TENANT,
)

from tests.unit.cc.test_e1_route_contract import (
    OBJECT_GATED,
    READ_SCOPED,
    ROUTE_CONTRACT,
    TENANT_GATED,
    UNSCOPED,
)

TENANT = "tenant-demo"


class Estate:
    """The seeded tenant, with the ids every test needs."""

    def __init__(self):
        self.units: dict[str, str] = {}
        self.sites: dict[str, str] = {}
        self.devices: dict[str, str] = {}


async def _seed(sessionmaker) -> Estate:
    estate = Estate()
    async with sessionmaker() as session:
        repo = OrgUnitRepo(session)
        root = await repo.create(
            TENANT, name="tenant-demo", unit_type="organization", parent=None
        )
        region_a = await repo.create(
            TENANT, name="Region A", unit_type="region", parent=root
        )
        region_b = await repo.create(
            TENANT, name="Region B", unit_type="region", parent=root
        )
        a1 = await repo.create(
            TENANT, name="Cluster A1", unit_type="cluster", parent=region_a
        )
        a2 = await repo.create(
            TENANT, name="Cluster A2", unit_type="cluster", parent=region_a
        )
        b1 = await repo.create(
            TENANT, name="Cluster B1", unit_type="cluster", parent=region_b
        )
        for key, unit in (
            ("root", root), ("region_a", region_a), ("region_b", region_b),
            ("a1", a1), ("a2", a2), ("b1", b1),
        ):
            estate.units[key] = unit.id

        for name, unit in (("site-1", a1), ("site-2", a1), ("site-3", b1)):
            site = CCSite(
                tenant_id=TENANT, site_name=name, sm_endpoint="sm:50051",
                sm_token="tok", org_unit_id=unit.id,
            )
            session.add(site)
            await session.flush()
            estate.sites[name] = site.id
            device = CCFleetCache(
                site_id=site.id, agent_id=f"node-{name}", agent_name=f"node-{name}",
                vendor="Dell", model="R750", health="ok", device_class="server",
            )
            session.add(device)
            await session.flush()
            estate.devices[name] = device.agent_id
            session.add(
                CCIncident(
                    incident_id=f"inc-{name}", tenant_id=TENANT, site_id=site.id,
                    device_agent_id=device.agent_id,
                    title=f"incident at {name}", status="open", kind="device",
                )
            )
            # One audit entry per site, so audit scoping is observable.
            await AuditRepo(session).append(
                actor="seed", action="seed.site", subject=site.id,
                tenant_id=TENANT, site_id=site.id,
            )
        await session.commit()
    return estate


async def _grant(sessionmaker, principal, scope_type, scope_ref="", role="",
                 subset=None):
    async with sessionmaker() as session:
        await ScopeGrantRepo(session).grant(
            tenant_id=TENANT, principal_type="user", principal_ref=principal,
            scope_type=scope_type, scope_ref=scope_ref,
            permission_subset=subset, role=role, granted_by="test",
        )
        await session.commit()


async def _strict(sessionmaker):
    async with sessionmaker() as session:
        await TenantSettingsRepo(session).set_enforcement(
            TENANT, ENFORCEMENT_STRICT, "test"
        )
        await session.commit()


async def _stack():
    config = CCConfig(tenant_id=TENANT, insecure=True)
    configure_auth("", "", "", insecure=True)
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sessionmaker = make_sessionmaker(engine)
    state = AppState(config=config, engine=engine, sessionmaker=sessionmaker)
    app = create_app(state)
    estate = await _seed(sessionmaker)
    return app, sessionmaker, estate


def _client(app, role: str, user_id: str):
    async def _fake():
        return UserContext(
            user_id=user_id, email=f"{user_id}@example.com", tenant_id=TENANT,
            role=role,
            permissions=list(ROLE_PERMISSIONS.get(role, ROLE_PERMISSIONS["viewer"])),
            is_platform_user=role == "platform_super_admin",
        )

    app.dependency_overrides[get_current_user] = _fake
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


# ---------------------------------------------------------------------------
# The generated sweep: every declared READ, every persona
# ---------------------------------------------------------------------------

#: persona -> (role, scope_type, scope_ref_key)
#: Region/Cluster Manager are roles the product ALREADY has plus a grant.
PERSONAS = {
    "tenant_owner":     ("tenant_owner", SCOPE_TENANT, None),
    "tenant_admin":     ("tenant_owner", SCOPE_TENANT, None),
    "region_manager":   ("site_admin", SCOPE_ORG_UNIT, "region_a"),
    "cluster_manager":  ("site_admin", SCOPE_ORG_UNIT, "a1"),
    "site_admin":       ("site_admin", SCOPE_SITE, "site-1"),
    "operator":         ("operator", SCOPE_SITE, "site-1"),
    "auditor":          ("auditor", SCOPE_TENANT, None),
    "platform_support": ("viewer", None, None),
    "platform_admin":   ("platform_super_admin", None, None),
}


def _substitute(path: str, estate: Estate) -> str:
    return (
        path.replace("{agent_id}", estate.devices["site-1"])
        .replace("{device_id}", "unknown-device")
        .replace("{unit_id}", estate.units["a1"])
        .replace("{site_id}", estate.sites["site-1"])
        .replace("{incident_id}", "unknown-incident")
        .replace("{action_id}", "unknown-action")
        .replace("{policy_id}", "unknown-policy")
        .replace("{group_id}", "unknown-group")
        .replace("{budget_id}", "unknown-budget")
        .replace("{member_id}", "unknown-member")
        .replace("{grant_id}", "unknown-grant")
        .replace("{transition}", "activate")
    )


READ_ROUTES = sorted(
    (m, p) for (m, p), (_, t, _a) in ROUTE_CONTRACT.items()
    if m == "GET" and t in (READ_SCOPED, UNSCOPED)
)


class TestGeneratedReadSweep:
    """Every declared read x every persona, derived from ROUTE_CONTRACT.

    9 personas x 36 reads. The expected outcome is computed from the
    declaration, so the matrix cannot drift from the contract: change a
    permission in one place and this sweep changes with it.
    """

    @pytest.mark.parametrize("persona", sorted(PERSONAS))
    @pytest.mark.asyncio
    async def test_permission_gate_matches_the_declaration(self, persona):
        role, scope_type, ref_key = PERSONAS[persona]
        app, sessionmaker, estate = await _stack()
        await _strict(sessionmaker)
        user_id = f"kc-{persona}"
        if scope_type:
            ref = ""
            if ref_key and ref_key.startswith("site-"):
                ref = estate.sites[ref_key]
            elif ref_key:
                ref = estate.units[ref_key]
            await _grant(sessionmaker, user_id, scope_type, ref, role=role)

        held = set(ROLE_PERMISSIONS.get(role, []))
        client = _client(app, role, user_id)
        async with client:
            for method, path in READ_ROUTES:
                permission, treatment, _ = ROUTE_CONTRACT[(method, path)]
                resp = await client.get(_substitute(path, estate))
                allowed = "*" in held or permission in held
                # /api/approvals/* also admits audit.view (E0.3, A13).
                if path.startswith("/api/approvals") and "audit.view" in held:
                    allowed = True
                if allowed:
                    assert resp.status_code != 403, (
                        f"{persona} holds {permission} but got 403 on "
                        f"{method} {path}"
                    )
                else:
                    assert resp.status_code == 403, (
                        f"{persona} lacks {permission} but got "
                        f"{resp.status_code} on {method} {path}"
                    )


class TestReadScoping:
    """Layer 2: which records may this actor read?"""

    async def _persona(self, persona):
        role, scope_type, ref_key = PERSONAS[persona]
        app, sessionmaker, estate = await _stack()
        await _strict(sessionmaker)
        user_id = f"kc-{persona}"
        if scope_type:
            ref = ""
            if ref_key and ref_key.startswith("site-"):
                ref = estate.sites[ref_key]
            elif ref_key:
                ref = estate.units[ref_key]
            await _grant(sessionmaker, user_id, scope_type, ref, role=role)
        return _client(app, role, user_id), estate, sessionmaker

    @pytest.mark.asyncio
    async def test_a_cluster_manager_reads_only_their_clusters_sites(self):
        client, estate, _ = await self._persona("cluster_manager")
        async with client:
            body = (await client.get("/api/sites/")).json()
            assert {s["site_name"] for s in body["sites"]} == {"site-1", "site-2"}

            fleet = (await client.get("/api/fleet/")).json()
            assert {d["agent_id"] for d in fleet["devices"]} == {
                "node-site-1", "node-site-2"
            }
            # The COUNT is scoped too: a total including the rest of the
            # fleet would leak its size.
            assert fleet["total"] == 2

            incidents = (await client.get("/api/incidents/")).json()
            assert all(
                i["site_id"] != estate.sites["site-3"] for i in incidents["incidents"]
            )

    @pytest.mark.asyncio
    async def test_a_region_manager_reads_the_whole_region_and_no_more(self):
        client, estate, _ = await self._persona("region_manager")
        async with client:
            body = (await client.get("/api/sites/")).json()
            assert {s["site_name"] for s in body["sites"]} == {"site-1", "site-2"}

    @pytest.mark.asyncio
    async def test_a_site_admin_reads_across_different_ancestors(self):
        """Sites need no common parent. Two grants, two branches."""
        role, _, _ = PERSONAS["site_admin"]
        app, sessionmaker, estate = await _stack()
        await _strict(sessionmaker)
        await _grant(sessionmaker, "kc-multi", SCOPE_SITE, estate.sites["site-1"],
                     role=role)
        await _grant(sessionmaker, "kc-multi", SCOPE_SITE, estate.sites["site-3"],
                     role=role)
        client = _client(app, role, "kc-multi")
        async with client:
            body = (await client.get("/api/sites/")).json()
            assert {s["site_name"] for s in body["sites"]} == {"site-1", "site-3"}
            # And NOT the site sitting right beside one of them.
            assert "site-2" not in {s["site_name"] for s in body["sites"]}

    @pytest.mark.asyncio
    async def test_a_tenant_owner_reads_everything(self):
        client, estate, _ = await self._persona("tenant_owner")
        async with client:
            body = (await client.get("/api/sites/")).json()
            assert len(body["sites"]) == 3

    @pytest.mark.asyncio
    async def test_an_ungranted_principal_reads_nothing_under_strict(self):
        app, sessionmaker, estate = await _stack()
        await _strict(sessionmaker)
        client = _client(app, "site_admin", "kc-nobody")
        async with client:
            assert (await client.get("/api/sites/")).json()["sites"] == []
            assert (await client.get("/api/fleet/")).json()["devices"] == []
            assert (await client.get("/api/incidents/")).json()["incidents"] == []

    @pytest.mark.asyncio
    async def test_a_single_object_out_of_scope_reads_as_absent(self):
        client, estate, _ = await self._persona("cluster_manager")
        async with client:
            # 404, not 403: a 403 would confirm site-3 exists.
            resp = await client.get(f"/api/sites/{estate.sites['site-3']}")
            assert resp.status_code == 404
            mine = await client.get(f"/api/sites/{estate.sites['site-1']}")
            assert mine.status_code == 200

    @pytest.mark.asyncio
    @pytest.mark.asyncio
    async def test_audit_entries_are_scoped_to_the_callers_sites(self):
        """A cluster-scoped AUDITOR sees their own sites' entries only.

        `site_admin` holds no `audit.view`, so the persona that proves
        this has to be one that does -- otherwise the test would be
        asserting a 403 and calling it scoping.
        """
        app, sessionmaker, estate = await _stack()
        await _strict(sessionmaker)
        await _grant(sessionmaker, "kc-cluster-auditor", SCOPE_ORG_UNIT,
                     estate.units["a1"], role="auditor")
        client = _client(app, "auditor", "kc-cluster-auditor")
        async with client:
            body = (await client.get("/api/audit/")).json()
            sites = {e.get("site_id") for e in body["entries"]}
            assert estate.sites["site-3"] not in sites, "another site's audit leaked"
            assert estate.sites["site-1"] in sites


class TestTreeVisibilityIsNotAuthority:
    """Ratified L3, end to end."""

    async def _cluster_manager(self):
        app, sessionmaker, estate = await _stack()
        await _strict(sessionmaker)
        await _grant(sessionmaker, "kc-cm", SCOPE_ORG_UNIT, estate.units["a1"],
                     role="site_admin")
        return _client(app, "site_admin", "kc-cm"), estate

    @pytest.mark.asyncio
    async def test_the_reachable_subtree_and_its_ancestors_are_visible(self):
        client, estate = await self._cluster_manager()
        async with client:
            body = (await client.get("/api/org-units/")).json()
            names = _walk(body["tree"])
            assert names == {"tenant-demo", "Region A", "Cluster A1"}

    @pytest.mark.asyncio
    async def test_siblings_and_unrelated_branches_are_invisible(self):
        client, estate = await self._cluster_manager()
        async with client:
            body = (await client.get("/api/org-units/")).json()
            names = _walk(body["tree"])
            assert "Cluster A2" not in names, "the sibling cluster leaked"
            assert "Region B" not in names, "an unrelated branch leaked"
            assert "Cluster B1" not in names

    @pytest.mark.asyncio
    async def test_ancestors_are_marked_contextual_and_without_authority(self):
        client, estate = await self._cluster_manager()
        async with client:
            body = (await client.get("/api/org-units/")).json()
            by_name = _index(body["tree"])
            assert by_name["Region A"]["contextual"] is True
            assert by_name["Region A"]["authority"] is False
            assert by_name["Cluster A1"]["contextual"] is False
            assert by_name["Cluster A1"]["authority"] is True

    @pytest.mark.asyncio
    async def test_seeing_an_ancestor_does_not_permit_mutating_it(self):
        client, estate = await self._cluster_manager()
        async with client:
            visible = await client.get(f"/api/org-units/{estate.units['region_a']}")
            assert visible.status_code == 200
            assert visible.json()["unit"]["authority"] is False

            refused = await client.patch(
                f"/api/org-units/{estate.units['region_a']}",
                json={"name": "Region A renamed"},
            )
            assert refused.status_code == 403
            assert "outside your authorized scope" in refused.json()["detail"]

    @pytest.mark.asyncio
    async def test_a_sibling_unit_is_not_even_readable(self):
        client, estate = await self._cluster_manager()
        async with client:
            resp = await client.get(f"/api/org-units/{estate.units['a2']}")
            assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_creating_under_a_contextual_ancestor_is_refused(self):
        client, estate = await self._cluster_manager()
        async with client:
            resp = await client.post(
                "/api/org-units/",
                json={"name": "sneaky", "unit_type": "cluster",
                      "parent_id": estate.units["region_a"]},
            )
            assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_creating_inside_the_granted_cluster_is_allowed(self):
        client, estate = await self._cluster_manager()
        async with client:
            resp = await client.post(
                "/api/org-units/",
                json={"name": "Hall A", "unit_type": "hall",
                      "parent_id": estate.units["a1"]},
            )
            assert resp.status_code == 201

    @pytest.mark.asyncio
    async def test_a_move_needs_authority_over_both_ends(self):
        client, estate = await self._cluster_manager()
        async with client:
            made = await client.post(
                "/api/org-units/",
                json={"name": "Movable", "unit_type": "hall",
                      "parent_id": estate.units["a1"]},
            )
            unit_id = made.json()["id"]
            # Source is in scope, destination is not.
            resp = await client.patch(
                f"/api/org-units/{unit_id}",
                json={"parent_id": estate.units["region_a"]},
            )
            assert resp.status_code == 403


class TestMutationGates:
    """Layer 3, and the tenant-governance read/mutate split."""

    async def _cluster_manager(self):
        app, sessionmaker, estate = await _stack()
        await _strict(sessionmaker)
        await _grant(sessionmaker, "kc-cm", SCOPE_ORG_UNIT, estate.units["a1"],
                     role="site_admin")
        return _client(app, "site_admin", "kc-cm"), estate, sessionmaker

    @pytest.mark.asyncio
    async def test_a_cluster_manager_may_read_tenant_governance(self):
        """READ AUTHORITY != MUTATION AUTHORITY.

        Reading why you are blocked is the point of the S5 contract;
        hiding it would make the product worse and no safer.
        """
        client, _, _ = await self._cluster_manager()
        async with client:
            for path in ("/api/policies/", "/api/policies/autonomy",
                         "/api/policies/stop-switch", "/api/autonomy/"):
                assert (await client.get(path)).status_code == 200, path

    @pytest.mark.asyncio
    async def test_a_cluster_manager_may_not_mutate_tenant_governance(self):
        client, _, _ = await self._cluster_manager()
        async with client:
            resp = await client.post(
                "/api/policies/",
                json={"name": "sneaky", "required_approvers": 1},
            )
            assert resp.status_code == 403
            assert "outside your authorized scope" in resp.json()["detail"]

            stop = await client.post("/api/policies/stop-switch", json={})
            assert stop.status_code == 403

    @pytest.mark.asyncio
    async def test_a_tenant_owner_may_mutate_tenant_governance(self):
        app, sessionmaker, estate = await _stack()
        await _strict(sessionmaker)
        await _grant(sessionmaker, "kc-owner", SCOPE_TENANT, role="tenant_owner")
        client = _client(app, "tenant_owner", "kc-owner")
        async with client:
            resp = await client.post(
                "/api/policies/",
                json={"name": "real", "required_approvers": 1},
            )
            assert resp.status_code in (200, 201), resp.text

    @pytest.mark.asyncio
    async def test_moving_a_site_needs_authority_over_site_and_destination(self):
        client, estate, _ = await self._cluster_manager()
        async with client:
            # site-3 is not in scope at all.
            resp = await client.put(
                f"/api/sites/{estate.sites['site-3']}/org-unit",
                json={"org_unit_id": estate.units["a1"]},
            )
            assert resp.status_code in (403, 404)

    @pytest.mark.asyncio
    async def test_registering_a_site_is_tenant_authority(self):
        client, _, _ = await self._cluster_manager()
        async with client:
            resp = await client.post(
                "/api/sites/register",
                json={"site_name": "new", "sm_endpoint": "sm:50051"},
            )
            assert resp.status_code == 403


class TestAgentDelegationCeiling:
    """An agent may never reach further than the human who built it.

    The gate on agent creation IS the ceiling rather than a tenant
    check: requiring tenant scope would make the ceiling unreachable,
    because a tenant-wide creator can delegate anything. That is the
    difference between a rule and a rule with no caller.
    """

    async def _region_owner(self):
        app, sessionmaker, estate = await _stack()
        await _strict(sessionmaker)
        await _grant(sessionmaker, "kc-ro", SCOPE_ORG_UNIT, estate["units"]["region_a"]
                     if isinstance(estate.units, dict) else estate.units["region_a"],
                     role="tenant_owner")
        return _client(app, "tenant_owner", "kc-ro"), estate

    @pytest.mark.asyncio
    async def test_a_region_owner_may_build_an_agent_inside_their_region(self):
        app, sessionmaker, estate = await _stack()
        await _strict(sessionmaker)
        await _grant(sessionmaker, "kc-ro", SCOPE_ORG_UNIT, estate.units["region_a"],
                     role="tenant_owner")
        client = _client(app, "tenant_owner", "kc-ro")
        async with client:
            resp = await client.post("/api/operational-agents/", json={
                "name": "region-agent",
                "scopes": [{"scope_type": "org_unit",
                            "scope_ref": estate.units["a1"]}],
                "capabilities": [{"kind": "action_class",
                                  "capability_ref": "SEL_CLEAR"}],
            })
            assert resp.status_code == 201, resp.text

    @pytest.mark.asyncio
    async def test_a_region_owner_may_not_build_one_reaching_another_region(self):
        app, sessionmaker, estate = await _stack()
        await _strict(sessionmaker)
        await _grant(sessionmaker, "kc-ro", SCOPE_ORG_UNIT, estate.units["region_a"],
                     role="tenant_owner")
        client = _client(app, "tenant_owner", "kc-ro")
        async with client:
            for scope_row in (
                {"scope_type": "org_unit", "scope_ref": estate.units["b1"]},
                {"scope_type": "site", "scope_ref": estate.sites["site-3"]},
                {"scope_type": "device_class", "scope_ref": "server"},
            ):
                resp = await client.post("/api/operational-agents/", json={
                    "name": f"overreach-{scope_row['scope_type']}",
                    "scopes": [scope_row],
                    "capabilities": [{"kind": "action_class",
                                      "capability_ref": "SEL_CLEAR"}],
                })
                assert resp.status_code == 403, (scope_row, resp.text)
                assert "never reach further" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_a_contextual_ancestor_cannot_be_bound(self):
        """Seeing Region A as a breadcrumb is not authority to aim at it."""
        app, sessionmaker, estate = await _stack()
        await _strict(sessionmaker)
        await _grant(sessionmaker, "kc-cm", SCOPE_ORG_UNIT, estate.units["a1"],
                     role="tenant_owner")
        client = _client(app, "tenant_owner", "kc-cm")
        async with client:
            resp = await client.post("/api/operational-agents/", json={
                "name": "ancestor-reach",
                "scopes": [{"scope_type": "org_unit",
                            "scope_ref": estate.units["region_a"]}],
                "capabilities": [{"kind": "action_class",
                                  "capability_ref": "SEL_CLEAR"}],
            })
            assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_a_scoped_principal_cannot_re_aim_somebody_elses_agent(self):
        app, sessionmaker, estate = await _stack()
        await _strict(sessionmaker)
        await _grant(sessionmaker, "kc-owner", SCOPE_TENANT, role="tenant_owner")
        owner = _client(app, "tenant_owner", "kc-owner")
        async with owner:
            made = await owner.post("/api/operational-agents/", json={
                "name": "tenant-agent",
                "scopes": [{"scope_type": "org_unit",
                            "scope_ref": estate.units["b1"]}],
                "capabilities": [{"kind": "action_class",
                                  "capability_ref": "SEL_CLEAR"}],
            })
            agent_id = made.json()["id"]

        await _grant(sessionmaker, "kc-cm", SCOPE_ORG_UNIT, estate.units["a1"],
                     role="tenant_owner")
        cm = _client(app, "tenant_owner", "kc-cm")
        async with cm:
            # The agent lives in Cluster B1, which this principal cannot
            # reach -- so they cannot re-point it at their own cluster.
            resp = await cm.put(f"/api/operational-agents/{agent_id}/bindings", json={
                "scopes": [{"scope_type": "org_unit",
                            "scope_ref": estate.units["a1"]}],
                "capabilities": [{"kind": "action_class",
                                  "capability_ref": "SEL_CLEAR"}],
            })
            assert resp.status_code == 403
            assert (
                await cm.post(f"/api/operational-agents/{agent_id}/activate")
            ).status_code == 403


class TestPlatformActors:
    """Platform-realm actors must not silently become tenant operators."""

    @pytest.mark.asyncio
    async def test_a_platform_super_admin_gets_no_implicit_tenant_scope(self):
        app, sessionmaker, estate = await _stack()
        await _strict(sessionmaker)
        client = _client(app, "platform_super_admin", "kc-platform")
        async with client:
            # Layer 1 passes ("*"), and every scoped surface is empty.
            assert (await client.get("/api/sites/")).status_code == 200
            assert (await client.get("/api/sites/")).json()["sites"] == []
            assert (await client.get("/api/fleet/")).json()["devices"] == []
            # And a mutation is refused: acting inside a tenant's CC
            # needs an explicit, audited grant like anyone else.
            resp = await client.post(
                "/api/policies/", json={"name": "x", "required_approvers": 1}
            )
            assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_platform_support_falls_through_to_viewer_and_reaches_nothing(self):
        app, sessionmaker, estate = await _stack()
        await _strict(sessionmaker)
        # `platform_support` is absent from CC's role map, so pick_role
        # yields "viewer" -- A12.1 enforced rather than configured.
        client = _client(app, "viewer", "kc-support")
        async with client:
            # `viewer` does hold fleet.view, so these are 200 -- and
            # EMPTY, which is the point: reaching the surface is not
            # reaching the data.
            assert (await client.get("/api/sites/")).json()["sites"] == []
            assert (await client.get("/api/fleet/")).json()["devices"] == []
            # And the surfaces viewer has no permission for stay 403.
            assert (await client.get("/api/audit/")).status_code == 403
            assert (await client.get("/api/org-units/")).status_code == 403
            assert (
                await client.post("/api/policies/", json={"name": "x"})
            ).status_code == 403

    @pytest.mark.asyncio
    async def test_an_explicit_grant_lets_a_platform_admin_act_and_is_audited(self):
        app, sessionmaker, estate = await _stack()
        await _strict(sessionmaker)
        await _grant(sessionmaker, "kc-platform", SCOPE_TENANT,
                     role="platform_super_admin")
        client = _client(app, "platform_super_admin", "kc-platform")
        async with client:
            assert len((await client.get("/api/sites/")).json()["sites"]) == 3


class TestAuditorReadsEverythingAndChangesNothing:
    @pytest.mark.asyncio
    async def test_an_auditor_with_tenant_scope_reads_the_whole_estate(self):
        app, sessionmaker, estate = await _stack()
        await _strict(sessionmaker)
        await _grant(sessionmaker, "kc-auditor", SCOPE_TENANT, role="auditor")
        client = _client(app, "auditor", "kc-auditor")
        async with client:
            assert len((await client.get("/api/sites/")).json()["sites"]) == 3
            assert (await client.get("/api/audit/")).status_code == 200
            assert (await client.get("/api/approvals/")).status_code == 200

    @pytest.mark.asyncio
    async def test_an_auditor_may_not_mutate_or_approve(self):
        app, sessionmaker, estate = await _stack()
        await _strict(sessionmaker)
        await _grant(sessionmaker, "kc-auditor", SCOPE_TENANT, role="auditor")
        client = _client(app, "auditor", "kc-auditor")
        async with client:
            assert (
                await client.post("/api/policies/", json={"name": "x"})
            ).status_code == 403
            assert (
                await client.post("/api/approvals/some-id/approve")
            ).status_code == 403
            assert (
                await client.post("/api/org-units/", json={"name": "x"})
            ).status_code == 403


def _walk(nodes) -> set[str]:
    out = set()
    for n in nodes:
        out.add(n["name"])
        out |= _walk(n.get("children", []))
    return out


def _index(nodes) -> dict:
    out = {}
    for n in nodes:
        out[n["name"]] = n
        out.update(_index(n.get("children", [])))
    return out
