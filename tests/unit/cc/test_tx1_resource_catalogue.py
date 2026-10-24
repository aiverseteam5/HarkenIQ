"""TX-1 (spec A31): the Resource Catalogue foundation.

The catalogue DESCRIBES the estate and decides nothing. These tests hold
that to its letter, against the production app and the real mirror:

1. **Vocabulary and primitives** -- closed kinds, the built-in type
   registry, physical-only primary containment with no cycle, no silent
   identity merge.
2. **The mirror** -- one resource per site and per Harken Node, idempotent,
   rename/move/retire/revive keep the id, two-site ambiguity holds
   placement, a planted conflict is reported and never resolved.
3. **Canonical parity, generated** -- for every persona the catalogue's
   active devices are EXACTLY the fleet list's agents, its sites EXACTLY
   the authoritative sites, its tombstones EXACTLY the A30.25 owner rule;
   detail, lookup and relationships agree with the list.
4. **The A31 invariants** -- adding a resource, adding or forging a
   relationship, re-typing a resource and deleting every relationship move
   no canonical read and no catalogue visibility; capabilities, execution
   and authority modules never consult the catalogue; Site Managers and
   Operational Agents are never physical parents; tenants stay apart; a
   machine principal is refused.

Every persona runs under STRICT enforcement through real grants.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import pathlib
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from harkeniq_cc.api import resources as resources_api
from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.attention_projection import DEVICE_CLASSES
from harkeniq_cc.auth import ROLE_PERMISSIONS, UserContext
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db import repos as repos_module
from harkeniq_cc.db.models import (
    CCFleetCache,
    CCOperationalAgent,
    CCResource,
    CCResourceExternalId,
    CCResourceRelationship,
    CCScopeGrant,
    CCSite,
)
from harkeniq_cc.db.repos import ResourceRepo, scope_resources
from harkeniq_cc.machine_identity import SPECIES_AGENT
from harkeniq_cc.resource_catalogue import (
    BUILTIN_TYPES,
    EXTERNAL_SOURCE_HARKENIQ,
    KIND_COMPONENT,
    KIND_LOGICAL_RESOURCE,
    KIND_PHYSICAL_CONTAINER,
    KIND_PHYSICAL_DEVICE,
    LEGACY_DEVICE,
    LEGACY_KINDS,
    LEGACY_SITE,
    LIFECYCLE_ACTIVE,
    LIFECYCLE_RETIRED,
    NAMESPACE_HARKEN_NODE,
    NAMESPACE_SITE,
    REL_CONTAINS,
    REL_HOSTS,
    REL_MANAGED_BY,
    REL_MEMBER_OF,
    RELATIONSHIP_TYPES,
    RESOURCE_KINDS,
    SOURCE_LEGACY_MIRROR,
    TYPE_SERVER,
    TYPE_SITE,
    TYPE_SWITCH,
    TYPE_UNCLASSIFIED_DEVICE,
    CatalogueError,
    IdentityConflict,
    device_type_for_class,
    is_well_formed_type,
    validate_kind,
    validate_lifecycle,
    validate_primary_containment,
    validate_relationship_type,
    validate_type,
)
from harkeniq_cc.resource_mirror import reconcile
from harkeniq_cc.route_contract import MACHINE_SURFACE, ROUTE_CONTRACT, READ_SCOPED
from harkeniq_cc.runtime import make_state, run
from harkeniq_cc.scope import (
    SCOPE_DEVICE,
    SCOPE_DEVICE_CLASS,
    SCOPE_ORG_UNIT,
    SCOPE_SITE,
    SCOPE_TENANT,
)

from tests.unit.cc.test_e1_persona_matrix import (
    TENANT,
    _client,
    _legacy,
    _stack,
    _strict,
)

CC_SRC = (
    pathlib.Path(__file__).resolve().parents[3]
    / "services" / "central_command" / "src" / "harkeniq_cc"
)
RESOURCES = "/api/resources/"
PAST = datetime.now(timezone.utc) - timedelta(days=1)


# ---------------------------------------------------------------------------
# The estate
#
#   site-1 (Cluster A1): node-site-1 (server), node-blank-1 (class ""),
#                        node-dual's first row
#   site-2 (Cluster A1): node-site-2 (server), node-switch-2 (switch)
#   site-3 (Cluster B1): node-site-3 (server), node-dual's second row,
#                        node-moved (moved here from site-1)
#   tombstones:          node-gone-1 (last at site-1), node-gone-3 (site-3)
# ---------------------------------------------------------------------------


async def _reconcile(sessionmaker):
    async with sessionmaker() as session:
        report = await reconcile(session, TENANT)
        await session.commit()
    return report


async def _tx1_stack(*, mirror: bool = True):
    app, sessionmaker, estate = await _stack()
    s1, s2, s3 = (estate.sites[k] for k in ("site-1", "site-2", "site-3"))

    def _dev(site, agent, cls, name=None):
        return CCFleetCache(
            site_id=site, agent_id=agent,
            agent_name=agent if name is None else name,
            vendor="Dell", model="R750", health="ok", device_class=cls,
        )

    async with sessionmaker() as session:
        session.add_all([
            _dev(s2, "node-switch-2", "switch"),
            _dev(s1, "node-blank-1", "", name=""),
            _dev(s1, "node-dual", "server"),
            _dev(s3, "node-dual", "server"),
            _dev(s1, "node-moved", "server"),
            _dev(s1, "node-gone-1", "server"),
            _dev(s3, "node-gone-3", "server"),
        ])
        await session.commit()
    await _strict(sessionmaker)
    if mirror:
        await _reconcile(sessionmaker)
        await _move_and_retire(sessionmaker, estate)
        await _reconcile(sessionmaker)
    return app, sessionmaker, estate


async def _move_and_retire(sessionmaker, estate):
    async with sessionmaker() as session:
        await session.execute(
            update(CCFleetCache)
            .where(CCFleetCache.agent_id == "node-moved")
            .values(site_id=estate.sites["site-3"])
        )
        await session.execute(
            delete(CCFleetCache).where(
                CCFleetCache.agent_id.in_(["node-gone-1", "node-gone-3"])
            )
        )
        await session.commit()


async def _resource(sessionmaker, legacy_kind, ref):
    async with sessionmaker() as session:
        return await ResourceRepo(session).get_by_legacy(TENANT, legacy_kind, ref)


async def _lapse(sessionmaker, grant_id, *, how):
    async with sessionmaker() as session:
        row = await session.get(CCScopeGrant, grant_id)
        if how == "expired":
            row.expires_at = PAST
        else:
            row.revoked_at = PAST
        await session.commit()


async def _grant_row(sessionmaker, principal, scope_type, ref="", role="viewer",
                     subset=None):
    from harkeniq_cc.db.repos import ScopeGrantRepo

    async with sessionmaker() as session:
        row = await ScopeGrantRepo(session).grant(
            tenant_id=TENANT, principal_type="user", principal_ref=principal,
            scope_type=scope_type, scope_ref=ref, permission_subset=subset,
            role=role, granted_by="tx1-test",
        )
        await session.commit()
        return row.id


#: persona -> list of (scope_type, ref key, role, subset, lapse)
#: `ref key` is a site key, an org-unit key, or a literal device/class ref.
PERSONAS = {
    "owner":          [(SCOPE_TENANT, "", "tenant_owner", None, None)],
    "region_a":       [(SCOPE_ORG_UNIT, "region_a", "site_admin", None, None)],
    "cluster_b1":     [(SCOPE_ORG_UNIT, "b1", "site_admin", None, None)],
    "site_1":         [(SCOPE_SITE, "site-1", "operator", None, None)],
    "device_3":       [(SCOPE_DEVICE, "node-site-3", "viewer", None, None)],
    "class_switch":   [(SCOPE_DEVICE_CLASS, "switch", "viewer", None, None)],
    "mixed":          [(SCOPE_SITE, "site-1", "viewer", None, None),
                       (SCOPE_DEVICE, "node-site-3", "viewer", None, None)],
    "narrowed":       [(SCOPE_SITE, "site-1", "auditor", ["incident.view"], None)],
    "narrowed_mixed": [(SCOPE_SITE, "site-1", "auditor", None, None),
                       (SCOPE_SITE, "site-3", "auditor", ["incident.view"], None)],
    "revoked":        [(SCOPE_SITE, "site-3", "site_admin", None, "revoked")],
    "expired":        [(SCOPE_SITE, "site-3", "site_admin", None, "expired")],
    "gone_device":    [(SCOPE_DEVICE, "node-gone-3", "viewer", None, None)],
    "ungranted":      [],
}

#: role the persona's token carries
TOKEN_ROLE = {
    "owner": "tenant_owner", "region_a": "site_admin", "cluster_b1": "site_admin",
    "site_1": "operator", "device_3": "viewer", "class_switch": "viewer",
    "mixed": "viewer", "narrowed": "auditor", "narrowed_mixed": "auditor",
    "revoked": "site_admin", "expired": "site_admin", "gone_device": "viewer",
    "ungranted": "viewer",
}


async def _as(app, sessionmaker, estate, persona):
    user_id = f"kc-tx1-{persona}"
    for scope_type, key, role, subset, lapse in PERSONAS[persona]:
        ref = estate.sites.get(key) or estate.units.get(key) or key
        grant_id = await _grant_row(
            sessionmaker, user_id, scope_type, ref, role=role, subset=subset
        )
        if lapse:
            await _lapse(sessionmaker, grant_id, how=lapse)
    return _client(app, TOKEN_ROLE[persona], user_id)


async def _pages(client, path, key, **params):
    out, page = [], 1
    while True:
        res = await client.get(path, params={**params, "page": page, "page_size": 200})
        assert res.status_code == 200, (path, res.status_code, res.text)
        body = res.json()
        out.extend(body[key])
        if len(body[key]) < 200:
            assert body.get("total", len(out)) == len(out), (path, body.get("total"))
            return out
        page += 1


async def _catalogue(client, *, lifecycle="active", kind=None):
    params = {"lifecycle_state": lifecycle}
    if kind:
        params["kind"] = kind
    return await _pages(client, RESOURCES, "resources", **params)


async def _views(client):
    """What a reader sees through the canonical reads and the catalogue."""
    fleet = {d["agent_id"] for d in await _pages(client, "/api/fleet/", "devices")}
    sites_body = (await client.get("/api/sites/", params={"page_size": 200})).json()
    authoritative = {s["id"] for s in sites_body["sites"] if not s.get("contextual")}
    devices = await _catalogue(client, kind=KIND_PHYSICAL_DEVICE)
    containers = await _catalogue(client, kind=KIND_PHYSICAL_CONTAINER)
    return {
        "fleet": fleet,
        "sites": authoritative,
        "cat_devices": {r["legacy"]["ref"] for r in devices},
        "cat_sites": {r["legacy"]["ref"] for r in containers
                      if r["resource_type"] == TYPE_SITE},
    }


# ---------------------------------------------------------------------------
# 1. Vocabulary
# ---------------------------------------------------------------------------


class TestVocabulary:
    def test_the_kinds_are_the_four_ratified_and_closed(self):
        assert RESOURCE_KINDS == {
            "physical_container", "physical_device", "component", "logical_resource",
        }
        with pytest.raises(CatalogueError):
            validate_kind("rack")
        assert validate_kind(KIND_COMPONENT) == KIND_COMPONENT

    def test_the_query_filter_is_exactly_the_kind_vocabulary(self):
        import typing

        assert set(typing.get_args(resources_api.ResourceKindFilter)) == set(RESOURCE_KINDS)

    def test_lifecycle_is_active_or_retired(self):
        assert validate_lifecycle(LIFECYCLE_ACTIVE) == "active"
        with pytest.raises(CatalogueError):
            validate_lifecycle("deleted")

    def test_relationship_types_are_the_six(self):
        assert RELATIONSHIP_TYPES == {
            "CONTAINS", "HOSTS", "RUNS_ON", "CONNECTED_TO", "MEMBER_OF", "MANAGED_BY",
        }
        with pytest.raises(CatalogueError):
            validate_relationship_type("PARENT_OF")

    def test_every_built_in_type_is_namespaced_and_reserved(self):
        for resource_type, kind in BUILTIN_TYPES.items():
            assert is_well_formed_type(resource_type), resource_type
            assert resource_type.startswith("harkeniq:")
            assert validate_type(resource_type, kind) == resource_type
        # Exactly what the mirror writes -- nothing declared without a writer.
        assert set(BUILTIN_TYPES) == {
            TYPE_SITE, TYPE_SERVER, TYPE_SWITCH, TYPE_UNCLASSIFIED_DEVICE,
        }

    def test_a_type_outside_the_registry_is_refused(self):
        for bad in ("harkeniq:compute.gpu_server", "acme:compute.blade",
                    "rack", "harkeniq:", "Harkeniq:compute.server",
                    "harkeniq:compute", "x" * 129):
            with pytest.raises(CatalogueError):
                validate_type(bad, KIND_PHYSICAL_DEVICE)

    def test_a_type_of_another_kind_is_refused(self):
        with pytest.raises(CatalogueError):
            validate_type(TYPE_SITE, KIND_PHYSICAL_DEVICE)
        with pytest.raises(CatalogueError):
            validate_type(TYPE_SERVER, KIND_PHYSICAL_CONTAINER)

    def test_a_blank_class_is_never_inferred_to_be_a_server(self):
        assert device_type_for_class("server") == TYPE_SERVER
        assert device_type_for_class("SWITCH") == TYPE_SWITCH
        for blank in ("", None, "   ", "storage", "gpu"):
            assert device_type_for_class(blank) == TYPE_UNCLASSIFIED_DEVICE

    def test_primary_containment_is_physical(self):
        validate_primary_containment(KIND_PHYSICAL_CONTAINER, KIND_PHYSICAL_DEVICE)
        validate_primary_containment(KIND_PHYSICAL_DEVICE, KIND_COMPONENT)
        for parent in (KIND_LOGICAL_RESOURCE, KIND_COMPONENT):
            with pytest.raises(CatalogueError):
                validate_primary_containment(parent, KIND_PHYSICAL_DEVICE)
        with pytest.raises(CatalogueError):
            validate_primary_containment(KIND_PHYSICAL_CONTAINER, KIND_LOGICAL_RESOURCE)

    def test_rack_is_a_recorded_contract_not_a_known_legacy_kind(self):
        """A31.3: rack ingest is TX-2's. Nothing writes it, so nothing may
        treat it as known."""
        assert LEGACY_KINDS == {LEGACY_SITE, LEGACY_DEVICE}
        assert not any("rack" in t for t in BUILTIN_TYPES)


# ---------------------------------------------------------------------------
# 2. Repository primitives
# ---------------------------------------------------------------------------


async def _bare():
    app, sessionmaker, estate = await _stack()
    return sessionmaker, estate


async def _insert(session, kind, rtype="x:y.z", tenant=TENANT, legacy=None):
    """A fixture row the primitives must handle -- written directly, because
    TX-1 has no writer for an unanchored or non-physical resource."""
    row = CCResource(
        tenant_id=tenant, resource_kind=kind, resource_type=rtype,
        display_name=f"{kind}-fixture", lifecycle_state=LIFECYCLE_ACTIVE,
        source="test-fixture",
        legacy_kind=legacy[0] if legacy else None,
        legacy_ref=legacy[1] if legacy else None,
        legacy_site_id=legacy[2] if legacy else None,
    )
    session.add(row)
    await session.flush()
    return row


class TestPrimitives:
    async def test_create_mirrored_validates(self):
        sessionmaker, estate = await _bare()
        async with sessionmaker() as session:
            repo = ResourceRepo(session)
            with pytest.raises(CatalogueError):
                await repo.create_mirrored(
                    TENANT, legacy_kind="rack", legacy_ref="r1", legacy_site_id=None,
                    resource_kind=KIND_PHYSICAL_CONTAINER, resource_type=TYPE_SITE,
                    display_name="r",
                )
            with pytest.raises(CatalogueError):
                await repo.create_mirrored(
                    TENANT, legacy_kind=LEGACY_SITE, legacy_ref="", legacy_site_id=None,
                    resource_kind=KIND_PHYSICAL_CONTAINER, resource_type=TYPE_SITE,
                    display_name="r",
                )
            with pytest.raises(CatalogueError):
                await repo.create_mirrored(
                    TENANT, legacy_kind=LEGACY_DEVICE, legacy_ref="n", legacy_site_id=None,
                    resource_kind=KIND_PHYSICAL_DEVICE, resource_type=TYPE_SITE,
                    display_name="n",
                )

    async def test_one_resource_per_legacy_object_is_a_database_guarantee(self):
        sessionmaker, estate = await _bare()
        async with sessionmaker() as session:
            await _insert(session, KIND_PHYSICAL_DEVICE, TYPE_SERVER,
                          legacy=(LEGACY_DEVICE, "dup", None))
            with pytest.raises(IntegrityError):
                await _insert(session, KIND_PHYSICAL_DEVICE, TYPE_SERVER,
                              legacy=(LEGACY_DEVICE, "dup", None))

    async def test_primary_parent_sets_repoints_and_is_idempotent(self):
        sessionmaker, estate = await _bare()
        async with sessionmaker() as session:
            repo = ResourceRepo(session)
            a = await _insert(session, KIND_PHYSICAL_CONTAINER, TYPE_SITE)
            b = await _insert(session, KIND_PHYSICAL_CONTAINER, TYPE_SITE)
            dev = await _insert(session, KIND_PHYSICAL_DEVICE, TYPE_SERVER)
            first = await repo.set_primary_parent(TENANT, dev.id, a.id, source="s")
            again = await repo.set_primary_parent(TENANT, dev.id, a.id, source="s")
            assert again.id == first.id
            moved = await repo.set_primary_parent(TENANT, dev.id, b.id, source="s")
            assert moved.id == first.id and moved.from_resource_id == b.id
            edges = (await session.execute(select(CCResourceRelationship))).scalars().all()
            assert len(edges) == 1 and edges[0].is_primary

    async def test_a_second_primary_parent_is_refused_by_the_database(self):
        sessionmaker, estate = await _bare()
        async with sessionmaker() as session:
            a = await _insert(session, KIND_PHYSICAL_CONTAINER, TYPE_SITE)
            b = await _insert(session, KIND_PHYSICAL_CONTAINER, TYPE_SITE)
            dev = await _insert(session, KIND_PHYSICAL_DEVICE, TYPE_SERVER)
            for parent in (a, b):
                session.add(CCResourceRelationship(
                    tenant_id=TENANT, from_resource_id=parent.id,
                    to_resource_id=dev.id, relationship_type=REL_CONTAINS,
                    is_primary=True, source="raw",
                ))
            with pytest.raises(IntegrityError):
                await session.flush()

    async def test_secondary_edges_do_not_count_against_the_primary(self):
        sessionmaker, estate = await _bare()
        async with sessionmaker() as session:
            repo = ResourceRepo(session)
            a = await _insert(session, KIND_PHYSICAL_CONTAINER, TYPE_SITE)
            b = await _insert(session, KIND_PHYSICAL_CONTAINER, TYPE_SITE)
            dev = await _insert(session, KIND_PHYSICAL_DEVICE, TYPE_SERVER)
            await repo.set_primary_parent(TENANT, dev.id, a.id, source="s")
            edge = await repo.add_relationship(TENANT, b.id, dev.id, REL_CONTAINS, source="s")
            assert edge.is_primary is False
            again = await repo.add_relationship(TENANT, b.id, dev.id, REL_CONTAINS, source="s")
            assert again.id == edge.id
            # A secondary CONTAINS from the would-be parent blocks re-pointing
            # onto it: the pair cannot carry two CONTAINS edges.
            with pytest.raises(CatalogueError):
                await repo.set_primary_parent(TENANT, dev.id, b.id, source="s")

    async def test_self_cross_tenant_and_unknown_type_edges_are_refused(self):
        sessionmaker, estate = await _bare()
        async with sessionmaker() as session:
            repo = ResourceRepo(session)
            a = await _insert(session, KIND_PHYSICAL_CONTAINER, TYPE_SITE)
            foreign = await _insert(session, KIND_PHYSICAL_DEVICE, TYPE_SERVER,
                                    tenant="other-tenant")
            with pytest.raises(CatalogueError):
                await repo.add_relationship(TENANT, a.id, a.id, REL_HOSTS, source="s")
            with pytest.raises(CatalogueError):
                await repo.add_relationship(TENANT, a.id, foreign.id, REL_HOSTS, source="s")
            with pytest.raises(CatalogueError):
                await repo.set_primary_parent(TENANT, foreign.id, a.id, source="s")
            with pytest.raises(CatalogueError):
                await repo.add_relationship(TENANT, a.id, a.id, "OWNS", source="s")

    async def test_a_containment_cycle_is_refused(self):
        sessionmaker, estate = await _bare()
        async with sessionmaker() as session:
            repo = ResourceRepo(session)
            outer = await _insert(session, KIND_PHYSICAL_CONTAINER, TYPE_SITE)
            inner = await _insert(session, KIND_PHYSICAL_CONTAINER, TYPE_SITE)
            await repo.set_primary_parent(TENANT, inner.id, outer.id, source="s")
            with pytest.raises(CatalogueError, match="cycle"):
                await repo.set_primary_parent(TENANT, outer.id, inner.id, source="s")

    async def test_a_parent_from_another_source_is_never_replaced(self):
        sessionmaker, estate = await _bare()
        async with sessionmaker() as session:
            repo = ResourceRepo(session)
            a = await _insert(session, KIND_PHYSICAL_CONTAINER, TYPE_SITE)
            b = await _insert(session, KIND_PHYSICAL_CONTAINER, TYPE_SITE)
            dev = await _insert(session, KIND_PHYSICAL_DEVICE, TYPE_SERVER)
            await repo.set_primary_parent(TENANT, dev.id, a.id, source="operator")
            with pytest.raises(CatalogueError, match="may not replace"):
                await repo.set_primary_parent(
                    TENANT, dev.id, b.id, source=SOURCE_LEGACY_MIRROR
                )

    async def test_an_identifier_is_never_silently_merged(self):
        sessionmaker, estate = await _bare()
        async with sessionmaker() as session:
            repo = ResourceRepo(session)
            a = await _insert(session, KIND_PHYSICAL_DEVICE, TYPE_SERVER)
            b = await _insert(session, KIND_PHYSICAL_DEVICE, TYPE_SERVER)
            kw = dict(source_system="cmdb", namespace="sys_id", external_id="X1",
                      source="s")
            first = await repo.bind_external_id(TENANT, a.id, **kw)
            assert (await repo.bind_external_id(TENANT, a.id, **kw)).id == first.id
            with pytest.raises(IdentityConflict):
                await repo.bind_external_id(TENANT, b.id, **kw)
            rows = (await session.execute(select(CCResourceExternalId))).scalars().all()
            assert [(r.resource_id, r.external_id) for r in rows] == [(a.id, "X1")]
            with pytest.raises(CatalogueError):
                await repo.bind_external_id(
                    TENANT, a.id, source_system="", namespace="n",
                    external_id="v", source="s",
                )


# ---------------------------------------------------------------------------
# 3. The mirror
# ---------------------------------------------------------------------------


class TestMirror:
    async def test_one_resource_per_site_and_per_node(self):
        app, sessionmaker, estate = await _tx1_stack()
        async with sessionmaker() as session:
            sites = (await session.execute(
                select(CCResource).where(CCResource.legacy_kind == LEGACY_SITE)
            )).scalars().all()
            devices = (await session.execute(
                select(CCResource).where(CCResource.legacy_kind == LEGACY_DEVICE)
            )).scalars().all()
        assert {r.legacy_ref for r in sites} == set(estate.sites.values())
        assert {r.legacy_ref for r in devices} == {
            "node-site-1", "node-site-2", "node-site-3", "node-switch-2",
            "node-blank-1", "node-dual", "node-moved", "node-gone-1", "node-gone-3",
        }
        by_ref = {r.legacy_ref: r for r in devices}
        assert by_ref["node-switch-2"].resource_type == TYPE_SWITCH
        assert by_ref["node-site-1"].resource_type == TYPE_SERVER
        assert by_ref["node-blank-1"].resource_type == TYPE_UNCLASSIFIED_DEVICE
        # A blank agent name reads as the node id, never as nothing.
        assert by_ref["node-blank-1"].display_name == "node-blank-1"
        for row in sites:
            assert row.resource_kind == KIND_PHYSICAL_CONTAINER
            assert row.resource_type == TYPE_SITE
            assert row.source == SOURCE_LEGACY_MIRROR

    async def test_external_ids_are_harkeniq_issued_and_bound_once(self):
        app, sessionmaker, estate = await _tx1_stack()
        async with sessionmaker() as session:
            rows = (await session.execute(select(CCResourceExternalId))).scalars().all()
        assert {r.source_system for r in rows} == {EXTERNAL_SOURCE_HARKENIQ}
        assert {r.namespace for r in rows} == {NAMESPACE_SITE, NAMESPACE_HARKEN_NODE}
        assert len({(r.namespace, r.external_id) for r in rows}) == len(rows)
        assert len(rows) == 3 + 9

    async def test_a_second_pass_writes_nothing(self):
        app, sessionmaker, estate = await _tx1_stack()
        async with sessionmaker() as session:
            before = {r.id: r.updated_at for r in
                      (await session.execute(select(CCResource))).scalars().all()}
        report = await _reconcile(sessionmaker)
        assert not report.changed and report.conflicts == 0
        async with sessionmaker() as session:
            after = {r.id: r.updated_at for r in
                     (await session.execute(select(CCResource))).scalars().all()}
        assert after == before

    async def test_devices_hang_directly_off_their_site(self):
        app, sessionmaker, estate = await _tx1_stack()
        site1 = await _resource(sessionmaker, LEGACY_SITE, estate.sites["site-1"])
        site3 = await _resource(sessionmaker, LEGACY_SITE, estate.sites["site-3"])
        node1 = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-1")
        moved = await _resource(sessionmaker, LEGACY_DEVICE, "node-moved")
        async with sessionmaker() as session:
            repo = ResourceRepo(session)
            assert (await repo.primary_parent_edge(node1.id)).from_resource_id == site1.id
            # Moved: the SAME resource, re-placed, its anchor following it.
            edge = await repo.primary_parent_edge(moved.id)
            assert edge.from_resource_id == site3.id
            assert (await repo.get(TENANT, moved.id)).legacy_site_id == estate.sites["site-3"]
            # A site has no primary parent: the org tree is administrative.
            assert await repo.primary_parent_edge(site1.id) is None

    async def test_a_device_at_two_sites_holds_its_placement(self):
        app, sessionmaker, estate = await _tx1_stack()
        dual = await _resource(sessionmaker, LEGACY_DEVICE, "node-dual")
        async with sessionmaker() as session:
            # First seen mid-move: no placement and no anchor yet.
            assert await ResourceRepo(session).primary_parent_edge(dual.id) is None
        assert dual.legacy_site_id is None
        # It settles at site-3: placed there.
        async with sessionmaker() as session:
            await session.execute(delete(CCFleetCache).where(
                CCFleetCache.agent_id == "node-dual",
                CCFleetCache.site_id == estate.sites["site-1"],
            ))
            await session.commit()
        await _reconcile(sessionmaker)
        site3 = await _resource(sessionmaker, LEGACY_SITE, estate.sites["site-3"])
        async with sessionmaker() as session:
            edge = await ResourceRepo(session).primary_parent_edge(dual.id)
        assert edge.from_resource_id == site3.id
        # Back at two sites: the placement it had is HELD, not flapped.
        async with sessionmaker() as session:
            session.add(CCFleetCache(
                site_id=estate.sites["site-1"], agent_id="node-dual",
                agent_name="node-dual", device_class="server",
            ))
            await session.commit()
        report = await _reconcile(sessionmaker)
        assert report.ambiguous == 1
        async with sessionmaker() as session:
            edge = await ResourceRepo(session).primary_parent_edge(dual.id)
            assert edge.from_resource_id == site3.id
            assert (await ResourceRepo(session).get(TENANT, dual.id)).legacy_site_id \
                == estate.sites["site-3"]

    async def test_a_vanished_device_is_a_tombstone_and_revives_as_itself(self):
        app, sessionmaker, estate = await _tx1_stack()
        gone = await _resource(sessionmaker, LEGACY_DEVICE, "node-gone-3")
        site3 = await _resource(sessionmaker, LEGACY_SITE, estate.sites["site-3"])
        assert gone.lifecycle_state == LIFECYCLE_RETIRED and gone.retired_at
        assert gone.legacy_site_id == estate.sites["site-3"]
        async with sessionmaker() as session:
            edge = await ResourceRepo(session).primary_parent_edge(gone.id)
        assert edge.from_resource_id == site3.id, "the last placement is kept"
        async with sessionmaker() as session:
            session.add(CCFleetCache(
                site_id=estate.sites["site-2"], agent_id="node-gone-3",
                agent_name="node-gone-3", device_class="server",
            ))
            await session.commit()
        report = await _reconcile(sessionmaker)
        assert report.revived == 1
        back = await _resource(sessionmaker, LEGACY_DEVICE, "node-gone-3")
        site2 = await _resource(sessionmaker, LEGACY_SITE, estate.sites["site-2"])
        assert back.id == gone.id and back.lifecycle_state == LIFECYCLE_ACTIVE
        assert back.retired_at is None
        async with sessionmaker() as session:
            edge = await ResourceRepo(session).primary_parent_edge(back.id)
        assert edge.from_resource_id == site2.id

    async def test_a_renamed_site_keeps_its_id(self):
        app, sessionmaker, estate = await _tx1_stack()
        before = await _resource(sessionmaker, LEGACY_SITE, estate.sites["site-2"])
        async with sessionmaker() as session:
            site = await session.get(CCSite, estate.sites["site-2"])
            site.site_name = "Site Two (renamed)"
            await session.commit()
        await _reconcile(sessionmaker)
        after = await _resource(sessionmaker, LEGACY_SITE, estate.sites["site-2"])
        assert after.id == before.id and after.display_name == "Site Two (renamed)"

    async def test_a_vanished_site_is_retired_never_deleted(self):
        app, sessionmaker, estate = await _tx1_stack()
        s2 = estate.sites["site-2"]
        before = await _resource(sessionmaker, LEGACY_SITE, s2)
        async with sessionmaker() as session:
            await session.execute(delete(CCFleetCache).where(CCFleetCache.site_id == s2))
            from harkeniq_cc.db.models import CCIncident

            await session.execute(delete(CCIncident).where(CCIncident.site_id == s2))
            await session.execute(delete(CCSite).where(CCSite.id == s2))
            await session.commit()
        await _reconcile(sessionmaker)
        after = await _resource(sessionmaker, LEGACY_SITE, s2)
        assert after.id == before.id and after.lifecycle_state == LIFECYCLE_RETIRED
        for agent in ("node-site-2", "node-switch-2"):
            assert (await _resource(sessionmaker, LEGACY_DEVICE, agent)).lifecycle_state \
                == LIFECYCLE_RETIRED

    async def test_a_planted_conflict_is_reported_and_never_resolved(self):
        app, sessionmaker, estate = await _tx1_stack(mirror=False)
        async with sessionmaker() as session:
            squatter = await _insert(session, KIND_PHYSICAL_DEVICE, TYPE_SERVER)
            session.add(CCResourceExternalId(
                tenant_id=TENANT, resource_id=squatter.id,
                source_system=EXTERNAL_SOURCE_HARKENIQ,
                namespace=NAMESPACE_HARKEN_NODE, external_id="node-site-1",
                source="planted",
            ))
            await session.commit()
        report = await _reconcile(sessionmaker)
        assert report.conflicts == 1
        node1 = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-1")
        async with sessionmaker() as session:
            row = (await session.execute(select(CCResourceExternalId).where(
                CCResourceExternalId.external_id == "node-site-1"
            ))).scalar_one()
        assert row.resource_id == squatter.id != node1.id

    async def test_the_mirror_never_makes_an_operational_agent_a_resource(self):
        app, sessionmaker, estate = await _tx1_stack(mirror=False)
        async with sessionmaker() as session:
            session.add(CCOperationalAgent(tenant_id=TENANT, name="op-agent-tx1"))
            await session.commit()
            agent_id = (await session.execute(select(CCOperationalAgent.id))).scalar_one()
        await _reconcile(sessionmaker)
        async with sessionmaker() as session:
            rows = (await session.execute(select(CCResource))).scalars().all()
        assert {r.legacy_kind for r in rows} <= LEGACY_KINDS
        assert agent_id not in {r.legacy_ref for r in rows}
        assert not any("agent" in r.resource_type or "manager" in r.resource_type
                       for r in rows)


# ---------------------------------------------------------------------------
# 4. Canonical parity, generated over every persona
# ---------------------------------------------------------------------------


def _tombstone_oracle(views, estate):
    """Retired devices a reader may see: the A30.25 owner rule, R9 --
    site-owned at the last site, i.e. visible exactly when that site is an
    AUTHORITATIVE site of theirs."""
    last = {"node-gone-1": estate.sites["site-1"], "node-gone-3": estate.sites["site-3"]}
    return {agent for agent, site in last.items() if site in views["sites"]}


class TestCanonicalParity:
    @pytest.mark.parametrize("persona", sorted(PERSONAS))
    async def test_the_catalogue_sees_exactly_what_the_canonical_reads_see(self, persona):
        app, sessionmaker, estate = await _tx1_stack()
        client = await _as(app, sessionmaker, estate, persona)
        async with client:
            views = await _views(client)
            retired = await _catalogue(client, lifecycle="retired")
        assert views["cat_devices"] == views["fleet"], persona
        assert views["cat_sites"] == views["sites"], persona
        assert {r["legacy"]["ref"] for r in retired} == _tombstone_oracle(views, estate)

    async def test_the_matrix_is_not_vacuous(self):
        """Parity over empty answers proves nothing: the personas must see
        DIFFERENT, non-empty estates, and the expected ones."""
        app, sessionmaker, estate = await _tx1_stack()
        seen = {}
        for persona in ("owner", "region_a", "cluster_b1", "site_1", "device_3",
                        "class_switch", "mixed", "narrowed", "ungranted"):
            async with await _as(app, sessionmaker, estate, persona) as client:
                seen[persona] = (await _views(client))["cat_devices"]
        assert seen["owner"] == {
            "node-site-1", "node-site-2", "node-site-3", "node-switch-2",
            "node-blank-1", "node-dual", "node-moved",
        }
        assert seen["region_a"] == {
            "node-site-1", "node-site-2", "node-switch-2", "node-blank-1", "node-dual",
        }
        assert seen["cluster_b1"] == {"node-site-3", "node-dual", "node-moved"}
        assert seen["site_1"] == {"node-site-1", "node-blank-1", "node-dual"}
        assert seen["device_3"] == {"node-site-3"}
        assert seen["class_switch"] == {"node-switch-2"}
        assert seen["mixed"] == {"node-site-1", "node-blank-1", "node-dual", "node-site-3"}
        assert seen["narrowed"] == set() and seen["ungranted"] == set()

    async def test_a_device_grant_reads_no_site_and_no_contextual_site(self):
        """A31.4: the contextual projection of A30.25 D2 is not extended."""
        app, sessionmaker, estate = await _tx1_stack()
        async with await _as(app, sessionmaker, estate, "device_3") as client:
            sites = (await client.get("/api/sites/")).json()["sites"]
            assert [s for s in sites if s.get("contextual")], "D2 still shows context"
            containers = await _catalogue(client, kind=KIND_PHYSICAL_CONTAINER)
            assert containers == []
            node3 = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-3")
            detail = (await client.get(f"{RESOURCES}{node3.id}")).json()
        assert detail["primary_parent"] is None

    async def test_a_device_grant_on_a_vanished_device_confers_nothing(self):
        app, sessionmaker, estate = await _tx1_stack()
        gone = await _resource(sessionmaker, LEGACY_DEVICE, "node-gone-3")
        async with await _as(app, sessionmaker, estate, "gone_device") as client:
            assert (await client.get(f"{RESOURCES}{gone.id}")).status_code == 404
            assert await _catalogue(client, lifecycle="any") == []

    @pytest.mark.parametrize("persona", ["owner", "region_a", "site_1", "device_3",
                                         "class_switch", "mixed", "ungranted"])
    async def test_detail_lookup_and_relationships_agree_with_the_list(self, persona):
        app, sessionmaker, estate = await _tx1_stack()
        async with sessionmaker() as session:
            everything = (await session.execute(select(CCResource))).scalars().all()
            edges = (await session.execute(select(CCResourceRelationship))).scalars().all()
        async with await _as(app, sessionmaker, estate, persona) as client:
            visible = {r["id"] for r in await _catalogue(client, lifecycle="any")}
            for row in everything:
                res = await client.get(f"{RESOURCES}{row.id}")
                assert res.status_code == (200 if row.id in visible else 404), (
                    persona, row.legacy_ref, res.status_code)
                rel = await client.get(f"{RESOURCES}{row.id}/relationships")
                assert rel.status_code == (200 if row.id in visible else 404)
                if row.id not in visible:
                    continue
                body = rel.json()
                shown = {e["id"] for e in body["relationships"]}
                expected = {
                    e.id for e in edges
                    if row.id in (e.from_resource_id, e.to_resource_id)
                    and ({e.from_resource_id, e.to_resource_id} - {row.id}) <= visible
                }
                assert shown == expected and body["total"] == len(expected)
                for edge in body["relationships"]:
                    assert edge["resource"]["id"] in visible
                parent = res.json()["primary_parent"]
                assert parent is None or parent["id"] in visible
            for agent in ("node-site-1", "node-site-3", "node-gone-3", "no-such-node"):
                body = (await client.get(RESOURCES, params={
                    "source_system": EXTERNAL_SOURCE_HARKENIQ,
                    "namespace": NAMESPACE_HARKEN_NODE, "external_id": agent,
                    "lifecycle_state": "any",
                })).json()
                found = {r["id"] for r in body["resources"]}
                assert found <= visible and body["total"] == len(found)
                owner_row = next((r for r in everything if r.legacy_ref == agent), None)
                if owner_row is not None and owner_row.id in visible:
                    assert found == {owner_row.id}
                else:
                    assert found == set()

    async def test_hidden_and_nonexistent_read_alike(self):
        app, sessionmaker, estate = await _tx1_stack()
        node3 = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-3")
        async with await _as(app, sessionmaker, estate, "site_1") as client:
            hidden = await client.get(f"{RESOURCES}{node3.id}")
            missing = await client.get(f"{RESOURCES}{'0' * 32}")
            hidden_rel = await client.get(f"{RESOURCES}{node3.id}/relationships")
        assert hidden.status_code == missing.status_code == hidden_rel.status_code == 404
        assert hidden.json() == missing.json()

    async def test_the_legacy_open_never_granted_reader_sees_the_tenant(self):
        """`legacy_open` synthesis is the canonical resolver's answer, and the
        catalogue follows it exactly -- no wider, no narrower."""
        app, sessionmaker, estate = await _tx1_stack()
        await _legacy(sessionmaker)
        async with _client(app, "viewer", "kc-tx1-never-granted") as client:
            views = await _views(client)
        assert views["cat_devices"] == views["fleet"] and views["fleet"]
        assert views["cat_sites"] == views["sites"] == set(estate.sites.values())


# ---------------------------------------------------------------------------
# 5. The A31 invariants
# ---------------------------------------------------------------------------

#: The canonical reads whose answers must not move.
CANONICAL_READS = (
    "/api/fleet/?page_size=200",
    "/api/fleet/summary",
    "/api/sites/?page_size=200",
    "/api/incidents/",
    "/api/scope-grants/me",
    "/api/capabilities/",
)
INVARIANT_PERSONAS = ("owner", "region_a", "site_1", "device_3", "class_switch",
                      "mixed", "narrowed", "ungranted")


async def _canonical(app, sessionmaker, estate, personas=INVARIANT_PERSONAS):
    out = {}
    for persona in personas:
        user_id = f"kc-tx1-{persona}"
        async with _client(app, TOKEN_ROLE[persona], user_id) as client:
            for path in CANONICAL_READS:
                res = await client.get(path)
                out[(persona, path)] = (res.status_code, res.text)
    return out


async def _catalogue_sets(app, personas=INVARIANT_PERSONAS):
    out = {}
    for persona in personas:
        async with _client(app, TOKEN_ROLE[persona], f"kc-tx1-{persona}") as client:
            out[persona] = {r["id"] for r in await _catalogue(client, lifecycle="any")}
    return out


async def _grant_all(app, sessionmaker, estate, personas=INVARIANT_PERSONAS):
    for persona in personas:
        client = await _as(app, sessionmaker, estate, persona)
        await client.aclose()


class TestInvariants:
    async def test_adding_resources_grants_nothing(self):
        """The same fleet and sites, with the catalogue populated (moves and
        tombstones included) and with it absent: every canonical read, for
        every persona, is byte-identical -- including `/api/scope-grants/me`,
        the resolved scope itself."""
        app, sessionmaker, estate = await _tx1_stack()
        await _grant_all(app, sessionmaker, estate)
        populated = await _canonical(app, sessionmaker, estate)
        async with sessionmaker() as session:
            assert (await session.execute(select(CCResource))).scalars().first()
            for model in (CCResourceExternalId, CCResourceRelationship, CCResource):
                await session.execute(delete(model))
            await session.commit()
        absent = await _canonical(app, sessionmaker, estate)
        assert populated == absent
        await _reconcile(sessionmaker)
        assert await _canonical(app, sessionmaker, estate) == absent

    async def test_relationships_forged_or_deleted_move_nothing(self):
        app, sessionmaker, estate = await _tx1_stack()
        await _grant_all(app, sessionmaker, estate)
        canonical = await _canonical(app, sessionmaker, estate)
        visible = await _catalogue_sets(app)

        site1 = await _resource(sessionmaker, LEGACY_SITE, estate.sites["site-1"])
        site3 = await _resource(sessionmaker, LEGACY_SITE, estate.sites["site-3"])
        node1 = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-1")
        node3 = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-3")
        async with sessionmaker() as session:
            repo = ResourceRepo(session)
            # A topology LIE: node-site-3 "inside" site-1, site-1 "inside"
            # site-3, and every typed edge between the two estates.
            await session.execute(
                update(CCResourceRelationship)
                .where(CCResourceRelationship.to_resource_id == node3.id)
                .values(from_resource_id=site1.id)
            )
            await repo.add_relationship(TENANT, site3.id, node1.id, REL_CONTAINS, source="lie")
            await repo.add_relationship(TENANT, node3.id, site1.id, REL_MEMBER_OF, source="lie")
            await repo.add_relationship(TENANT, node1.id, node3.id, REL_HOSTS, source="lie")
            await repo.add_relationship(TENANT, site1.id, site3.id, REL_MANAGED_BY, source="lie")
            await session.commit()
        assert await _canonical(app, sessionmaker, estate) == canonical
        assert await _catalogue_sets(app) == visible
        async with _client(app, TOKEN_ROLE["site_1"], "kc-tx1-site_1") as client:
            # node-site-3's forged parent is site-1, and still site-1's reader
            # cannot see it -- nor any forged edge reaching into site-3.
            assert (await client.get(f"{RESOURCES}{node3.id}")).status_code == 404
            rels = (await client.get(f"{RESOURCES}{site1.id}/relationships")).json()
            others = {e["resource"]["id"] for e in rels["relationships"]}
            assert node3.id not in others and site3.id not in others

        async with sessionmaker() as session:
            await session.execute(delete(CCResourceRelationship))
            await session.commit()
        assert await _canonical(app, sessionmaker, estate) == canonical
        assert await _catalogue_sets(app) == visible

    async def test_retyping_every_resource_moves_nothing(self):
        """resource_type is not an authorization input, and device_class is
        untouched: re-typing every device a switch changes no visibility --
        the class-switch reader still sees exactly the switch fleet."""
        app, sessionmaker, estate = await _tx1_stack()
        await _grant_all(app, sessionmaker, estate)
        canonical = await _canonical(app, sessionmaker, estate)
        visible = await _catalogue_sets(app)
        async with sessionmaker() as session:
            await session.execute(
                update(CCResource)
                .where(CCResource.legacy_kind == LEGACY_DEVICE)
                .values(resource_type=TYPE_SWITCH)
            )
            await session.execute(
                update(CCResource)
                .where(CCResource.legacy_kind == LEGACY_SITE)
                .values(resource_type="harkeniq:network.switch", display_name="renamed")
            )
            await session.commit()
        assert await _canonical(app, sessionmaker, estate) == canonical
        assert await _catalogue_sets(app) == visible
        assert DEVICE_CLASSES == frozenset({"server", "switch"})

    async def test_capabilities_are_not_inferred_from_the_catalogue(self):
        app, sessionmaker, estate = await _tx1_stack(mirror=False)
        async with _client(app, "tenant_owner", "kc-cap") as client:
            await _grant_row(sessionmaker, "kc-cap", SCOPE_TENANT, role="tenant_owner")
            before = (await client.get("/api/capabilities/")).text
            await _reconcile(sessionmaker)
            async with sessionmaker() as session:
                await session.execute(update(CCResource).values(
                    resource_type=TYPE_SWITCH, resource_kind=KIND_PHYSICAL_DEVICE,
                ))
                await session.commit()
            after = (await client.get("/api/capabilities/")).text
        assert after == before

    async def test_tenants_stay_apart(self):
        app, sessionmaker, estate = await _tx1_stack()
        async with sessionmaker() as session:
            foreign = await _insert(
                session, KIND_PHYSICAL_CONTAINER, TYPE_SITE, tenant="other-tenant",
                # anchored to THIS tenant's site id, to prove the anchor alone
                # cannot carry a resource across the tenant boundary
                legacy=(LEGACY_SITE, estate.sites["site-1"], estate.sites["site-1"]),
            )
            session.add(CCResourceExternalId(
                tenant_id="other-tenant", resource_id=foreign.id,
                source_system=EXTERNAL_SOURCE_HARKENIQ, namespace=NAMESPACE_SITE,
                external_id=estate.sites["site-1"], source="foreign",
            ))
            await session.commit()
        async with await _as(app, sessionmaker, estate, "owner") as client:
            ids = {r["id"] for r in await _catalogue(client, lifecycle="any")}
            assert foreign.id not in ids
            assert (await client.get(f"{RESOURCES}{foreign.id}")).status_code == 404
            hits = (await client.get(RESOURCES, params={
                "source_system": EXTERNAL_SOURCE_HARKENIQ, "namespace": NAMESPACE_SITE,
                "external_id": estate.sites["site-1"],
            })).json()["resources"]
        site1 = await _resource(sessionmaker, LEGACY_SITE, estate.sites["site-1"])
        assert [h["id"] for h in hits] == [site1.id]

    async def test_an_identifier_bound_in_another_tenant_resolves_nothing_here(self):
        """Defence in depth: `bind_external_id` never writes a cross-tenant
        binding, but a raw row that did must still not let ANOTHER tenant's
        identifier resolve to this tenant's resource."""
        app, sessionmaker, estate = await _tx1_stack()
        node3 = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-3")
        async with sessionmaker() as session:
            session.add(CCResourceExternalId(
                tenant_id="other-tenant", resource_id=node3.id,
                source_system="cmdb", namespace="sys_id",
                external_id="FOREIGN-1", source="corrupted",
            ))
            await session.commit()
        async with await _as(app, sessionmaker, estate, "owner") as client:
            body = (await client.get(RESOURCES, params={
                "source_system": "cmdb", "namespace": "sys_id",
                "external_id": "FOREIGN-1",
            })).json()
        assert body["resources"] == [] and body["total"] == 0

    async def test_unanchored_and_unknown_kind_rows_are_tenant_owned(self):
        app, sessionmaker, estate = await _tx1_stack()
        async with sessionmaker() as session:
            loose = await _insert(session, KIND_LOGICAL_RESOURCE, "x:logical.thing")
            odd = await _insert(session, KIND_PHYSICAL_CONTAINER, TYPE_SITE,
                                legacy=("rack", "r-1", estate.sites["site-1"]))
            await session.commit()
        async with await _as(app, sessionmaker, estate, "owner") as client:
            assert (await client.get(f"{RESOURCES}{loose.id}")).status_code == 200
        for persona in ("region_a", "site_1", "device_3", "mixed"):
            async with await _as(app, sessionmaker, estate, persona) as client:
                for row in (loose, odd):
                    assert (await client.get(f"{RESOURCES}{row.id}")).status_code == 404

    async def test_a_logical_resource_can_never_be_a_physical_parent(self):
        """Where a Site Manager or Operational Agent would have to live if a
        later slice represented them -- and it still could not contain."""
        sessionmaker, estate = await _bare()
        async with sessionmaker() as session:
            repo = ResourceRepo(session)
            manager = await _insert(session, KIND_LOGICAL_RESOURCE, "x:runtime.site_manager")
            device = await _insert(session, KIND_PHYSICAL_DEVICE, TYPE_SERVER)
            with pytest.raises(CatalogueError, match="primary containment parent"):
                await repo.set_primary_parent(TENANT, device.id, manager.id, source="s")
            # The runtime relationship has its own name, and it is never
            # containment.
            edge = await repo.add_relationship(
                TENANT, device.id, manager.id, REL_MANAGED_BY, source="s"
            )
            assert edge.relationship_type == REL_MANAGED_BY and not edge.is_primary

    async def test_a_machine_principal_is_refused_on_every_catalogue_route(self):
        app, sessionmaker, estate = await _tx1_stack()
        node1 = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-1")

        async def _machine():
            return UserContext(
                user_id="agent-tx1", email="", tenant_id=TENANT, role="",
                permissions=["fleet.view", "incident.view", "proposal.submit"],
                species=SPECIES_AGENT,
                machine_jobs=frozenset({"self", "attention", "incidents",
                                        "proposals", "autonomy"}),
            )

        app.dependency_overrides[get_current_user] = _machine
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for path in (RESOURCES, f"{RESOURCES}{node1.id}",
                         f"{RESOURCES}{node1.id}/relationships"):
                res = await client.get(path)
                assert res.status_code == 403, (path, res.status_code)
                assert "External Agent API plane" in res.text


# ---------------------------------------------------------------------------
# 6. Structure: the catalogue decides nothing, and nothing consults it
# ---------------------------------------------------------------------------

#: The only files that may name the catalogue at all.
CATALOGUE_FILES = {
    "resource_catalogue.py", "resource_mirror.py", "api/resources.py",
    "db/models.py", "db/repos.py", "runtime.py", "app.py",
    "db/migrations/versions/0030_resource_catalogue.py",
}
CATALOGUE_NAMES = (
    "CCResource", "cc_resource", "resource_catalogue", "resource_mirror",
    "ResourceRepo", "scope_resources", "api.resources", "resources_api",
)


class TestStructure:
    def test_no_authority_execution_or_capability_module_consults_the_catalogue(self):
        offenders = []
        for path in sorted(CC_SRC.rglob("*.py")):
            rel = path.relative_to(CC_SRC).as_posix()
            if rel in CATALOGUE_FILES or "__pycache__" in rel:
                continue
            source = path.read_text()
            hit = [n for n in CATALOGUE_NAMES if n in source]
            if hit:
                offenders.append(f"{rel}: {hit}")
        assert not offenders, (
            "the catalogue is descriptive only (A31.1); these modules reach it:\n  "
            + "\n  ".join(offenders)
        )
        # The modules that DECIDE are all present to be checked.
        for name in ("scope.py", "auth.py", "api/deps.py", "grant_integrity.py",
                     "machine_identity.py", "target_authority.py", "autonomy.py",
                     "global_safety.py", "agent_runtime.py", "operational_agent.py",
                     "proposal_admission.py", "campaign_runner.py", "capabilities.py",
                     "capability_catalogue.py", "discovery.py", "fleet_poller.py"):
            assert (CC_SRC / name).exists(), name

    def test_in_repos_only_the_catalogue_predicate_and_repo_touch_it(self):
        tree = ast.parse(inspect.getsource(repos_module))
        users = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                src = ast.get_source_segment(inspect.getsource(repos_module), node) or ""
                if any(n in src for n in ("CCResource", "scope_resources")):
                    users.add(node.name)
        assert users == {"scope_resources", "ResourceRepo"}, users

    def test_visibility_reads_only_the_legacy_link_and_the_canonical_predicates(self):
        source = inspect.getsource(scope_resources)
        code = "\n".join(
            line for line in source.splitlines() if not line.strip().startswith(("#", "*"))
        )
        body = code.split('"""')[-1]   # past the docstring
        for canonical in ("scope_sites(", "scope_fleet_devices("):
            assert canonical in body
        for forbidden in ("CCResourceRelationship", "CCResourceExternalId",
                          "resource_type", "display_name", "lifecycle_state",
                          "resource_kind", "contextual", "list_context"):
            assert forbidden not in body, forbidden

    def test_only_the_mirror_writes_a_legacy_link(self):
        callers = []
        for path in sorted(CC_SRC.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "create_mirrored"):
                    callers.append(path.relative_to(CC_SRC).as_posix())
        assert set(callers) == {"resource_mirror.py"}, callers

    def test_the_surface_moved_by_exactly_three_human_reads(self):
        declared = {k: v for k, v in ROUTE_CONTRACT.items() if k[1].startswith(RESOURCES)}
        assert declared == {
            ("GET", "/api/resources/"): ("fleet.view", READ_SCOPED, False),
            ("GET", "/api/resources/{resource_id}"): ("fleet.view", READ_SCOPED, False),
            ("GET", "/api/resources/{resource_id}/relationships"):
                ("fleet.view", READ_SCOPED, False),
        }
        assert not any(k[1].startswith(RESOURCES) for k in MACHINE_SURFACE)
        assert len(ROUTE_CONTRACT) == 102 and len(MACHINE_SURFACE) == 14

    def test_the_permissions_and_the_ceiling_are_unchanged(self):
        from harkeniq_cc.machine_identity import MACHINE_PRINCIPAL_CEILING
        from harkeniq_console.permissions import PERMISSIONS

        assert len(PERMISSIONS) == 25
        assert set(MACHINE_PRINCIPAL_CEILING) == {
            "fleet.view", "incident.view", "proposal.submit",
        }
        assert set(ROLE_PERMISSIONS) and "resource.view" not in PERMISSIONS


# ---------------------------------------------------------------------------
# 7. The loop is wired: the real runtime populates the catalogue at boot
# ---------------------------------------------------------------------------


class TestRuntimeWiring:
    async def test_the_runtime_mirrors_at_boot(self):
        config = CCConfig(insecure=True, tenant_id="lab", http_host="127.0.0.1",
                          http_port=0, site_poll_interval_s=3600)
        state = await make_state(config)
        async with state.sessionmaker() as session:
            session.add(CCSite(id="s" * 32, tenant_id="lab", site_name="lab-site",
                               sm_endpoint="sm:1"))
            await session.flush()
            session.add(CCFleetCache(site_id="s" * 32, agent_id="node-lab",
                                     agent_name="node-lab", device_class="server"))
            await session.commit()
        task = asyncio.create_task(run(config, state=state))
        try:
            found = []
            for _ in range(100):
                async with state.sessionmaker() as session:
                    found = (await session.execute(
                        select(CCResource.legacy_ref).where(CCResource.tenant_id == "lab")
                    )).scalars().all()
                if len(found) == 2:
                    break
                await asyncio.sleep(0.1)
            assert sorted(found) == sorted(["s" * 32, "node-lab"]), found
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, BaseExceptionGroup):
                pass
