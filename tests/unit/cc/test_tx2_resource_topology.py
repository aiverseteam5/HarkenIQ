"""TX-2 (spec A32): resource relationships, topology and the rack mapping.

The catalogue now describes WHERE things are -- a device in a rack in a
site -- and keeps a former placement instead of rewriting it. It still
decides nothing. These tests hold A32 to its letter against the production
app and the real mirror, every persona under STRICT through real grants:

1. **Vocabulary** -- rack is the one new legacy kind and type; the reserved
   contextual container types are NOT registered (nothing writes them).
2. **Relationship lifecycle** -- retire and revive, never rewrite or
   delete; one ACTIVE primary parent is a database guarantee; an edge is
   asserted only between live resources.
3. **Topology integrity** -- no self-parent; no cycle through ANY active
   CONTAINS edge, secondary ones included; bounded walks fail closed; no
   cross-tenant or dangling edge (refused, and keyed out by the declared
   constraints); no name identity; physical parents only.
4. **The rack mirror** -- Site CONTAINS Rack CONTAINS Device from the Site
   Manager's report; moves, renames, retirement and revival keep every id;
   an UNKNOWN report holds; cross-site and duplicate claims are refused; a
   rack id reported at two sites is held.
5. **Visibility and the topology reads, generated** -- for every persona the
   catalogue's racks equal the racks at its authoritative sites, and every
   parents chain, children page and subtree equals an INDEPENDENT oracle
   computed from the database and restricted to what that persona can see.
6. **The A32 invariants** -- racks, relationships, forged topology, a shared
   rack and a tenant-owned container above the sites confer nothing; the
   canonical reads, `device_class`, capabilities and the resolved scope do
   not move.
7. **Structure** -- the rack report has one writer and one reader; nothing
   that decides consults it or the catalogue; the surface moved by exactly
   three human-only reads.
"""

from __future__ import annotations

import ast
import pathlib

import httpx
import pytest
from sqlalchemy import delete, event, select, update
from sqlalchemy.exc import IntegrityError

from harkeniq_cc.api import resources as resources_api
from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.attention_projection import DEVICE_CLASSES
from harkeniq_cc.auth import UserContext
from harkeniq_cc.db import repos as repos_module
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.models import (
    CCFleetCache,
    CCResource,
    CCResourceExternalId,
    CCResourceRelationship,
    CCSite,
    CCSiteTopology,
)
from harkeniq_cc.db.repos import ResourceRepo, SiteTopologyRepo, scope_resources
from harkeniq_cc.machine_identity import SPECIES_AGENT
from harkeniq_cc.resource_catalogue import (
    BUILTIN_TYPES,
    EXTERNAL_SOURCE_HARKENIQ,
    KIND_LOGICAL_RESOURCE,
    KIND_PHYSICAL_CONTAINER,
    KIND_PHYSICAL_DEVICE,
    LEGACY_DEVICE,
    LEGACY_KINDS,
    LEGACY_RACK,
    LEGACY_SITE,
    LIFECYCLE_ACTIVE,
    LIFECYCLE_RETIRED,
    LIFECYCLE_STATES,
    MAX_CONTAINMENT_DEPTH,
    MAX_TOPOLOGY_DEPTH,
    NAMESPACE_RACK,
    REL_CONTAINS,
    REL_MANAGED_BY,
    SOURCE_LEGACY_MIRROR,
    TYPE_RACK,
    TYPE_SERVER,
    TYPE_SITE,
    CatalogueError,
    validate_type,
)
from harkeniq_cc.route_contract import (
    MACHINE_SURFACE,
    READ_SCOPED,
    ROUTE_CONTRACT,
    census,
)
from harkeniq_cc.scope import SCOPE_DEVICE, SCOPE_TENANT

from tests.unit.cc.test_e1_persona_matrix import TENANT, _client, _stack
from tests.unit.cc.test_tx1_resource_catalogue import (
    CANONICAL_READS,
    CC_SRC,
    PERSONAS as TX1_PERSONAS,
    RESOURCES,
    TOKEN_ROLE as TX1_TOKEN_ROLE,
    _bare,
    _catalogue,
    _grant_row,
    _insert,
    _lapse,
    _move_and_retire,
    _reconcile,
    _resource,
    _tx1_stack,
    _views,
)

# ---------------------------------------------------------------------------
# The estate: TX-1's, plus what each site's Site Manager reports.
#
#   site-1 (reported): rack rk-1a "A01" holds node-site-1, node-blank-1 and
#                      node-gone-1 (a tombstone after the move pass);
#                      rack rk-1b "A02" is empty
#   site-2 (reported): rack rk-2a "B01" holds node-switch-2; node-site-2 is
#                      unplaced
#   site-3 (UNKNOWN):  its Site Manager predates TX-2
# ---------------------------------------------------------------------------

RACKS = {
    "site-1": [
        ("rk-1a", "A01", ["node-site-1", "node-blank-1", "node-gone-1"]),
        ("rk-1b", "A02", []),
    ],
    "site-2": [("rk-2a", "B01", ["node-switch-2"])],
}

#: TX-1's personas, and one more: a device grant on a device that SHARES a
#: rack, so a sibling can be shown to gain nothing.
PERSONAS = {
    **TX1_PERSONAS,
    "device_1": [(SCOPE_DEVICE, "node-site-1", "viewer", None, None)],
}
TOKEN_ROLE = {**TX1_TOKEN_ROLE, "device_1": "viewer"}


def _racks_payload(racks):
    return [{"rack_id": r, "name": n, "device_agent_ids": list(m)} for r, n, m in racks]


async def _report(sessionmaker, estate, key, racks=None, *, reported=True):
    async with sessionmaker() as session:
        await SiteTopologyRepo(session).upsert(TENANT, estate.sites[key], {
            "reported": reported, "racks": _racks_payload(racks or []),
        })
        await session.commit()


async def _tx2_stack(*, mirror: bool = True):
    app, sessionmaker, estate = await _tx1_stack(mirror=False)
    for key, racks in RACKS.items():
        await _report(sessionmaker, estate, key, racks)
    await _report(sessionmaker, estate, "site-3", reported=False)
    if mirror:
        await _reconcile(sessionmaker)
        await _move_and_retire(sessionmaker, estate)
        await _reconcile(sessionmaker)
    return app, sessionmaker, estate


async def _rack(sessionmaker, ref):
    return await _resource(sessionmaker, LEGACY_RACK, ref)


async def _parent_of(sessionmaker, resource_id):
    async with sessionmaker() as session:
        edge = await ResourceRepo(session).primary_parent_edge(resource_id)
    return edge.from_resource_id if edge is not None else None


async def _edges(sessionmaker):
    async with sessionmaker() as session:
        return (await session.execute(select(CCResourceRelationship))).scalars().all()


async def _as(app, sessionmaker, estate, persona):
    user_id = f"kc-tx2-{persona}"
    for scope_type, key, role, subset, lapse in PERSONAS[persona]:
        ref = estate.sites.get(key) or estate.units.get(key) or key
        grant_id = await _grant_row(
            sessionmaker, user_id, scope_type, ref, role=role, subset=subset
        )
        if lapse:
            await _lapse(sessionmaker, grant_id, how=lapse)
    return _client(app, TOKEN_ROLE[persona], user_id)


async def _visible(client) -> set[str]:
    return {r["id"] for r in await _catalogue(client, lifecycle="any")}


# ---------------------------------------------------------------------------
# 1. Vocabulary
# ---------------------------------------------------------------------------

#: A32.6: allowed by the model, reserved by name, registered by nobody.
RESERVED_CONTAINER_TYPES = (
    "harkeniq:geography.region", "harkeniq:facility.campus",
    "harkeniq:facility.datacenter", "harkeniq:facility.building",
    "harkeniq:facility.room", "harkeniq:facility.hall",
    "harkeniq:placement.zone", "harkeniq:placement.row",
    "harkeniq:enclosure.chassis",
)


class TestVocabulary:
    def test_rack_is_the_one_new_legacy_kind_and_type(self):
        assert LEGACY_KINDS == {LEGACY_SITE, LEGACY_DEVICE, LEGACY_RACK}
        assert BUILTIN_TYPES[TYPE_RACK] == KIND_PHYSICAL_CONTAINER
        assert TYPE_RACK == "harkeniq:placement.rack" and NAMESPACE_RACK == "rack"

    @pytest.mark.parametrize("reserved", RESERVED_CONTAINER_TYPES)
    def test_a_reserved_container_type_is_not_registered(self, reserved):
        """Nothing writes one, so nothing may treat one as known (A32.6)."""
        assert reserved not in BUILTIN_TYPES
        with pytest.raises(CatalogueError, match="not in the built-in registry"):
            validate_type(reserved, KIND_PHYSICAL_CONTAINER)

    def test_an_edge_speaks_the_resource_lifecycle(self):
        assert LIFECYCLE_STATES == {LIFECYCLE_ACTIVE, LIFECYCLE_RETIRED}
        column = CCResourceRelationship.__table__.c.lifecycle_state
        assert column.server_default.arg == LIFECYCLE_ACTIVE

    def test_the_lifecycle_filters_are_exactly_the_vocabulary(self):
        import typing

        assert set(typing.get_args(resources_api.LifecycleFilter)) == (
            set(LIFECYCLE_STATES) | {"any"}
        )


# ---------------------------------------------------------------------------
# 2. Relationship lifecycle -- the primitives
# ---------------------------------------------------------------------------


class TestRelationshipLifecycle:
    async def test_a_move_retires_and_moving_back_revives_the_same_edge(self):
        sessionmaker, estate = await _bare()
        async with sessionmaker() as session:
            repo = ResourceRepo(session)
            a = await _insert(session, KIND_PHYSICAL_CONTAINER, TYPE_SITE)
            b = await _insert(session, KIND_PHYSICAL_CONTAINER, TYPE_SITE)
            dev = await _insert(session, KIND_PHYSICAL_DEVICE, TYPE_SERVER)
            first = await repo.set_primary_parent(TENANT, dev.id, a.id, source="s")
            second = await repo.set_primary_parent(TENANT, dev.id, b.id, source="s")
            assert (first.lifecycle_state, first.retired_at is not None) == (
                LIFECYCLE_RETIRED, True)
            back = await repo.set_primary_parent(TENANT, dev.id, a.id, source="s")
            # One row per (from, to, type) for life: the SAME edge, revived.
            assert back.id == first.id
            assert (back.lifecycle_state, back.retired_at, back.is_primary) == (
                LIFECYCLE_ACTIVE, None, True)
            assert second.lifecycle_state == LIFECYCLE_RETIRED
            rows = (await session.execute(select(CCResourceRelationship))).scalars().all()
            assert len(rows) == 2, "never a third row, never a delete"

    async def test_only_an_active_primary_counts_against_the_index(self):
        sessionmaker, estate = await _bare()
        async with sessionmaker() as session:
            a = await _insert(session, KIND_PHYSICAL_CONTAINER, TYPE_SITE)
            b = await _insert(session, KIND_PHYSICAL_CONTAINER, TYPE_SITE)
            c = await _insert(session, KIND_PHYSICAL_CONTAINER, TYPE_SITE)
            dev = await _insert(session, KIND_PHYSICAL_DEVICE, TYPE_SERVER)

            def edge(parent, state):
                return CCResourceRelationship(
                    tenant_id=TENANT, from_resource_id=parent.id,
                    to_resource_id=dev.id, relationship_type=REL_CONTAINS,
                    is_primary=True, source="raw", lifecycle_state=state,
                )

            session.add_all([edge(a, LIFECYCLE_RETIRED), edge(b, LIFECYCLE_ACTIVE)])
            await session.flush()          # a former placement + a current one
            session.add(edge(c, LIFECYCLE_ACTIVE))
            with pytest.raises(IntegrityError):
                await session.flush()      # a second CURRENT one: never

    @pytest.mark.parametrize("which", ["parent", "child"])
    async def test_nothing_new_is_said_about_a_tombstone(self, which):
        sessionmaker, estate = await _bare()
        async with sessionmaker() as session:
            repo = ResourceRepo(session)
            parent = await _insert(session, KIND_PHYSICAL_CONTAINER, TYPE_SITE)
            child = await _insert(session, KIND_PHYSICAL_DEVICE, TYPE_SERVER)
            await repo.retire(parent if which == "parent" else child)
            with pytest.raises(CatalogueError, match="only between live resources"):
                await repo.set_primary_parent(TENANT, child.id, parent.id, source="s")
            with pytest.raises(CatalogueError, match="only between live resources"):
                await repo.add_relationship(
                    TENANT, parent.id, child.id, REL_MANAGED_BY, source="s")

    async def test_a_former_edge_of_another_source_is_never_taken_over(self):
        sessionmaker, estate = await _bare()
        async with sessionmaker() as session:
            repo = ResourceRepo(session)
            a = await _insert(session, KIND_PHYSICAL_CONTAINER, TYPE_SITE)
            b = await _insert(session, KIND_PHYSICAL_CONTAINER, TYPE_SITE)
            dev = await _insert(session, KIND_PHYSICAL_DEVICE, TYPE_SERVER)
            session.add(CCResourceRelationship(
                tenant_id=TENANT, from_resource_id=a.id, to_resource_id=dev.id,
                relationship_type=REL_CONTAINS, is_primary=True, source="operator",
                lifecycle_state=LIFECYCLE_RETIRED,
            ))
            await session.flush()
            await repo.set_primary_parent(TENANT, dev.id, b.id, source=SOURCE_LEGACY_MIRROR)
            with pytest.raises(CatalogueError, match="may not take it over"):
                await repo.set_primary_parent(
                    TENANT, dev.id, a.id, source=SOURCE_LEGACY_MIRROR)
            with pytest.raises(CatalogueError, match="may not take it over"):
                await repo.add_relationship(
                    TENANT, a.id, dev.id, REL_CONTAINS, source=SOURCE_LEGACY_MIRROR)

    async def test_a_secondary_edge_revives_rather_than_duplicates(self):
        sessionmaker, estate = await _bare()
        async with sessionmaker() as session:
            repo = ResourceRepo(session)
            a = await _insert(session, KIND_PHYSICAL_DEVICE, TYPE_SERVER)
            b = await _insert(session, KIND_PHYSICAL_DEVICE, TYPE_SERVER)
            edge = await repo.add_relationship(TENANT, a.id, b.id, REL_MANAGED_BY, source="s")
            edge.lifecycle_state = LIFECYCLE_RETIRED
            await session.flush()
            again = await repo.add_relationship(TENANT, a.id, b.id, REL_MANAGED_BY, source="s")
            assert again.id == edge.id and again.lifecycle_state == LIFECYCLE_ACTIVE
            assert again.is_primary is False


# ---------------------------------------------------------------------------
# 3. Topology integrity
# ---------------------------------------------------------------------------


class TestIntegrity:
    async def test_a_resource_is_never_its_own_parent(self):
        sessionmaker, estate = await _bare()
        async with sessionmaker() as session:
            repo = ResourceRepo(session)
            box = await _insert(session, KIND_PHYSICAL_CONTAINER, RESERVED_CONTAINER_TYPES[0])
            with pytest.raises(CatalogueError, match="itself"):
                await repo.set_primary_parent(TENANT, box.id, box.id, source="s")
            with pytest.raises(CatalogueError, match="itself"):
                await repo.add_relationship(TENANT, box.id, box.id, REL_CONTAINS, source="s")

    async def test_a_cycle_through_the_primary_chain_is_refused(self):
        """region > site > rack > device, then the device 'containing' the
        region -- kind-legal (a device may contain), a cycle, refused."""
        sessionmaker, estate = await _bare()
        async with sessionmaker() as session:
            repo = ResourceRepo(session)
            region = await _insert(session, KIND_PHYSICAL_CONTAINER, "harkeniq:geography.region")
            site = await _insert(session, KIND_PHYSICAL_CONTAINER, TYPE_SITE)
            rack = await _insert(session, KIND_PHYSICAL_CONTAINER, TYPE_RACK)
            dev = await _insert(session, KIND_PHYSICAL_DEVICE, TYPE_SERVER)
            await repo.set_primary_parent(TENANT, site.id, region.id, source="s")
            await repo.set_primary_parent(TENANT, rack.id, site.id, source="s")
            await repo.set_primary_parent(TENANT, dev.id, rack.id, source="s")
            for parent, child in ((dev, region), (rack, site), (dev, site)):
                with pytest.raises(CatalogueError, match="cycle"):
                    await repo.set_primary_parent(TENANT, child.id, parent.id, source="s")
                with pytest.raises(CatalogueError, match="cycle"):
                    await repo.add_relationship(
                        TENANT, parent.id, child.id, REL_CONTAINS, source="s")

    async def test_a_cycle_through_a_secondary_contains_edge_is_refused(self):
        """TX-1's walk followed primary edges only, so a secondary CONTAINS
        could close a loop it never saw. Every active CONTAINS counts now."""
        sessionmaker, estate = await _bare()
        async with sessionmaker() as session:
            repo = ResourceRepo(session)
            hall = await _insert(session, KIND_PHYSICAL_CONTAINER, "harkeniq:facility.hall")
            row = await _insert(session, KIND_PHYSICAL_CONTAINER, "harkeniq:placement.row")
            await repo.add_relationship(TENANT, hall.id, row.id, REL_CONTAINS, source="s")
            with pytest.raises(CatalogueError, match="cycle"):
                await repo.set_primary_parent(TENANT, hall.id, row.id, source="s")
            with pytest.raises(CatalogueError, match="cycle"):
                await repo.add_relationship(TENANT, row.id, hall.id, REL_CONTAINS, source="s")
            # A retired secondary edge is history, not containment.
            edge = (await session.execute(select(CCResourceRelationship))).scalar_one()
            edge.lifecycle_state = LIFECYCLE_RETIRED
            await session.flush()
            await repo.set_primary_parent(TENANT, hall.id, row.id, source="s")

    async def test_a_chain_deeper_than_its_bound_is_refused(self):
        sessionmaker, estate = await _bare()
        async with sessionmaker() as session:
            repo = ResourceRepo(session)
            chain = [await _insert(session, KIND_PHYSICAL_CONTAINER, TYPE_SITE)
                     for _ in range(MAX_CONTAINMENT_DEPTH + 2)]
            for depth in range(MAX_CONTAINMENT_DEPTH):
                await repo.set_primary_parent(
                    TENANT, chain[depth + 1].id, chain[depth].id, source="s")
            with pytest.raises(CatalogueError, match="this deep"):
                await repo.set_primary_parent(
                    TENANT, chain[MAX_CONTAINMENT_DEPTH + 1].id,
                    chain[MAX_CONTAINMENT_DEPTH].id, source="s")

    async def test_a_walk_wider_than_its_bound_fails_closed(self, monkeypatch):
        monkeypatch.setattr(repos_module, "MAX_CONTAINMENT_WALK", 3)
        sessionmaker, estate = await _bare()
        async with sessionmaker() as session:
            repo = ResourceRepo(session)
            hub = await _insert(session, KIND_PHYSICAL_CONTAINER, TYPE_SITE)
            for _ in range(4):
                outer = await _insert(session, KIND_PHYSICAL_CONTAINER, TYPE_SITE)
                await repo.add_relationship(TENANT, outer.id, hub.id, REL_CONTAINS, source="s")
            dev = await _insert(session, KIND_PHYSICAL_DEVICE, TYPE_SERVER)
            with pytest.raises(CatalogueError, match="cannot be ruled out"):
                await repo.set_primary_parent(TENANT, dev.id, hub.id, source="s")

    async def test_containment_is_physical_even_on_a_secondary_edge(self):
        """Where a Site Manager or an Operational Agent would live, if ever
        represented -- and it can contain nothing, by any CONTAINS edge."""
        sessionmaker, estate = await _bare()
        async with sessionmaker() as session:
            repo = ResourceRepo(session)
            manager = await _insert(session, KIND_LOGICAL_RESOURCE, "x:runtime.site_manager")
            site = await _insert(session, KIND_PHYSICAL_CONTAINER, TYPE_SITE)
            with pytest.raises(CatalogueError, match="primary containment parent"):
                await repo.add_relationship(TENANT, manager.id, site.id, REL_CONTAINS, source="s")
            with pytest.raises(CatalogueError, match="primary containment parent"):
                await repo.set_primary_parent(TENANT, site.id, manager.id, source="s")
            managed = await repo.add_relationship(
                TENANT, site.id, manager.id, REL_MANAGED_BY, source="s")
            assert managed.relationship_type == REL_MANAGED_BY and not managed.is_primary

    def test_both_ends_are_keyed_to_the_edges_own_tenant(self):
        """The constraints that make a cross-tenant or dangling edge
        impossible, as declared -- migration 0031 adds them on PostgreSQL."""
        table = CCResourceRelationship.__table__
        keyed = {
            tuple(fk.column_keys): tuple(e.target_fullname for e in fk.elements)
            for fk in table.foreign_key_constraints
        }
        assert keyed[("from_resource_id", "tenant_id")] == (
            "cc_resources.id", "cc_resources.tenant_id")
        assert keyed[("to_resource_id", "tenant_id")] == (
            "cc_resources.id", "cc_resources.tenant_id")
        assert keyed[("from_resource_id",)] == ("cc_resources.id",)
        assert keyed[("to_resource_id",)] == ("cc_resources.id",)

    @pytest.mark.parametrize("case", ["cross_tenant_from", "cross_tenant_to", "dangling"])
    async def test_the_database_refuses_a_cross_tenant_or_dangling_edge(self, case):
        """Enforced, not only declared: a fresh database with foreign keys
        ON refuses the raw row the repository would never write."""
        engine = make_engine("sqlite+aiosqlite:///:memory:")

        @event.listens_for(engine.sync_engine, "connect")
        def _on(conn, _record):
            conn.execute("PRAGMA foreign_keys=ON")

        await create_all(engine)
        maker = make_sessionmaker(engine)
        try:
            async with maker() as session:
                mine = await _insert(session, KIND_PHYSICAL_CONTAINER, TYPE_SITE)
                theirs = await _insert(session, KIND_PHYSICAL_DEVICE, TYPE_SERVER,
                                       tenant="other-tenant")
                ours = await _insert(session, KIND_PHYSICAL_DEVICE, TYPE_SERVER)
                await session.commit()
                ends = {
                    "cross_tenant_from": (theirs.id, ours.id),
                    "cross_tenant_to": (mine.id, theirs.id),
                    "dangling": (mine.id, "0" * 32),
                }[case]
                session.add(CCResourceRelationship(
                    tenant_id=TENANT, from_resource_id=ends[0], to_resource_id=ends[1],
                    relationship_type=REL_CONTAINS, is_primary=False, source="raw",
                ))
                with pytest.raises(IntegrityError):
                    await session.commit()
        finally:
            await engine.dispose()

    async def test_the_repository_refuses_an_edge_into_another_tenant(self):
        sessionmaker, estate = await _bare()
        async with sessionmaker() as session:
            repo = ResourceRepo(session)
            mine = await _insert(session, KIND_PHYSICAL_CONTAINER, TYPE_RACK)
            theirs = await _insert(session, KIND_PHYSICAL_DEVICE, TYPE_SERVER,
                                   tenant="other-tenant")
            for call in (
                repo.set_primary_parent(TENANT, theirs.id, mine.id, source="s"),
                repo.set_primary_parent("other-tenant", theirs.id, mine.id, source="s"),
                repo.add_relationship(TENANT, mine.id, theirs.id, REL_CONTAINS, source="s"),
            ):
                with pytest.raises(CatalogueError, match="this tenant"):
                    await call
            assert (await session.execute(select(CCResourceRelationship))).first() is None


# ---------------------------------------------------------------------------
# 4. The rack mirror
# ---------------------------------------------------------------------------


class TestRackMirror:
    async def test_site_contains_rack_contains_device(self):
        app, sessionmaker, estate = await _tx2_stack()
        site1 = await _resource(sessionmaker, LEGACY_SITE, estate.sites["site-1"])
        site2 = await _resource(sessionmaker, LEGACY_SITE, estate.sites["site-2"])
        rk1a, rk1b, rk2a = [await _rack(sessionmaker, r) for r in ("rk-1a", "rk-1b", "rk-2a")]
        for rack, site, key, name in ((rk1a, site1, "site-1", "A01"),
                                      (rk1b, site1, "site-1", "A02"),
                                      (rk2a, site2, "site-2", "B01")):
            assert (rack.resource_kind, rack.resource_type, rack.source) == (
                KIND_PHYSICAL_CONTAINER, TYPE_RACK, SOURCE_LEGACY_MIRROR)
            assert rack.display_name == name and rack.legacy_site_id == estate.sites[key]
            assert await _parent_of(sessionmaker, rack.id) == site.id
        for agent, rack in (("node-site-1", rk1a), ("node-blank-1", rk1a),
                            ("node-switch-2", rk2a)):
            device = await _resource(sessionmaker, LEGACY_DEVICE, agent)
            assert await _parent_of(sessionmaker, device.id) == rack.id, agent
        # Unplaced: directly under its site, as the architecture permits.
        node2 = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-2")
        assert await _parent_of(sessionmaker, node2.id) == site2.id
        async with sessionmaker() as session:
            ids = (await session.execute(select(CCResourceExternalId).where(
                CCResourceExternalId.namespace == NAMESPACE_RACK))).scalars().all()
        assert {(e.source_system, e.external_id, e.resource_id) for e in ids} == {
            (EXTERNAL_SOURCE_HARKENIQ, "rk-1a", rk1a.id),
            (EXTERNAL_SOURCE_HARKENIQ, "rk-1b", rk1b.id),
            (EXTERNAL_SOURCE_HARKENIQ, "rk-2a", rk2a.id),
        }

    async def test_a_site_that_never_reported_has_no_racks_and_keeps_its_devices(self):
        app, sessionmaker, estate = await _tx2_stack()
        async with sessionmaker() as session:
            racks = await ResourceRepo(session).list_mirrored(TENANT, LEGACY_RACK)
        assert {r.legacy_site_id for r in racks} == {
            estate.sites["site-1"], estate.sites["site-2"]}
        site3 = await _resource(sessionmaker, LEGACY_SITE, estate.sites["site-3"])
        node3 = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-3")
        moved = await _resource(sessionmaker, LEGACY_DEVICE, "node-moved")
        assert await _parent_of(sessionmaker, node3.id) == site3.id
        assert await _parent_of(sessionmaker, moved.id) == site3.id

    async def test_a_second_pass_writes_nothing(self):
        app, sessionmaker, estate = await _tx2_stack()
        async with sessionmaker() as session:
            before = sorted((r.id, r.updated_at, r.lifecycle_state) for r in
                            (await session.execute(select(CCResourceRelationship))).scalars())
        report = await _reconcile(sessionmaker)
        assert not report.changed and report.conflicts == 0 and report.refused == 0
        async with sessionmaker() as session:
            after = sorted((r.id, r.updated_at, r.lifecycle_state) for r in
                           (await session.execute(select(CCResourceRelationship))).scalars())
        assert after == before

    async def test_a_move_between_racks_keeps_the_former_placement(self):
        app, sessionmaker, estate = await _tx2_stack()
        rk1a, rk1b = await _rack(sessionmaker, "rk-1a"), await _rack(sessionmaker, "rk-1b")
        node1 = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-1")

        async def place(where):
            racks = [("rk-1a", "A01", ["node-blank-1"] + (["node-site-1"] if where == "a" else [])),
                     ("rk-1b", "A02", ["node-site-1"] if where == "b" else [])]
            await _report(sessionmaker, estate, "site-1", racks)
            return await _reconcile(sessionmaker)

        assert (await place("b")).reparented == 1
        assert await _parent_of(sessionmaker, node1.id) == rk1b.id
        edges = {(e.from_resource_id, e.to_resource_id): e for e in await _edges(sessionmaker)}
        former = edges[(rk1a.id, node1.id)]
        assert former.lifecycle_state == LIFECYCLE_RETIRED and former.is_primary
        await place("a")
        assert await _parent_of(sessionmaker, node1.id) == rk1a.id
        edges = {(e.from_resource_id, e.to_resource_id): e for e in await _edges(sessionmaker)}
        assert edges[(rk1a.id, node1.id)].id == former.id, "the same edge, revived"
        assert edges[(rk1b.id, node1.id)].lifecycle_state == LIFECYCLE_RETIRED

    async def test_a_rack_gone_from_its_report_is_a_tombstone_and_revives_as_itself(self):
        app, sessionmaker, estate = await _tx2_stack()
        site1 = await _resource(sessionmaker, LEGACY_SITE, estate.sites["site-1"])
        rk1a = await _rack(sessionmaker, "rk-1a")
        node1 = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-1")
        await _report(sessionmaker, estate, "site-1", [("rk-1b", "A02", [])])
        report = await _reconcile(sessionmaker)
        gone = await _rack(sessionmaker, "rk-1a")
        assert gone.id == rk1a.id and gone.lifecycle_state == LIFECYCLE_RETIRED
        assert report.retired == 1
        # The tombstone keeps its last placement; its devices move to the site.
        assert await _parent_of(sessionmaker, rk1a.id) == site1.id
        assert await _parent_of(sessionmaker, node1.id) == site1.id
        await _report(sessionmaker, estate, "site-1", RACKS["site-1"])
        report = await _reconcile(sessionmaker)
        back = await _rack(sessionmaker, "rk-1a")
        assert back.id == rk1a.id and back.lifecycle_state == LIFECYCLE_ACTIVE
        assert report.revived == 1 and back.retired_at is None
        assert await _parent_of(sessionmaker, node1.id) == rk1a.id

    async def test_a_renamed_rack_keeps_its_id(self):
        app, sessionmaker, estate = await _tx2_stack()
        before = await _rack(sessionmaker, "rk-2a")
        await _report(sessionmaker, estate, "site-2", [("rk-2a", "B01-renamed", ["node-switch-2"])])
        await _reconcile(sessionmaker)
        after = await _rack(sessionmaker, "rk-2a")
        assert after.id == before.id and after.display_name == "B01-renamed"

    async def test_names_are_not_identity(self):
        """Two racks of one name at two sites are two racks; a blank name
        reads as the rack's id, never as nothing."""
        app, sessionmaker, estate = await _tx2_stack()
        await _report(sessionmaker, estate, "site-2", [
            ("rk-2a", "B01", ["node-switch-2"]), ("rk-2z", "A01", []), ("rk-2y", "", []),
        ])
        await _reconcile(sessionmaker)
        same_name = [await _rack(sessionmaker, r) for r in ("rk-1a", "rk-2z")]
        assert {r.display_name for r in same_name} == {"A01"}
        assert same_name[0].id != same_name[1].id
        assert (await _rack(sessionmaker, "rk-2y")).display_name == "rk-2y"

    async def test_an_unknown_report_holds_what_is_known(self):
        app, sessionmaker, estate = await _tx2_stack()
        rk1a = await _rack(sessionmaker, "rk-1a")
        node1 = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-1")
        await _report(sessionmaker, estate, "site-1", reported=False)
        async with sessionmaker() as session:
            session.add(CCFleetCache(site_id=estate.sites["site-1"], agent_id="node-new-1",
                                     agent_name="node-new-1", device_class="server"))
            await session.commit()
        report = await _reconcile(sessionmaker)
        assert report.retired == 0, "an unknown report retires nothing"
        for ref in ("rk-1a", "rk-1b"):
            assert (await _rack(sessionmaker, ref)).lifecycle_state == LIFECYCLE_ACTIVE
        assert await _parent_of(sessionmaker, node1.id) == rk1a.id
        site1 = await _resource(sessionmaker, LEGACY_SITE, estate.sites["site-1"])
        new = await _resource(sessionmaker, LEGACY_DEVICE, "node-new-1")
        assert await _parent_of(sessionmaker, new.id) == site1.id

    async def test_a_claim_from_another_sites_report_is_refused(self):
        app, sessionmaker, estate = await _tx2_stack()
        site1 = await _resource(sessionmaker, LEGACY_SITE, estate.sites["site-1"])
        node1 = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-1")
        await _report(sessionmaker, estate, "site-2",
                      [("rk-2a", "B01", ["node-switch-2", "node-site-1"])])
        report = await _reconcile(sessionmaker)
        assert report.refused == 1
        assert await _parent_of(sessionmaker, node1.id) == site1.id

    async def test_two_racks_claiming_one_device_place_it_in_neither(self):
        app, sessionmaker, estate = await _tx2_stack()
        site1 = await _resource(sessionmaker, LEGACY_SITE, estate.sites["site-1"])
        blank = await _resource(sessionmaker, LEGACY_DEVICE, "node-blank-1")
        await _report(sessionmaker, estate, "site-1", [
            ("rk-1a", "A01", ["node-site-1", "node-blank-1"]),
            ("rk-1b", "A02", ["node-blank-1"]),
        ])
        report = await _reconcile(sessionmaker)
        assert report.refused == 1
        assert await _parent_of(sessionmaker, blank.id) == site1.id

    async def test_a_rack_reported_at_two_sites_is_held(self):
        app, sessionmaker, estate = await _tx2_stack()
        rk2a = await _rack(sessionmaker, "rk-2a")
        switch = await _resource(sessionmaker, LEGACY_DEVICE, "node-switch-2")
        await _report(sessionmaker, estate, "site-1",
                      RACKS["site-1"] + [("rk-2a", "B01 at site-1?", [])])
        report = await _reconcile(sessionmaker)
        assert report.ambiguous >= 1
        held = await _rack(sessionmaker, "rk-2a")
        assert (held.legacy_site_id, held.display_name) == (estate.sites["site-2"], "B01")
        assert held.lifecycle_state == LIFECYCLE_ACTIVE
        assert await _parent_of(sessionmaker, switch.id) == rk2a.id

    async def test_a_device_that_moves_site_leaves_its_rack(self):
        """A rack recorded at a different site from its device is refused
        (A31.3) -- even while the old site's report still claims it."""
        app, sessionmaker, estate = await _tx2_stack()
        site2 = await _resource(sessionmaker, LEGACY_SITE, estate.sites["site-2"])
        node1 = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-1")
        async with sessionmaker() as session:
            await session.execute(update(CCFleetCache)
                                  .where(CCFleetCache.agent_id == "node-site-1")
                                  .values(site_id=estate.sites["site-2"]))
            await session.commit()
        await _reconcile(sessionmaker)
        assert await _parent_of(sessionmaker, node1.id) == site2.id

    async def test_a_vanished_site_retires_its_racks(self):
        app, sessionmaker, estate = await _tx2_stack()
        s2 = estate.sites["site-2"]
        async with sessionmaker() as session:
            from harkeniq_cc.db.models import CCIncident

            await session.execute(delete(CCFleetCache).where(CCFleetCache.site_id == s2))
            await session.execute(delete(CCIncident).where(CCIncident.site_id == s2))
            await session.execute(delete(CCSite).where(CCSite.id == s2))
            await session.commit()
        await _reconcile(sessionmaker)
        assert (await _rack(sessionmaker, "rk-2a")).lifecycle_state == LIFECYCLE_RETIRED

    async def test_a_report_of_another_tenant_is_never_read(self):
        app, sessionmaker, estate = await _tx2_stack(mirror=False)
        async with sessionmaker() as session:
            row = await session.get(CCSiteTopology, estate.sites["site-2"])
            row.tenant_id = "other-tenant"
            await session.commit()
        await _reconcile(sessionmaker)
        assert await _rack(sessionmaker, "rk-2a") is None

    async def test_the_mirror_never_makes_a_runtime_or_an_actor_a_resource(self):
        app, sessionmaker, estate = await _tx2_stack()
        async with sessionmaker() as session:
            rows = (await session.execute(select(CCResource))).scalars().all()
            endpoints = set((await session.execute(select(CCSite.sm_endpoint))).scalars())
        assert {r.legacy_kind for r in rows} <= LEGACY_KINDS
        assert not {r.legacy_ref for r in rows} & endpoints
        assert not any(r.resource_kind == KIND_LOGICAL_RESOURCE for r in rows)
        assert {e.relationship_type for e in await _edges(sessionmaker)} == {REL_CONTAINS}

    async def test_the_rack_report_never_touches_the_fleet(self):
        """`device_class` -- and every other fleet field -- is untouched."""
        app, sessionmaker, estate = await _tx2_stack(mirror=False)

        async def fleet():
            async with sessionmaker() as session:
                return sorted(
                    (r.id, r.site_id, r.agent_id, r.device_class, r.agent_name)
                    for r in (await session.execute(select(CCFleetCache))).scalars())

        before = await fleet()
        await _reconcile(sessionmaker)
        assert await fleet() == before
        assert DEVICE_CLASSES == frozenset({"server", "switch"})


# ---------------------------------------------------------------------------
# 5. Visibility and the topology reads, generated over every persona
# ---------------------------------------------------------------------------


def _oracle(edges, visible, root, *, active_ids):
    """An INDEPENDENT reading of the topology, straight from the database:
    the active primary-containment edges, restricted to `visible`."""
    parent = {}
    children: dict[str, list[str]] = {}
    for e in edges:
        if (e.relationship_type == REL_CONTAINS and e.is_primary
                and e.lifecycle_state == LIFECYCLE_ACTIVE):
            parent[e.to_resource_id] = e.from_resource_id
            children.setdefault(e.from_resource_id, []).append(e.to_resource_id)
    chain, cursor = [], root
    while cursor in parent and parent[cursor] in visible:
        cursor = parent[cursor]
        chain.append(cursor)
    direct_any = {c for c in children.get(root, []) if c in visible}
    subtree, frontier = set(), [root]
    for _ in range(MAX_TOPOLOGY_DEPTH):
        frontier = [c for f in frontier for c in children.get(f, [])
                    if c in visible and c in active_ids and c not in subtree]
        subtree.update(frontier)
    return chain, direct_any, direct_any & active_ids, subtree


async def _forge(sessionmaker, estate):
    """Raw writes the repository would refuse or the mirror would never
    make -- deliberately acyclic, because a raw write bypasses the cycle
    check, and one active primary parent each, because the database holds
    that whatever writes:

        region (tenant-owned) > site-1 > rk-1a > node-site-3 (hidden child)
                                       > rk-1b > site-3 > node-site-1
                                                        > rk-2a > node-switch-2

    so a site-1 reader meets a hidden child under a visible rack, and from
    node-site-1 a hidden ancestor (site-3) with VISIBLE ones above it -- a
    chain that must stop, not skip."""
    site1 = await _resource(sessionmaker, LEGACY_SITE, estate.sites["site-1"])
    site3 = await _resource(sessionmaker, LEGACY_SITE, estate.sites["site-3"])
    rk1a, rk1b, rk2a = [await _rack(sessionmaker, r) for r in ("rk-1a", "rk-1b", "rk-2a")]
    node1 = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-1")
    node3 = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-3")
    async with sessionmaker() as session:
        for child, parent in ((node3, rk1a), (node1, site3), (rk2a, site3)):
            await session.execute(update(CCResourceRelationship).where(
                CCResourceRelationship.to_resource_id == child.id,
                CCResourceRelationship.lifecycle_state == LIFECYCLE_ACTIVE,
            ).values(from_resource_id=parent.id))
        region = await _insert(session, KIND_PHYSICAL_CONTAINER, "harkeniq:geography.region")
        for parent, child in ((region, site1), (rk1b, site3)):
            session.add(CCResourceRelationship(
                tenant_id=TENANT, from_resource_id=parent.id, to_resource_id=child.id,
                relationship_type=REL_CONTAINS, is_primary=True, source="forged",
            ))
        await session.commit()
    return region


class TestTopologyReads:
    @pytest.mark.parametrize("persona", sorted(PERSONAS))
    async def test_racks_are_exactly_the_racks_at_the_readers_sites(self, persona):
        app, sessionmaker, estate = await _tx2_stack()
        async with await _as(app, sessionmaker, estate, persona) as client:
            views = await _views(client)
            racks = {r["legacy"]["ref"] for r in await _catalogue(client, lifecycle="any")
                     if r["resource_type"] == TYPE_RACK}
        by_site = {estate.sites[k]: {r for r, _n, _m in v} for k, v in RACKS.items()}
        expected = set().union(*(by_site.get(s, set()) for s in views["sites"]))
        assert racks == expected, persona
        # And TX-1's parity still holds with racks in the catalogue.
        assert views["cat_devices"] == views["fleet"], persona
        assert views["cat_sites"] == views["sites"], persona

    @pytest.mark.parametrize("forged", [False, True], ids=["mirrored", "forged"])
    @pytest.mark.parametrize("persona", sorted(PERSONAS))
    async def test_every_walk_equals_the_oracle(self, persona, forged):
        """`forged` adds what the mirror never writes and a later writer
        might: hidden children under visible parents, a hidden ancestor
        above a visible resource, and a tenant-owned region over two sites.
        Without it every visible container's children happen to be visible
        too, and a walk that forgot to filter would still pass."""
        app, sessionmaker, estate = await _tx2_stack()
        if forged:
            await _forge(sessionmaker, estate)
        edges = await _edges(sessionmaker)
        async with sessionmaker() as session:
            active_ids = set((await session.execute(select(CCResource.id).where(
                CCResource.lifecycle_state == LIFECYCLE_ACTIVE))).scalars())
            everything = set((await session.execute(select(CCResource.id))).scalars())
        async with await _as(app, sessionmaker, estate, persona) as client:
            visible = await _visible(client)
            for rid in sorted(everything):
                paths = [f"{RESOURCES}{rid}/parents", f"{RESOURCES}{rid}/children",
                         f"{RESOURCES}{rid}/topology"]
                if rid not in visible:
                    for path in paths:
                        assert (await client.get(path)).status_code == 404, (persona, path)
                    continue
                chain, any_kids, live_kids, subtree = _oracle(
                    edges, visible, rid, active_ids=active_ids)
                parents = (await client.get(paths[0])).json()["parents"]
                assert [p["resource"]["id"] for p in parents] == chain, (persona, rid)
                assert [p["depth"] for p in parents] == list(range(1, len(chain) + 1))
                kids = (await client.get(paths[1], params={
                    "lifecycle_state": "any", "page_size": 200})).json()
                assert {c["resource"]["id"] for c in kids["children"]} == any_kids
                assert kids["total"] == len(any_kids)
                live = (await client.get(paths[1], params={"page_size": 200})).json()
                assert {c["resource"]["id"] for c in live["children"]} == live_kids
                tree = (await client.get(paths[2], params={"depth": MAX_TOPOLOGY_DEPTH})).json()
                assert {n["id"] for n in tree["nodes"]} == subtree, (persona, rid)
                assert tree["truncated"] is False
                shown = {rid} | {n["id"] for n in tree["nodes"]}
                assert all(n["parent_id"] in shown for n in tree["nodes"])

    async def test_the_walks_are_not_vacuous(self):
        """Equality with an oracle over empty answers proves nothing."""
        app, sessionmaker, estate = await _tx2_stack()
        site1 = await _resource(sessionmaker, LEGACY_SITE, estate.sites["site-1"])
        rk1a = await _rack(sessionmaker, "rk-1a")
        node1 = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-1")
        gone1 = await _resource(sessionmaker, LEGACY_DEVICE, "node-gone-1")
        async with await _as(app, sessionmaker, estate, "site_1") as client:
            chain = (await client.get(f"{RESOURCES}{node1.id}/parents")).json()["parents"]
            assert [p["resource"]["id"] for p in chain] == [rk1a.id, site1.id]
            assert [p["relationship"]["relationship_type"] for p in chain] == [
                REL_CONTAINS, REL_CONTAINS]
            tree = (await client.get(f"{RESOURCES}{site1.id}/topology")).json()
            assert {n["id"] for n in tree["nodes"]} >= {rk1a.id, node1.id}
            retired = (await client.get(f"{RESOURCES}{rk1a.id}/children",
                                        params={"lifecycle_state": "retired"})).json()
            # A tombstone is still placed where it was last seen.
            assert [c["resource"]["id"] for c in retired["children"]] == [gone1.id]

    @pytest.mark.parametrize("persona,agent", [
        ("device_3", "node-site-3"), ("class_switch", "node-switch-2"),
        ("device_1", "node-site-1"),
    ])
    async def test_a_device_or_class_reader_sees_no_container_and_no_breadcrumb(
        self, persona, agent,
    ):
        app, sessionmaker, estate = await _tx2_stack()
        device = await _resource(sessionmaker, LEGACY_DEVICE, agent)
        async with await _as(app, sessionmaker, estate, persona) as client:
            assert await _catalogue(client, kind=KIND_PHYSICAL_CONTAINER) == []
            detail = (await client.get(f"{RESOURCES}{device.id}")).json()
            parents = (await client.get(f"{RESOURCES}{device.id}/parents")).json()
        assert detail["primary_parent"] is None and parents["parents"] == []

    async def test_hidden_and_nonexistent_read_alike_on_the_topology_reads(self):
        app, sessionmaker, estate = await _tx2_stack()
        rk2a = await _rack(sessionmaker, "rk-2a")
        async with await _as(app, sessionmaker, estate, "site_1") as client:
            for tail in ("parents", "children", "topology"):
                hidden = await client.get(f"{RESOURCES}{rk2a.id}/{tail}")
                missing = await client.get(f"{RESOURCES}{'0' * 32}/{tail}")
                assert hidden.status_code == missing.status_code == 404
                assert hidden.json() == missing.json()

    async def test_a_subtree_is_bounded_and_its_cut_counts_only_visible_nodes(
        self, monkeypatch,
    ):
        app, sessionmaker, estate = await _tx2_stack()
        site1 = await _resource(sessionmaker, LEGACY_SITE, estate.sites["site-1"])
        node3 = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-3")
        # Forge a HIDDEN child under site-1: node-site-3 "inside" it.
        async with sessionmaker() as session:
            await session.execute(update(CCResourceRelationship)
                                  .where(CCResourceRelationship.to_resource_id == node3.id,
                                         CCResourceRelationship.lifecycle_state == LIFECYCLE_ACTIVE)
                                  .values(from_resource_id=site1.id))
            await session.commit()
        # site-1's live subtree for its own reader: rk-1a, rk-1b, node-site-1,
        # node-blank-1 -- four visible; the forged fifth is hidden from them.
        monkeypatch.setattr(resources_api, "MAX_TOPOLOGY_NODES", 4)
        async with await _as(app, sessionmaker, estate, "site_1") as client:
            tree = (await client.get(f"{RESOURCES}{site1.id}/topology")).json()
            assert len(tree["nodes"]) == 4 and tree["truncated"] is False
            assert node3.id not in {n["id"] for n in tree["nodes"]}
            for depth in (0, MAX_TOPOLOGY_DEPTH + 1):
                assert (await client.get(f"{RESOURCES}{site1.id}/topology",
                                         params={"depth": depth})).status_code == 422
        async with await _as(app, sessionmaker, estate, "owner") as client:
            tree = (await client.get(f"{RESOURCES}{site1.id}/topology")).json()
            assert len(tree["nodes"]) == 4 and tree["truncated"] is True
            assert tree["limit"] == 4

    async def test_a_machine_principal_is_refused_on_the_topology_reads(self):
        app, sessionmaker, estate = await _tx2_stack()
        site1 = await _resource(sessionmaker, LEGACY_SITE, estate.sites["site-1"])

        async def _machine():
            return UserContext(
                user_id="agent-tx2", email="", tenant_id=TENANT, role="",
                permissions=["fleet.view", "incident.view", "proposal.submit"],
                species=SPECIES_AGENT,
                machine_jobs=frozenset({"self", "attention", "incidents",
                                        "proposals", "autonomy"}),
            )

        app.dependency_overrides[get_current_user] = _machine
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for tail in ("parents", "children", "topology"):
                res = await client.get(f"{RESOURCES}{site1.id}/{tail}")
                assert res.status_code == 403, (tail, res.status_code)
                assert "External Agent API plane" in res.text


# ---------------------------------------------------------------------------
# 6. The A32 invariants
# ---------------------------------------------------------------------------

INVARIANT_PERSONAS = ("owner", "region_a", "site_1", "device_3", "device_1",
                      "class_switch", "mixed", "narrowed", "ungranted")


async def _grant_all(app, sessionmaker, estate):
    for persona in INVARIANT_PERSONAS:
        await (await _as(app, sessionmaker, estate, persona)).aclose()


async def _canonical(app):
    """Every canonical read, for every persona, byte for byte."""
    out = {}
    for persona in INVARIANT_PERSONAS:
        async with _client(app, TOKEN_ROLE[persona], f"kc-tx2-{persona}") as client:
            for path in CANONICAL_READS:
                res = await client.get(path)
                out[(persona, path)] = (res.status_code, res.text)
    return out


async def _visible_sets(app):
    out = {}
    for persona in INVARIANT_PERSONAS:
        async with _client(app, TOKEN_ROLE[persona], f"kc-tx2-{persona}") as client:
            out[persona] = await _visible(client)
    return out


class TestInvariants:
    async def test_racks_and_relationships_grant_nothing(self):
        """The same fleet and sites with the rack report and the whole
        catalogue present, and with both absent: every canonical read for
        every persona -- the resolved scope included -- is byte-identical."""
        app, sessionmaker, estate = await _tx2_stack()
        await _grant_all(app, sessionmaker, estate)
        populated = await _canonical(app)
        async with sessionmaker() as session:
            assert (await session.execute(select(CCResource).where(
                CCResource.legacy_kind == LEGACY_RACK))).scalars().first()
            for model in (CCResourceExternalId, CCResourceRelationship, CCResource,
                          CCSiteTopology):
                await session.execute(delete(model))
            await session.commit()
        absent = await _canonical(app)
        assert populated == absent
        for key, racks in RACKS.items():
            await _report(sessionmaker, estate, key, racks)
        await _reconcile(sessionmaker)
        assert await _canonical(app) == absent

    async def test_forged_containment_moves_nothing_and_shows_nothing_hidden(self):
        app, sessionmaker, estate = await _tx2_stack()
        await _grant_all(app, sessionmaker, estate)
        canonical = await _canonical(app)
        visible = await _visible_sets(app)

        site1 = await _resource(sessionmaker, LEGACY_SITE, estate.sites["site-1"])
        site3 = await _resource(sessionmaker, LEGACY_SITE, estate.sites["site-3"])
        rk1a, rk2a = await _rack(sessionmaker, "rk-1a"), await _rack(sessionmaker, "rk-2a")
        node1 = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-1")
        node3 = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-3")
        async with sessionmaker() as session:
            # A topology LIE in every direction: site-3's device inside
            # site-1's rack; site-1's device inside site-3; site-2's rack
            # inside site-3; site-3 inside site-1's rack.
            for child, parent in ((node3, rk1a), (node1, site3), (rk2a, site3)):
                await session.execute(update(CCResourceRelationship).where(
                    CCResourceRelationship.to_resource_id == child.id,
                    CCResourceRelationship.lifecycle_state == LIFECYCLE_ACTIVE,
                ).values(from_resource_id=parent.id))
            session.add(CCResourceRelationship(
                tenant_id=TENANT, from_resource_id=rk1a.id, to_resource_id=site3.id,
                relationship_type=REL_CONTAINS, is_primary=True, source="lie",
            ))
            await session.commit()
        assert await _canonical(app) == canonical
        assert await _visible_sets(app) == visible
        async with _client(app, TOKEN_ROLE["site_1"], "kc-tx2-site_1") as client:
            kids = (await client.get(f"{RESOURCES}{rk1a.id}/children",
                                     params={"lifecycle_state": "any"})).json()
            shown = {c["resource"]["id"] for c in kids["children"]}
            assert node3.id not in shown and site3.id not in shown
            tree = (await client.get(f"{RESOURCES}{site1.id}/topology",
                                     params={"depth": MAX_TOPOLOGY_DEPTH})).json()
            assert not {n["id"] for n in tree["nodes"]} & {node3.id, site3.id, rk2a.id}
            # node-site-1's forged parent is hidden: the chain stops, and
            # reads as if it ended at a root.
            assert (await client.get(f"{RESOURCES}{node1.id}/parents")).json()["parents"] == []
            assert (await client.get(f"{RESOURCES}{node3.id}")).status_code == 404

    async def test_a_shared_rack_confers_nothing_on_a_sibling(self):
        app, sessionmaker, estate = await _tx2_stack()
        rk1a = await _rack(sessionmaker, "rk-1a")
        node1 = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-1")
        blank = await _resource(sessionmaker, LEGACY_DEVICE, "node-blank-1")
        async with await _as(app, sessionmaker, estate, "device_1") as client:
            fleet = {d["agent_id"] for d in (await client.get("/api/fleet/")).json()["devices"]}
            assert fleet == {"node-site-1"}
            assert await _visible(client) == {node1.id}
            for path in (f"{RESOURCES}{blank.id}", f"{RESOURCES}{rk1a.id}",
                         f"{RESOURCES}{rk1a.id}/children", f"{RESOURCES}{rk1a.id}/topology"):
                assert (await client.get(path)).status_code == 404, path
            tree = (await client.get(f"{RESOURCES}{node1.id}/topology")).json()
            assert tree["nodes"] == []

    async def test_a_tenant_owned_container_above_the_sites_confers_nothing(self):
        """A region over site-1 and site-3 -- the shape A32.6 reserves --
        is visible to a tenant-wide reader and to nobody else, and a reader
        of site-1 learns nothing of site-3 through the container they share."""
        app, sessionmaker, estate = await _tx2_stack()
        await _grant_all(app, sessionmaker, estate)
        canonical = await _canonical(app)
        before = await _visible_sets(app)
        site1 = await _resource(sessionmaker, LEGACY_SITE, estate.sites["site-1"])
        site3 = await _resource(sessionmaker, LEGACY_SITE, estate.sites["site-3"])
        async with sessionmaker() as session:
            region = await _insert(session, KIND_PHYSICAL_CONTAINER, "harkeniq:geography.region")
            repo = ResourceRepo(session)
            for site in (site1, site3):
                await repo.set_primary_parent(TENANT, site.id, region.id, source="fixture")
            await session.commit()
        assert await _canonical(app) == canonical
        after = await _visible_sets(app)
        assert after["owner"] == before["owner"] | {region.id}
        assert {p: s for p, s in after.items() if p != "owner"} == {
            p: s for p, s in before.items() if p != "owner"}
        async with _client(app, TOKEN_ROLE["owner"], "kc-tx2-owner") as client:
            assert [p["resource"]["id"] for p in (await client.get(
                f"{RESOURCES}{site1.id}/parents")).json()["parents"]] == [region.id]
            kids = (await client.get(f"{RESOURCES}{region.id}/children")).json()
            assert {c["resource"]["id"] for c in kids["children"]} == {site1.id, site3.id}
        for persona in ("site_1", "region_a", "mixed"):
            async with _client(app, TOKEN_ROLE[persona], f"kc-tx2-{persona}") as client:
                assert (await client.get(f"{RESOURCES}{region.id}")).status_code == 404
                assert (await client.get(
                    f"{RESOURCES}{site1.id}/parents")).json()["parents"] == []

    async def test_capabilities_are_not_inferred_from_the_topology(self):
        app, sessionmaker, estate = await _tx2_stack(mirror=False)
        await _grant_row(sessionmaker, "kc-tx2-cap", SCOPE_TENANT, role="tenant_owner")
        async with _client(app, "tenant_owner", "kc-tx2-cap") as client:
            before = (await client.get("/api/capabilities/")).text
            await _reconcile(sessionmaker)
            async with sessionmaker() as session:
                await session.execute(update(CCResource).values(resource_type=TYPE_RACK))
                await session.commit()
            after = (await client.get("/api/capabilities/")).text
        assert after == before

    async def test_the_site_stays_the_anchor_and_is_never_contained_by_the_mirror(self):
        app, sessionmaker, estate = await _tx2_stack()
        for key in ("site-1", "site-2", "site-3"):
            site = await _resource(sessionmaker, LEGACY_SITE, estate.sites[key])
            assert await _parent_of(sessionmaker, site.id) is None


# ---------------------------------------------------------------------------
# 7. Structure
# ---------------------------------------------------------------------------

#: The only files that may name the Site Manager's rack report.
TOPOLOGY_FILES = {
    "fleet_poller.py", "resource_mirror.py", "sm_client.py",
    "db/models.py", "db/repos.py",
    "db/migrations/versions/0031_resource_topology.py",
}
TOPOLOGY_NAMES = (
    "CCSiteTopology", "cc_site_topology", "SiteTopologyRepo", "clean_topology_racks",
)
#: Every write primitive of the catalogue: the mirror is its one caller.
WRITE_PRIMITIVES = {
    "create_mirrored", "refresh_mirrored", "retire", "revive",
    "set_primary_parent", "add_relationship", "bind_external_id",
}


def _calls(attr_names):
    found = []
    for path in sorted(CC_SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr in attr_names):
                found.append((path.relative_to(CC_SRC).as_posix(), node.func.attr))
    return found


class TestStructure:
    def test_the_rack_report_has_one_writer_and_one_reader(self):
        offenders = []
        for path in sorted(CC_SRC.rglob("*.py")):
            rel = path.relative_to(CC_SRC).as_posix()
            if rel in TOPOLOGY_FILES or "__pycache__" in rel:
                continue
            hits = [n for n in TOPOLOGY_NAMES if n in path.read_text()]
            if hits:
                offenders.append(f"{rel}: {hits}")
        assert not offenders, (
            "the rack report is a description the mirror reads (A32.4); these "
            "modules reach it:\n  " + "\n  ".join(offenders)
        )
        writers = set()
        for path in sorted(CC_SRC.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "upsert"
                        and isinstance(node.func.value, ast.Call)
                        and getattr(node.func.value.func, "id", "") == "SiteTopologyRepo"):
                    writers.add(path.relative_to(CC_SRC).as_posix())
        assert writers == {"fleet_poller.py"}, writers

    def test_only_the_mirror_writes_the_catalogue(self):
        callers = {(f, a) for f, a in _calls(WRITE_PRIMITIVES)
                   if f not in ("db/repos.py",)}
        assert {f for f, _a in callers} == {"resource_mirror.py"}, callers
        # No production writer of a secondary edge exists yet (A32.11).
        assert not [c for c in callers if c[1] == "add_relationship"]

    def test_visibility_still_reads_nothing_the_catalogue_stores(self):
        source = ast.get_source_segment(
            pathlib.Path(repos_module.__file__).read_text(),
            next(n for n in ast.parse(pathlib.Path(repos_module.__file__).read_text()).body
                 if isinstance(n, ast.FunctionDef) and n.name == "scope_resources"),
        )
        body = source.split('"""')[-1]
        assert "LEGACY_RACK" in body and "site_rule" in body
        for forbidden in ("CCResourceRelationship", "CCSiteTopology", "resource_type",
                          "display_name", "lifecycle_state", "contextual", "list_context"):
            assert forbidden not in body, forbidden
        assert scope_resources is repos_module.scope_resources

    def test_central_command_never_reads_a_rack_suggestion(self):
        """A31.3: operator-assigned racks only -- the name heuristic lives at
        the Site Manager and never crosses."""
        hits = [p.relative_to(CC_SRC).as_posix() for p in CC_SRC.rglob("*.py")
                if "__pycache__" not in p.parts and "rack_suggestion" in p.read_text()]
        assert hits == []

    def test_the_surface_moved_by_exactly_three_human_reads(self):
        read = ("fleet.view", READ_SCOPED, False)
        for tail in ("parents", "children", "topology"):
            key = ("GET", f"/api/resources/{{resource_id}}/{tail}")
            assert ROUTE_CONTRACT[key] == read
            assert key not in MACHINE_SURFACE
        assert len(ROUTE_CONTRACT) == 105 and len(MACHINE_SURFACE) == 14

    async def test_every_catalogue_handler_consumes_the_scope_it_declares(self):
        app, _sessionmaker, _estate = await _stack()
        problems = [p for p in census(app) if "/api/resources" in p]
        assert problems == []

    def test_the_permissions_and_the_ceiling_are_unchanged(self):
        from harkeniq_cc.machine_identity import MACHINE_PRINCIPAL_CEILING
        from harkeniq_console.permissions import PERMISSIONS

        assert len(PERMISSIONS) == 25
        assert set(MACHINE_PRINCIPAL_CEILING) == {
            "fleet.view", "incident.view", "proposal.submit",
        }
        assert not {p for p in PERMISSIONS if "resource" in p or "topology" in p}
