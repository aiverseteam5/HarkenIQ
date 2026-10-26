"""TX-3 (spec A33): external identity, provenance, resource typing and
tombstones.

The catalogue now keeps what a resource IS -- its opaque id and its one
identifier -- apart from what it is CALLED (aliases, never unique) and from
where each fact CAME FROM (provenance). It still decides nothing. These
tests hold A33 to its letter against the production app and the real
mirror, every persona under STRICT through real grants:

1. **Vocabulary** -- the closed alias vocabulary; serial-like evidence never
   includes a name; the type registry in three statuses.
2. **External identity** -- one identifier per resource, for life;
   idempotent; a conflict refused and left alone; tenant-keyed, declared
   and enforced.
3. **Aliases** -- never unique across resources; one row per (resource,
   type, value, origin) for life; retired and revived, never deleted; the
   mirror's service tags and former names.
4. **Provenance** -- an origin and an observation on every fact, from the
   canonical object's own stamp: bounded, never backwards, frozen at
   retirement, forced at revival, absent for a held placement, recorded
   for a row written before TX-3.
5. **Reconciliation** -- identity candidates equal an INDEPENDENT oracle for
   every persona, over the mirrored estate and a forged one; nothing ever
   merges; a hidden counterpart is never shown or counted.
6. **Tombstones** -- addressable by id, identifier and alias, and through
   the agent id an incident carries; never deleted by any production path.
7. **The surface** -- the detail, the alias lookup, edge provenance; no
   route added.
8. **The A33 invariants** -- identifiers, aliases, types, provenance,
   tombstones and candidates grant nothing: every canonical read, for every
   persona, is byte-identical with them present, absent and forged;
   `device_class` and capability are unchanged.
"""

from __future__ import annotations

import ast
import pathlib
import typing
from datetime import datetime, timedelta, timezone

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
    CCIncident,
    CCResource,
    CCResourceAlias,
    CCResourceExternalId,
    CCResourceRelationship,
    CCSite,
)
from harkeniq_cc.db.repos import ResourceRepo, scope_resources
from harkeniq_cc.machine_identity import SPECIES_AGENT
from harkeniq_cc.resource_catalogue import (
    ALIAS_ASSET_TAG,
    ALIAS_HISTORICAL_NAME,
    ALIAS_HOSTNAME,
    ALIAS_OPERATOR_LABEL,
    ALIAS_SERVICE_TAG,
    ALIAS_TYPES,
    BUILTIN_TYPES,
    EXTERNAL_SOURCE_HARKENIQ,
    FEED_FLEET_SNAPSHOT,
    FEED_SITE_REGISTRY,
    FEED_SITE_TOPOLOGY,
    IDENTITY_EVIDENCE_ALIAS_TYPES,
    KIND_PHYSICAL_CONTAINER,
    KIND_PHYSICAL_DEVICE,
    LEGACY_DEVICE,
    LEGACY_FEEDS,
    LEGACY_KINDS,
    LEGACY_RACK,
    LEGACY_SITE,
    LIFECYCLE_ACTIVE,
    LIFECYCLE_RETIRED,
    MAX_ALIAS_LENGTH,
    MAX_IDENTITY_CANDIDATES,
    MIRROR_FEEDS,
    NAMESPACE_HARKEN_NODE,
    NAMESPACE_RACK,
    NAMESPACE_SITE,
    OBSERVATION_REFRESH,
    RESERVED_TYPES,
    SOURCE_LEGACY_MIRROR,
    TYPE_RACK,
    TYPE_SERVER,
    TYPE_SITE,
    TYPE_SWITCH,
    TYPE_UNCLASSIFIED_DEVICE,
    CatalogueError,
    IdentityConflict,
    is_well_formed_type,
    normalize_alias,
    validate_alias,
    validate_type,
)
from harkeniq_cc.route_contract import MACHINE_SURFACE, ROUTE_CONTRACT
from harkeniq_cc.scope import SCOPE_TENANT

from tests.unit.cc.test_e1_persona_matrix import TENANT, _client
from tests.unit.cc.test_tx1_resource_catalogue import (
    CANONICAL_READS,
    CC_SRC,
    RESOURCES,
    _bare,
    _catalogue,
    _grant_row,
    _insert,
    _lapse,
    _move_and_retire,
    _reconcile,
    _resource,
)
from tests.unit.cc.test_tx2_resource_topology import (
    PERSONAS,
    TOKEN_ROLE,
    _rack,
    _report,
    _tx2_stack,
)

# ---------------------------------------------------------------------------
# The estate: TX-2's, plus what each Harken Node reports in its service_tag
# field, plus one reinstalled node.
#
#   node-site-1 (site-1) and node-site-3 (site-3) report ONE serial, in
#   different case and spacing -- two sites, so a reader of one site never
#   sees the other as a candidate, and a reader of both does;
#   node-gone-1 (site-1) is reinstalled as node-reinstalled at the SAME
#   site: a new Harken Node identity, the same box, and node-gone-1 a
#   tombstone after the move pass;
#   the switch's "service tag" is its base MAC, as SONiC reports it.
# ---------------------------------------------------------------------------

TAGS = {
    "node-site-1": "TAG-SHARED",
    "node-site-3": "  tag-shared ",
    "node-site-2": "TAG-2",
    "node-switch-2": "aa:bb:cc:dd:ee:02",
    "node-dual": "TAG-DUAL",
    "node-moved": "TAG-MOVED",
    "node-gone-1": "TAG-REINSTALL",
    "node-reinstalled": "TAG-REINSTALL",
    "node-gone-3": "TAG-GONE-3",
}

INVARIANT_PERSONAS = ("owner", "region_a", "site_1", "device_3", "device_1",
                      "class_switch", "mixed", "narrowed", "ungranted")


def _utc(value):
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


async def _tx3_stack(*, mirror: bool = True):
    app, sessionmaker, estate = await _tx2_stack(mirror=False)
    async with sessionmaker() as session:
        session.add(CCFleetCache(
            site_id=estate.sites["site-1"], agent_id="node-reinstalled",
            agent_name="node-reinstalled", vendor="Dell", model="R750",
            health="ok", device_class="server",
        ))
        await session.flush()
        for agent, tag in TAGS.items():
            await session.execute(
                update(CCFleetCache).where(CCFleetCache.agent_id == agent)
                .values(service_tag=tag)
            )
        await session.commit()
    if mirror:
        await _reconcile(sessionmaker)
        await _move_and_retire(sessionmaker, estate)
        await _reconcile(sessionmaker)
    return app, sessionmaker, estate


async def _as(app, sessionmaker, estate, persona):
    user_id = f"kc-tx3-{persona}"
    for scope_type, key, role, subset, lapse in PERSONAS[persona]:
        ref = estate.sites.get(key) or estate.units.get(key) or key
        grant_id = await _grant_row(
            sessionmaker, user_id, scope_type, ref, role=role, subset=subset
        )
        if lapse:
            await _lapse(sessionmaker, grant_id, how=lapse)
    return _client(app, TOKEN_ROLE[persona], user_id)


async def _rows(sessionmaker, model):
    async with sessionmaker() as session:
        return (await session.execute(select(model))).scalars().all()


async def _visible(client) -> set[str]:
    return {r["id"] for r in await _catalogue(client, lifecycle="any")}


async def _detail(client, rid) -> dict:
    res = await client.get(f"{RESOURCES}{rid}")
    assert res.status_code == 200, (rid, res.status_code, res.text)
    return res.json()


async def _by_alias(client, alias_type, value, lifecycle="any") -> set[str]:
    res = await client.get(RESOURCES, params={
        "alias_type": alias_type, "alias": value,
        "lifecycle_state": lifecycle, "page_size": 200,
    })
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["total"] == len(body["resources"])
    return {r["id"] for r in body["resources"]}


async def _set_seen(sessionmaker, site_id, when):
    async with sessionmaker() as session:
        await session.execute(update(CCSite).where(CCSite.id == site_id)
                              .values(last_seen_at=when))
        await session.commit()


async def _set_snapshot(sessionmaker, agent_id, when):
    async with sessionmaker() as session:
        await session.execute(update(CCFleetCache).where(CCFleetCache.agent_id == agent_id)
                              .values(snapshot_at=when))
        await session.commit()


async def _site_row(sessionmaker, site_id):
    async with sessionmaker() as session:
        return await session.get(CCSite, site_id)


async def _fresh(sessionmaker, rid):
    async with sessionmaker() as session:
        return await session.get(CCResource, rid)


# ---------------------------------------------------------------------------
# 1. Vocabulary
# ---------------------------------------------------------------------------


class TestVocabulary:
    def test_the_alias_types_are_the_six_and_closed(self):
        assert ALIAS_TYPES == {
            "hostname", "fqdn", "service_tag", "asset_tag",
            "operator_label", "historical_name",
        }
        with pytest.raises(CatalogueError, match="closed"):
            validate_alias("mac_address", "aa:bb")

    def test_the_alias_filter_is_exactly_the_vocabulary(self):
        assert set(typing.get_args(resources_api.AliasTypeFilter)) == set(ALIAS_TYPES)

    def test_identity_evidence_is_serial_like_and_never_a_name(self):
        assert IDENTITY_EVIDENCE_ALIAS_TYPES == {ALIAS_SERVICE_TAG, ALIAS_ASSET_TAG}
        assert IDENTITY_EVIDENCE_ALIAS_TYPES < ALIAS_TYPES
        for name in ("hostname", "fqdn", "operator_label", "historical_name"):
            assert name not in IDENTITY_EVIDENCE_ALIAS_TYPES

    def test_an_alias_is_compared_normalized_and_shown_as_reported(self):
        assert validate_alias(ALIAS_SERVICE_TAG, "  ab  CD1 ") == ("ab  CD1", "ab cd1")
        assert normalize_alias("TAG-SHARED") == normalize_alias("  tag-shared ")
        for blank in ("", "   ", None):
            with pytest.raises(CatalogueError, match="value"):
                validate_alias(ALIAS_HOSTNAME, blank)
        with pytest.raises(CatalogueError, match="at most"):
            validate_alias(ALIAS_HOSTNAME, "x" * (MAX_ALIAS_LENGTH + 1))

    def test_the_registry_holds_three_statuses(self):
        # REGISTERED: exactly what the mirror writes -- unchanged by TX-3.
        assert set(BUILTIN_TYPES) == {
            TYPE_SITE, TYPE_RACK, TYPE_SERVER, TYPE_SWITCH, TYPE_UNCLASSIFIED_DEVICE,
        }
        # RESERVED: A32.6's nine containers and A33.5's four device types.
        assert set(RESERVED_TYPES) == {
            "harkeniq:geography.region", "harkeniq:facility.campus",
            "harkeniq:facility.datacenter", "harkeniq:facility.building",
            "harkeniq:facility.room", "harkeniq:facility.hall",
            "harkeniq:placement.zone", "harkeniq:placement.row",
            "harkeniq:enclosure.chassis",
            "harkeniq:compute.rack_server", "harkeniq:compute.gpu_server",
            "harkeniq:network.ethernet_switch", "harkeniq:storage.block_array",
        }
        assert not set(BUILTIN_TYPES) & set(RESERVED_TYPES)
        for name in RESERVED_TYPES:
            assert is_well_formed_type(name) and name.startswith("harkeniq:")

    @pytest.mark.parametrize("reserved", sorted(RESERVED_TYPES))
    def test_a_reserved_type_is_refused_in_its_own_kind_too(self, reserved):
        """Reserved means named, not writable -- even in the kind it is for."""
        with pytest.raises(CatalogueError, match="reserved"):
            validate_type(reserved, RESERVED_TYPES[reserved])

    def test_reserved_kinds_are_fixed(self):
        devices = {t for t, k in RESERVED_TYPES.items() if k == KIND_PHYSICAL_DEVICE}
        assert devices == {
            "harkeniq:compute.rack_server", "harkeniq:compute.gpu_server",
            "harkeniq:network.ethernet_switch", "harkeniq:storage.block_array",
        }
        assert set(RESERVED_TYPES.values()) == {KIND_PHYSICAL_CONTAINER, KIND_PHYSICAL_DEVICE}

    @pytest.mark.parametrize("unknown", [
        "acme:compute.blade", "servicenow:cmdb_ci.server", "vmware:compute.vm",
        "harkeniq:compute.blade_server", "harkeniq:storage.nas",
    ])
    def test_an_unknown_type_is_refused_in_any_namespace(self, unknown):
        with pytest.raises(CatalogueError, match="not in the built-in registry"):
            validate_type(unknown, KIND_PHYSICAL_DEVICE)

    def test_every_legacy_kind_has_its_feed(self):
        assert set(LEGACY_FEEDS) == set(LEGACY_KINDS)
        assert set(LEGACY_FEEDS.values()) == set(MIRROR_FEEDS) == {
            FEED_SITE_REGISTRY, FEED_FLEET_SNAPSHOT, FEED_SITE_TOPOLOGY,
        }
        assert OBSERVATION_REFRESH == timedelta(hours=1)


# ---------------------------------------------------------------------------
# 2. External identity
# ---------------------------------------------------------------------------


class TestExternalIdentity:
    async def test_one_identifier_per_resource_a_second_is_refused(self):
        sessionmaker, _estate = await _bare()
        async with sessionmaker() as session:
            repo = ResourceRepo(session)
            dev = await _insert(session, KIND_PHYSICAL_DEVICE, TYPE_SERVER)
            first = await repo.bind_external_id(
                TENANT, dev.id, source_system="harkeniq", namespace="harken_node",
                external_id="n-1", source="s",
            )
            for system, namespace, value in (
                ("openmanage", "device", "1234"),       # another system
                ("harkeniq", "harken_node", "n-2"),     # the same namespace
            ):
                with pytest.raises(IdentityConflict, match="reconciliation"):
                    await repo.bind_external_id(
                        TENANT, dev.id, source_system=system, namespace=namespace,
                        external_id=value, source="s",
                    )
            rows = (await session.execute(select(CCResourceExternalId))).scalars().all()
            assert [(r.id, r.external_id) for r in rows] == [(first.id, "n-1")]

    async def test_registration_is_idempotent_and_observed(self):
        sessionmaker, _estate = await _bare()
        when = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
        async with sessionmaker() as session:
            repo = ResourceRepo(session)
            dev = await _insert(session, KIND_PHYSICAL_DEVICE, TYPE_SERVER)
            kw = dict(source_system="cmdb", namespace="sys_id", external_id="X1",
                      source="s", observed_at=when)
            first = await repo.bind_external_id(TENANT, dev.id, **kw)
            again = await repo.bind_external_id(TENANT, dev.id, **kw)
            assert again.id == first.id
            assert _utc(first.first_observed_at) == _utc(first.last_observed_at) == when

    async def test_the_mirror_binds_one_harkeniq_identifier_each_and_never_a_serial(self):
        app, sessionmaker, estate = await _tx3_stack()
        resources = await _rows(sessionmaker, CCResource)
        bindings = await _rows(sessionmaker, CCResourceExternalId)
        by_resource: dict[str, list] = {}
        for b in bindings:
            by_resource.setdefault(b.resource_id, []).append(b)
        assert set(by_resource) == {r.id for r in resources}
        expected_ns = {LEGACY_SITE: NAMESPACE_SITE, LEGACY_DEVICE: NAMESPACE_HARKEN_NODE,
                       LEGACY_RACK: NAMESPACE_RACK}
        for r in resources:
            (b,) = by_resource[r.id]
            assert (b.source_system, b.namespace, b.external_id) == (
                EXTERNAL_SOURCE_HARKENIQ, expected_ns[r.legacy_kind], r.legacy_ref)
        serials = {normalize_alias(t) for t in TAGS.values()}
        assert not {normalize_alias(b.external_id) for b in bindings} & serials

    def test_both_tenant_keys_are_declared(self):
        for model, name in ((CCResourceExternalId, "fk_resource_external_id_tenant"),
                            (CCResourceAlias, "fk_resource_alias_tenant")):
            keyed = {
                fk.name: (tuple(fk.column_keys),
                          tuple(e.target_fullname for e in fk.elements))
                for fk in model.__table__.foreign_key_constraints
            }
            assert keyed[name] == (("resource_id", "tenant_id"),
                                   ("cc_resources.id", "cc_resources.tenant_id"))

    @pytest.mark.parametrize("table", ["binding", "alias"])
    async def test_the_database_refuses_a_row_naming_another_tenants_resource(self, table):
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
                theirs = await _insert(session, KIND_PHYSICAL_DEVICE, TYPE_SERVER,
                                       tenant="other-tenant")
                await session.commit()
                if table == "binding":
                    session.add(CCResourceExternalId(
                        tenant_id=TENANT, resource_id=theirs.id, source_system="cmdb",
                        namespace="sys_id", external_id="X", source="raw",
                    ))
                else:
                    session.add(CCResourceAlias(
                        tenant_id=TENANT, resource_id=theirs.id, alias_type="hostname",
                        value="h", normalized_value="h", source_system="cmdb",
                        source_namespace="ci", source="raw",
                    ))
                with pytest.raises(IntegrityError):
                    await session.commit()
        finally:
            await engine.dispose()

    async def test_an_identifier_survives_retirement_and_revives_its_own_resource(self):
        app, sessionmaker, estate = await _tx3_stack()
        gone = await _resource(sessionmaker, LEGACY_DEVICE, "node-gone-3")
        assert gone.lifecycle_state == LIFECYCLE_RETIRED
        before = {b.id: (b.resource_id, b.external_id)
                  for b in await _rows(sessionmaker, CCResourceExternalId)}
        async with sessionmaker() as session:
            session.add(CCFleetCache(
                site_id=estate.sites["site-2"], agent_id="node-gone-3",
                agent_name="node-gone-3", device_class="server", service_tag="TAG-GONE-3",
            ))
            await session.commit()
        await _reconcile(sessionmaker)
        back = await _resource(sessionmaker, LEGACY_DEVICE, "node-gone-3")
        assert back.id == gone.id and back.lifecycle_state == LIFECYCLE_ACTIVE
        after = {b.id: (b.resource_id, b.external_id)
                 for b in await _rows(sessionmaker, CCResourceExternalId)}
        assert after == before, "an identifier is never moved, added or deleted"


# ---------------------------------------------------------------------------
# 3. Aliases
# ---------------------------------------------------------------------------

_ORIGIN = ("cmdb", "ci")


class TestAliases:
    async def test_two_resources_may_share_an_alias_but_not_one_row_twice(self):
        sessionmaker, _estate = await _bare()
        async with sessionmaker() as session:
            repo = ResourceRepo(session)
            a = await _insert(session, KIND_PHYSICAL_DEVICE, TYPE_SERVER)
            b = await _insert(session, KIND_PHYSICAL_DEVICE, TYPE_SERVER)
            for target in (a, b):
                await repo.create_alias(TENANT, target, alias_type=ALIAS_HOSTNAME,
                                        value="web01", origin=_ORIGIN, source="s")
            # The same alias from ANOTHER origin is another fact, beside it.
            await repo.create_alias(TENANT, a, alias_type=ALIAS_HOSTNAME,
                                    value="WEB01", origin=("dns", "a"), source="s")
            await session.commit()
            session.add(CCResourceAlias(
                tenant_id=TENANT, resource_id=a.id, alias_type=ALIAS_HOSTNAME,
                value="Web01", normalized_value="web01", source_system=_ORIGIN[0],
                source_namespace=_ORIGIN[1], source="raw",
            ))
            with pytest.raises(IntegrityError):
                await session.commit()

    async def test_an_alias_is_asserted_only_of_a_live_resource_of_this_tenant(self):
        sessionmaker, _estate = await _bare()
        async with sessionmaker() as session:
            repo = ResourceRepo(session)
            dev = await _insert(session, KIND_PHYSICAL_DEVICE, TYPE_SERVER)
            foreign = await _insert(session, KIND_PHYSICAL_DEVICE, TYPE_SERVER,
                                    tenant="other-tenant")
            with pytest.raises(CatalogueError, match="this tenant"):
                await repo.create_alias(TENANT, foreign, alias_type=ALIAS_HOSTNAME,
                                        value="h", origin=_ORIGIN, source="s")
            await repo.retire(dev)
            with pytest.raises(CatalogueError, match="live resource"):
                await repo.create_alias(TENANT, dev, alias_type=ALIAS_HOSTNAME,
                                        value="h", origin=_ORIGIN, source="s")
            assert (await session.execute(select(CCResourceAlias))).first() is None

    async def test_retire_and_revive_keep_the_row(self):
        sessionmaker, _estate = await _bare()
        async with sessionmaker() as session:
            repo = ResourceRepo(session)
            dev = await _insert(session, KIND_PHYSICAL_DEVICE, TYPE_SERVER)
            alias = await repo.create_alias(TENANT, dev, alias_type=ALIAS_ASSET_TAG,
                                            value="A-1", origin=_ORIGIN, source="s")
            await repo.retire_alias(alias)
            assert alias.lifecycle_state == LIFECYCLE_RETIRED and alias.retired_at
            await repo.revive_alias(alias, value="a-1")
            rows = (await session.execute(select(CCResourceAlias))).scalars().all()
            assert [(r.id, r.lifecycle_state, r.value, r.retired_at) for r in rows] == [
                (alias.id, LIFECYCLE_ACTIVE, "a-1", None)]

    async def test_the_mirror_records_each_nodes_service_tag(self):
        app, sessionmaker, estate = await _tx3_stack()
        resources = {r.id: r for r in await _rows(sessionmaker, CCResource)}
        aliases = await _rows(sessionmaker, CCResourceAlias)
        tags = {resources[a.resource_id].legacy_ref: a for a in aliases
                if a.alias_type == ALIAS_SERVICE_TAG}
        assert set(tags) == set(TAGS)               # node-blank-1 reports none
        for agent, alias in tags.items():
            assert alias.value == TAGS[agent].strip()
            assert alias.normalized_value == normalize_alias(TAGS[agent])
            assert (alias.source_system, alias.source_namespace, alias.source) == (
                EXTERNAL_SOURCE_HARKENIQ, FEED_FLEET_SNAPSHOT, SOURCE_LEGACY_MIRROR)
            assert alias.lifecycle_state == LIFECYCLE_ACTIVE
        # Only the two types with a source are written.
        assert {a.alias_type for a in aliases} <= {ALIAS_SERVICE_TAG, ALIAS_HISTORICAL_NAME}

    async def test_a_changed_service_tag_retires_the_old_and_a_return_revives_it(self):
        app, sessionmaker, estate = await _tx3_stack()
        node = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-2")

        async def _tags():
            rows = [a for a in await _rows(sessionmaker, CCResourceAlias)
                    if a.resource_id == node.id and a.alias_type == ALIAS_SERVICE_TAG]
            return {a.normalized_value: (a.id, a.lifecycle_state) for a in rows}

        first = await _tags()
        async with sessionmaker() as session:
            await session.execute(update(CCFleetCache).where(
                CCFleetCache.agent_id == "node-site-2").values(service_tag="TAG-NEW-BOARD"))
            await session.commit()
        report = await _reconcile(sessionmaker)
        assert report.aliased == 2
        swapped = await _tags()
        assert swapped["tag-2"] == (first["tag-2"][0], LIFECYCLE_RETIRED)
        assert swapped["tag-new-board"][1] == LIFECYCLE_ACTIVE
        async with sessionmaker() as session:
            await session.execute(update(CCFleetCache).where(
                CCFleetCache.agent_id == "node-site-2").values(service_tag="TAG-2"))
            await session.commit()
        await _reconcile(sessionmaker)
        back = await _tags()
        assert back["tag-2"] == (first["tag-2"][0], LIFECYCLE_ACTIVE), "the same row"
        assert back["tag-new-board"][1] == LIFECYCLE_RETIRED
        assert len(back) == 2

    async def test_a_tombstone_keeps_its_aliases_as_last_asserted(self):
        app, sessionmaker, estate = await _tx3_stack()
        for agent in ("node-gone-1", "node-gone-3"):
            gone = await _resource(sessionmaker, LEGACY_DEVICE, agent)
            assert gone.lifecycle_state == LIFECYCLE_RETIRED
            (alias,) = [a for a in await _rows(sessionmaker, CCResourceAlias)
                        if a.resource_id == gone.id]
            assert alias.lifecycle_state == LIFECYCLE_ACTIVE
            assert alias.normalized_value == normalize_alias(TAGS[agent])

    @pytest.mark.parametrize("what", ["site", "rack", "device"])
    async def test_a_rename_keeps_the_id_and_the_former_name(self, what):
        app, sessionmaker, estate = await _tx3_stack()
        if what == "site":
            before = await _resource(sessionmaker, LEGACY_SITE, estate.sites["site-2"])
            async with sessionmaker() as session:
                (await session.get(CCSite, estate.sites["site-2"])).site_name = "Site Two"
                await session.commit()
        elif what == "rack":
            before = await _rack(sessionmaker, "rk-2a")
            await _report(sessionmaker, estate, "site-2",
                          [("rk-2a", "B01-renamed", ["node-switch-2"])])
        else:
            before = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-2")
            async with sessionmaker() as session:
                await session.execute(update(CCFleetCache).where(
                    CCFleetCache.agent_id == "node-site-2").values(agent_name="web-02"))
                await session.commit()
        former = before.display_name
        await _reconcile(sessionmaker)
        after = await _fresh(sessionmaker, before.id)
        assert after.id == before.id and after.display_name != former
        names = [a for a in await _rows(sessionmaker, CCResourceAlias)
                 if a.resource_id == before.id and a.alias_type == ALIAS_HISTORICAL_NAME]
        assert [(a.value, a.lifecycle_state) for a in names] == [(former, LIFECYCLE_ACTIVE)]
        assert (names[0].source_system, names[0].source_namespace) == (
            EXTERNAL_SOURCE_HARKENIQ, LEGACY_FEEDS[before.legacy_kind])
        async with await _as(app, sessionmaker, estate, "owner") as client:
            assert await _by_alias(client, ALIAS_HISTORICAL_NAME, former.upper()) == {before.id}
            assert await _by_alias(client, ALIAS_HISTORICAL_NAME, after.display_name) == set()

    async def test_renaming_back_keeps_both_former_names(self):
        app, sessionmaker, estate = await _tx3_stack()
        site = await _resource(sessionmaker, LEGACY_SITE, estate.sites["site-2"])
        for name in ("Site Two", "site-2"):
            async with sessionmaker() as session:
                (await session.get(CCSite, estate.sites["site-2"])).site_name = name
                await session.commit()
            await _reconcile(sessionmaker)
        names = sorted(a.value for a in await _rows(sessionmaker, CCResourceAlias)
                       if a.resource_id == site.id and a.alias_type == ALIAS_HISTORICAL_NAME)
        assert names == ["Site Two", "site-2"]
        assert (await _fresh(sessionmaker, site.id)).display_name == "site-2"

    async def test_every_replaced_display_name_is_kept_a_fallback_too(self):
        """node-blank-1 has no agent name and was shown by its own id.
        Naming it later keeps that display as a former name too: the
        resource was shown by it, and guessing which names 'count' is how
        a real one -- an agent named after its own id -- would be lost."""
        app, sessionmaker, estate = await _tx3_stack()
        blank = await _resource(sessionmaker, LEGACY_DEVICE, "node-blank-1")
        assert blank.display_name == "node-blank-1"
        async with sessionmaker() as session:
            await session.execute(update(CCFleetCache).where(
                CCFleetCache.agent_id == "node-blank-1").values(agent_name="db-01"))
            await session.commit()
        await _reconcile(sessionmaker)
        assert (await _fresh(sessionmaker, blank.id)).display_name == "db-01"
        assert [(a.alias_type, a.value) for a in await _rows(sessionmaker, CCResourceAlias)
                if a.resource_id == blank.id] == [(ALIAS_HISTORICAL_NAME, "node-blank-1")]

    async def test_a_second_pass_writes_nothing(self):
        app, sessionmaker, estate = await _tx3_stack()
        before = {(m.__name__, r.id): r.updated_at if hasattr(r, "updated_at") else None
                  for m in (CCResource, CCResourceRelationship, CCResourceAlias)
                  for r in await _rows(sessionmaker, m)}
        report = await _reconcile(sessionmaker)
        assert not report.changed and report.conflicts == 0, report
        after = {(m.__name__, r.id): r.updated_at if hasattr(r, "updated_at") else None
                 for m in (CCResource, CCResourceRelationship, CCResourceAlias)
                 for r in await _rows(sessionmaker, m)}
        assert after == before


# ---------------------------------------------------------------------------
# 4. Provenance
# ---------------------------------------------------------------------------


async def _canonical_stamps(sessionmaker):
    """resource id -> the stamp its canonical object carries now (A33.4), for
    every LIVE mirrored resource."""
    async with sessionmaker() as session:
        sites = {s.id: _utc(s.last_seen_at) for s in
                 (await session.execute(select(CCSite))).scalars().all()}
        fleet: dict[str, list] = {}
        for row in (await session.execute(select(CCFleetCache))).scalars().all():
            fleet.setdefault(row.agent_id, []).append(_utc(row.snapshot_at))
        resources = (await session.execute(select(CCResource))).scalars().all()
    out = {}
    for r in resources:
        if r.lifecycle_state != LIFECYCLE_ACTIVE:
            continue
        if r.legacy_kind == LEGACY_SITE:
            out[r.id] = sites[r.legacy_ref]
        elif r.legacy_kind == LEGACY_RACK:
            out[r.id] = sites[r.legacy_site_id]
        else:
            out[r.id] = max(fleet[r.legacy_ref])
    return out


class TestProvenance:
    async def test_every_mirrored_fact_names_its_origin(self):
        app, sessionmaker, estate = await _tx3_stack()
        resources = {r.id: r for r in await _rows(sessionmaker, CCResource)}
        for r in resources.values():
            assert (r.source_system, r.source_namespace) == (
                EXTERNAL_SOURCE_HARKENIQ, LEGACY_FEEDS[r.legacy_kind]), r.legacy_ref
            assert r.first_observed_at and r.last_observed_at
            assert _utc(r.first_observed_at) <= _utc(r.last_observed_at)
        for e in await _rows(sessionmaker, CCResourceRelationship):
            child, parent = resources[e.to_resource_id], resources[e.from_resource_id]
            feed = (FEED_FLEET_SNAPSHOT if parent.legacy_kind == LEGACY_SITE
                    and child.legacy_kind == LEGACY_DEVICE else FEED_SITE_TOPOLOGY)
            assert (e.source_system, e.source_namespace) == (EXTERNAL_SOURCE_HARKENIQ, feed)
            assert _utc(e.first_observed_at) <= _utc(e.last_observed_at)
        for b in await _rows(sessionmaker, CCResourceExternalId):
            assert _utc(b.first_observed_at) <= _utc(b.last_observed_at)
        for a in await _rows(sessionmaker, CCResourceAlias):
            assert a.source_system == EXTERNAL_SOURCE_HARKENIQ
            assert a.source_namespace in MIRROR_FEEDS

    async def test_observation_is_the_canonical_objects_own_stamp(self):
        app, sessionmaker, estate = await _tx3_stack()
        stamps = await _canonical_stamps(sessionmaker)
        resources = {r.id: r for r in await _rows(sessionmaker, CCResource)}
        assert stamps and set(stamps) <= set(resources)
        for rid, stamp in stamps.items():
            assert _utc(resources[rid].last_observed_at) == stamp, resources[rid].legacy_ref
        for b in await _rows(sessionmaker, CCResourceExternalId):
            if b.resource_id in stamps:
                assert _utc(b.last_observed_at) == stamps[b.resource_id]
        for a in await _rows(sessionmaker, CCResourceAlias):
            if a.resource_id in stamps and a.alias_type == ALIAS_SERVICE_TAG:
                assert _utc(a.last_observed_at) == stamps[a.resource_id]
        for e in await _rows(sessionmaker, CCResourceRelationship):
            if e.lifecycle_state != LIFECYCLE_ACTIVE or e.to_resource_id not in stamps:
                continue
            child, parent = resources[e.to_resource_id], resources[e.from_resource_id]
            # A device under its site is observed with the device; anything
            # the rack report states -- a rack in its site, a device in its
            # rack -- with that site's report, which is the rack's own stamp.
            expected = (stamps[child.id] if parent.legacy_kind == LEGACY_SITE
                        and child.legacy_kind == LEGACY_DEVICE else stamps[parent.id])
            assert _utc(e.last_observed_at) == expected, (parent.legacy_ref, child.legacy_ref)

    async def test_observation_is_bounded_never_backwards_and_a_dark_site_does_not_advance(self):
        app, sessionmaker, estate = await _tx3_stack()
        s1, s3 = estate.sites["site-1"], estate.sites["site-3"]
        site1 = await _resource(sessionmaker, LEGACY_SITE, s1)
        site3 = await _resource(sessionmaker, LEGACY_SITE, s3)
        rk1a = await _rack(sessionmaker, "rk-1a")
        t0 = _utc((await _site_row(sessionmaker, s1)).last_seen_at)
        dark = _utc(site3.last_observed_at)
        assert _utc(site1.last_observed_at) == t0

        await _set_seen(sessionmaker, s1, t0 + OBSERVATION_REFRESH / 2)
        report = await _reconcile(sessionmaker)
        assert report.observed == 0 and not report.changed, report
        assert _utc((await _fresh(sessionmaker, site1.id)).last_observed_at) == t0

        later = t0 + 2 * OBSERVATION_REFRESH
        await _set_seen(sessionmaker, s1, later)
        report = await _reconcile(sessionmaker)
        assert report.observed > 0
        for rid in (site1.id, rk1a.id):
            row = await _fresh(sessionmaker, rid)
            assert _utc(row.last_observed_at) == later
            assert _utc(row.first_observed_at) <= t0, "the first observation never moves"
        assert _utc((await _fresh(sessionmaker, site3.id)).last_observed_at) == dark

        await _set_seen(sessionmaker, s1, t0 - OBSERVATION_REFRESH)
        await _reconcile(sessionmaker)
        assert _utc((await _fresh(sessionmaker, site1.id)).last_observed_at) == later

    async def test_retirement_freezes_and_revival_observes_at_once(self):
        app, sessionmaker, estate = await _tx3_stack()
        gone = await _resource(sessionmaker, LEGACY_DEVICE, "node-gone-3")
        frozen = _utc(gone.last_observed_at)
        for _ in range(2):
            await _reconcile(sessionmaker)
        assert _utc((await _fresh(sessionmaker, gone.id)).last_observed_at) == frozen
        soon = frozen + OBSERVATION_REFRESH / 6
        async with sessionmaker() as session:
            session.add(CCFleetCache(
                site_id=estate.sites["site-2"], agent_id="node-gone-3",
                agent_name="node-gone-3", device_class="server",
                service_tag="TAG-GONE-3", snapshot_at=soon,
            ))
            await session.commit()
        report = await _reconcile(sessionmaker)
        assert report.revived == 1
        back = await _fresh(sessionmaker, gone.id)
        assert _utc(back.last_observed_at) == soon, "a revival does not wait an hour"
        assert _utc(back.first_observed_at) == _utc(gone.first_observed_at)

    async def test_a_held_placement_is_not_observed(self):
        """Site-1's report goes UNKNOWN: node-site-1 keeps its rack, and that
        placement -- held, not asserted -- does not advance, while the site
        and the device themselves, still observed, do."""
        app, sessionmaker, estate = await _tx3_stack()
        s1 = estate.sites["site-1"]
        node1 = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-1")
        rk1a = await _rack(sessionmaker, "rk-1a")
        site1 = await _resource(sessionmaker, LEGACY_SITE, s1)
        async with sessionmaker() as session:
            edge = await ResourceRepo(session).primary_parent_edge(node1.id)
        assert edge.from_resource_id == rk1a.id
        held = _utc(edge.last_observed_at)
        await _report(sessionmaker, estate, "site-1", reported=False)
        later = held + 2 * OBSERVATION_REFRESH
        await _set_seen(sessionmaker, s1, later)
        await _set_snapshot(sessionmaker, "node-site-1", later)
        await _reconcile(sessionmaker)
        async with sessionmaker() as session:
            edge = await ResourceRepo(session).primary_parent_edge(node1.id)
        assert edge.from_resource_id == rk1a.id and _utc(edge.last_observed_at) == held
        assert _utc((await _fresh(sessionmaker, rk1a.id)).last_observed_at) == held
        assert _utc((await _fresh(sessionmaker, node1.id)).last_observed_at) == later
        assert _utc((await _fresh(sessionmaker, site1.id)).last_observed_at) == later

    async def test_a_row_written_before_tx3_is_recorded_on_the_next_pass(self):
        app, sessionmaker, estate = await _tx3_stack()
        site1 = await _resource(sessionmaker, LEGACY_SITE, estate.sites["site-1"])
        node1 = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-1")
        stamps = await _canonical_stamps(sessionmaker)
        long_ago = stamps[site1.id] - timedelta(days=1)
        after_seen = stamps[node1.id] + timedelta(days=1)
        nulls = dict(source_system=None, source_namespace=None,
                     first_observed_at=None, last_observed_at=None)
        async with sessionmaker() as session:
            await session.execute(update(CCResource).values(**nulls))
            await session.execute(update(CCResourceRelationship).values(**nulls))
            await session.execute(update(CCResourceExternalId).values(
                first_observed_at=None, last_observed_at=None))
            await session.execute(delete(CCResourceAlias))
            # Created before its site was last seen, and -- a dark site at
            # the upgrade -- after its device was.
            await session.execute(update(CCResource).where(CCResource.id == site1.id)
                                  .values(created_at=long_ago))
            await session.execute(update(CCResource).where(CCResource.id == node1.id)
                                  .values(created_at=after_seen))
            await session.commit()
        report = await _reconcile(sessionmaker)
        assert report.observed > 0 and report.aliased == len(TAGS) - 2  # the two tombstones
        site = await _fresh(sessionmaker, site1.id)
        node = await _fresh(sessionmaker, node1.id)
        assert (site.source_system, site.source_namespace) == (
            EXTERNAL_SOURCE_HARKENIQ, FEED_SITE_REGISTRY)
        assert _utc(site.first_observed_at) == long_ago        # its creation
        assert _utc(site.last_observed_at) == stamps[site1.id]
        assert _utc(node.first_observed_at) == stamps[node1.id]  # never after the last
        assert _utc(node.last_observed_at) == stamps[node1.id]
        for e in await _rows(sessionmaker, CCResourceRelationship):
            if e.lifecycle_state == LIFECYCLE_ACTIVE and e.to_resource_id in stamps:
                assert e.source_system == EXTERNAL_SOURCE_HARKENIQ and e.last_observed_at
        for b in await _rows(sessionmaker, CCResourceExternalId):
            if b.resource_id in stamps:
                assert _utc(b.last_observed_at) == stamps[b.resource_id]
        # A tombstone from before TX-3 is asserted by nobody now: what it
        # never had recorded stays "not recorded", rather than invented.
        gone = await _fresh(sessionmaker, (await _resource(
            sessionmaker, LEGACY_DEVICE, "node-gone-3")).id)
        assert gone.source_system is None and gone.last_observed_at is None

    async def test_a_description_belongs_to_the_source_that_created_it(self):
        sessionmaker, _estate = await _bare()
        async with sessionmaker() as session:
            repo = ResourceRepo(session)
            imported = await _insert(session, KIND_PHYSICAL_DEVICE, TYPE_SERVER)
            with pytest.raises(CatalogueError, match="may not rewrite"):
                await repo.refresh_mirrored(
                    imported, display_name="renamed", resource_type=TYPE_SERVER,
                    legacy_site_id=None,
                )
            assert imported.display_name == f"{KIND_PHYSICAL_DEVICE}-fixture"


# ---------------------------------------------------------------------------
# 5. Reconciliation
# ---------------------------------------------------------------------------


def _candidate_oracle(aliases, visible, rid) -> set[str]:
    """An INDEPENDENT reading, straight from the alias rows: the other
    VISIBLE resources sharing an ACTIVE serial-like alias with `rid`."""
    mine = {(a.alias_type, a.normalized_value) for a in aliases
            if a.resource_id == rid and a.lifecycle_state == LIFECYCLE_ACTIVE
            and a.alias_type in IDENTITY_EVIDENCE_ALIAS_TYPES}
    return {a.resource_id for a in aliases
            if a.resource_id != rid and a.lifecycle_state == LIFECYCLE_ACTIVE
            and (a.alias_type, a.normalized_value) in mine and a.resource_id in visible}


async def _forge_aliases(sessionmaker, estate):
    """Aliases the mirror never writes and a later source might:

    * node-site-2 (site-2) and node-site-3 (site-3) share an ASSET TAG --
      a pair that region A's reader can see one half of;
    * a RETIRED service tag on node-switch-2 equal to TAG-SHARED, which must
      count for nobody;
    * a HISTORICAL NAME on node-blank-1 equal to TAG-SHARED, and node-site-2
      called `node-site-1` -- names, which never make a candidate;
    * a tenant-owned region carrying node-site-2's serial, a candidate only
      a tenant-wide reader can see."""
    node2 = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-2")
    node3 = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-3")
    switch = await _resource(sessionmaker, LEGACY_DEVICE, "node-switch-2")
    blank = await _resource(sessionmaker, LEGACY_DEVICE, "node-blank-1")
    async with sessionmaker() as session:
        region = await _insert(session, KIND_PHYSICAL_CONTAINER, "harkeniq:geography.region")

        def _alias(resource, alias_type, value, state=LIFECYCLE_ACTIVE):
            session.add(CCResourceAlias(
                tenant_id=TENANT, resource_id=resource.id, alias_type=alias_type,
                value=value, normalized_value=normalize_alias(value),
                source_system="forged", source_namespace="raw", source="forged",
                lifecycle_state=state,
            ))

        for target in (node2, node3):
            _alias(target, ALIAS_ASSET_TAG, "ASSET-9")
        _alias(switch, ALIAS_SERVICE_TAG, "TAG-SHARED", LIFECYCLE_RETIRED)
        _alias(blank, ALIAS_HISTORICAL_NAME, "TAG-SHARED")
        _alias(node2, ALIAS_HISTORICAL_NAME, "node-site-1")
        _alias(region, ALIAS_SERVICE_TAG, "TAG-2")
        await session.commit()
    return region


class TestReconciliation:
    @pytest.mark.parametrize("forged", [False, True], ids=["mirrored", "forged"])
    @pytest.mark.parametrize("persona", sorted(PERSONAS))
    async def test_candidates_equal_the_oracle(self, persona, forged):
        app, sessionmaker, estate = await _tx3_stack()
        if forged:
            await _forge_aliases(sessionmaker, estate)
        aliases = await _rows(sessionmaker, CCResourceAlias)
        everything = {r.id for r in await _rows(sessionmaker, CCResource)}
        async with await _as(app, sessionmaker, estate, persona) as client:
            visible = await _visible(client)
            for rid in sorted(everything):
                res = await client.get(f"{RESOURCES}{rid}")
                if rid not in visible:
                    assert res.status_code == 404, (persona, rid)
                    continue
                block = res.json()["identity_candidates"]
                shown = [c["resource"]["id"] for c in block["candidates"]]
                assert shown == sorted(shown), "ordered by id"
                assert set(shown) == _candidate_oracle(aliases, visible, rid), (persona, rid)
                assert block["truncated"] is False
                for c in block["candidates"]:
                    assert c["shared"] and {s["alias_type"] for s in c["shared"]} \
                        <= IDENTITY_EVIDENCE_ALIAS_TYPES

    async def test_candidates_are_not_vacuous(self):
        """Equality with an oracle over empty answers proves nothing."""
        app, sessionmaker, estate = await _tx3_stack()
        node1 = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-1")
        node3 = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-3")
        gone1 = await _resource(sessionmaker, LEGACY_DEVICE, "node-gone-1")
        fresh = await _resource(sessionmaker, LEGACY_DEVICE, "node-reinstalled")

        async def _ids(persona, rid):
            async with await _as(app, sessionmaker, estate, persona) as client:
                return [c["resource"]["id"] for c in
                        (await _detail(client, rid))["identity_candidates"]["candidates"]]

        assert await _ids("owner", node1.id) == [node3.id]
        assert await _ids("owner", node3.id) == [node1.id]
        # A reader holding both sites sees the pair; one holding either does not.
        assert await _ids("mixed", node1.id) == [node3.id]
        assert await _ids("site_1", node1.id) == []
        assert await _ids("device_3", node3.id) == []
        # The reinstalled box: a tombstone and a new node at one site.
        assert await _ids("site_1", fresh.id) == [gone1.id]
        assert await _ids("site_1", gone1.id) == [fresh.id]

    async def test_nothing_ever_merges(self):
        app, sessionmaker, estate = await _tx3_stack()
        node1 = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-1")
        node3 = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-3")
        before = {r.id: (r.legacy_kind, r.legacy_ref, r.lifecycle_state)
                  for r in await _rows(sessionmaker, CCResource)}
        # Same serial, and now the same NAME too.
        async with sessionmaker() as session:
            await session.execute(update(CCFleetCache).where(CCFleetCache.agent_id.in_(
                ["node-site-1", "node-site-3"])).values(agent_name="the-same-box"))
            await session.commit()
        for _ in range(3):
            await _reconcile(sessionmaker)
        after = {r.id: (r.legacy_kind, r.legacy_ref, r.lifecycle_state)
                 for r in await _rows(sessionmaker, CCResource)}
        assert after == before
        assert node1.id != node3.id
        bindings = {(b.resource_id, b.external_id)
                    for b in await _rows(sessionmaker, CCResourceExternalId)}
        assert (node1.id, "node-site-1") in bindings and (node3.id, "node-site-3") in bindings
        async with await _as(app, sessionmaker, estate, "owner") as client:
            assert await _by_alias(client, ALIAS_HISTORICAL_NAME, "node-site-1") == {node1.id}
            named = {r["id"] for r in await _catalogue(client)
                     if r["display_name"] == "the-same-box"}
            assert named == {node1.id, node3.id}

    async def test_a_hidden_counterpart_is_neither_shown_nor_counted(self, monkeypatch):
        app, sessionmaker, estate = await _tx3_stack()
        async with sessionmaker() as session:
            for i in range(4):
                session.add(CCFleetCache(
                    site_id=estate.sites["site-3"], agent_id=f"bulk-hidden-{i}",
                    agent_name=f"bulk-hidden-{i}", device_class="server",
                    service_tag="BULK",
                ))
            for i in range(4):
                session.add(CCFleetCache(
                    site_id=estate.sites["site-1"], agent_id=f"bulk-seen-{i}",
                    agent_name=f"bulk-seen-{i}", device_class="server",
                    service_tag="BULK",
                ))
            await session.commit()
        await _reconcile(sessionmaker)
        seen = sorted([await _resource(sessionmaker, LEGACY_DEVICE, f"bulk-seen-{i}")
                       for i in range(4)], key=lambda r: r.id)
        monkeypatch.setattr(resources_api, "MAX_IDENTITY_CANDIDATES", 3)
        async with await _as(app, sessionmaker, estate, "site_1") as client:
            block = (await _detail(client, seen[0].id))["identity_candidates"]
        # Exactly the limit is visible: the list is complete, and the four
        # hidden peers are not "more".
        assert [c["resource"]["id"] for c in block["candidates"]] == [r.id for r in seen[1:]]
        assert block["truncated"] is False
        async with await _as(app, sessionmaker, estate, "owner") as client:
            block = (await _detail(client, seen[0].id))["identity_candidates"]
        assert len(block["candidates"]) == 3 and block["truncated"] is True

    async def test_the_list_is_bounded_and_deterministic(self):
        app, sessionmaker, estate = await _tx3_stack()
        async with sessionmaker() as session:
            for i in range(MAX_IDENTITY_CANDIDATES + 5):
                session.add(CCFleetCache(
                    site_id=estate.sites["site-2"], agent_id=f"clone-{i:02d}",
                    agent_name=f"clone-{i:02d}", device_class="server",
                    service_tag="Default string",
                ))
            await session.commit()
        await _reconcile(sessionmaker)
        clones = sorted(
            [r for r in await _rows(sessionmaker, CCResource)
             if (r.legacy_ref or "").startswith("clone-")], key=lambda r: r.id)
        async with await _as(app, sessionmaker, estate, "owner") as client:
            first = (await _detail(client, clones[0].id))["identity_candidates"]
            again = (await _detail(client, clones[0].id))["identity_candidates"]
        assert first == again
        assert [c["resource"]["id"] for c in first["candidates"]] == [
            c.id for c in clones[1:MAX_IDENTITY_CANDIDATES + 1]]
        assert first["truncated"] is True

    async def test_names_and_retired_aliases_never_make_a_candidate(self):
        app, sessionmaker, estate = await _tx3_stack()
        await _forge_aliases(sessionmaker, estate)
        blank = await _resource(sessionmaker, LEGACY_DEVICE, "node-blank-1")
        switch = await _resource(sessionmaker, LEGACY_DEVICE, "node-switch-2")
        node1 = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-1")
        node2 = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-2")
        async with await _as(app, sessionmaker, estate, "owner") as client:
            for target in (blank, switch):
                ids = {c["resource"]["id"] for c in
                       (await _detail(client, target.id))["identity_candidates"]["candidates"]}
                assert node1.id not in ids, target.legacy_ref
            ids = {c["resource"]["id"] for c in
                   (await _detail(client, node1.id))["identity_candidates"]["candidates"]}
            assert not {blank.id, switch.id, node2.id} & ids


# ---------------------------------------------------------------------------
# 6. Tombstones
# ---------------------------------------------------------------------------


class TestTombstones:
    @pytest.mark.parametrize("persona", ["owner", "site_1"])
    async def test_a_tombstone_is_addressable_by_id_identifier_and_alias(self, persona):
        app, sessionmaker, estate = await _tx3_stack()
        gone = await _resource(sessionmaker, LEGACY_DEVICE, "node-gone-1")
        fresh = await _resource(sessionmaker, LEGACY_DEVICE, "node-reinstalled")
        assert gone.lifecycle_state == LIFECYCLE_RETIRED
        async with await _as(app, sessionmaker, estate, persona) as client:
            detail = await _detail(client, gone.id)
            assert detail["lifecycle_state"] == LIFECYCLE_RETIRED and detail["retired_at"]
            assert detail["provenance"]["last_observed_at"]
            assert [a["value"] for a in detail["aliases"]] == ["TAG-REINSTALL"]
            lookup = (await client.get(RESOURCES, params={
                "source_system": EXTERNAL_SOURCE_HARKENIQ,
                "namespace": NAMESPACE_HARKEN_NODE, "external_id": "node-gone-1",
                "lifecycle_state": "any",
            })).json()["resources"]
            assert [r["id"] for r in lookup] == [gone.id]
            # An alias is not an identity: it finds the tombstone AND the
            # node that replaced it.
            assert await _by_alias(client, ALIAS_SERVICE_TAG, "tag-reinstall") == {
                gone.id, fresh.id}
            assert await _by_alias(client, ALIAS_SERVICE_TAG, "TAG-REINSTALL",
                                   lifecycle="active") == {fresh.id}

    async def test_a_former_name_finds_a_tombstone(self):
        app, sessionmaker, estate = await _tx3_stack()
        node = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-2")
        async with sessionmaker() as session:
            await session.execute(update(CCFleetCache).where(
                CCFleetCache.agent_id == "node-site-2").values(agent_name="web-02"))
            await session.commit()
        await _reconcile(sessionmaker)
        async with sessionmaker() as session:
            await session.execute(delete(CCFleetCache).where(
                CCFleetCache.agent_id == "node-site-2"))
            await session.commit()
        await _reconcile(sessionmaker)
        assert (await _fresh(sessionmaker, node.id)).lifecycle_state == LIFECYCLE_RETIRED
        async with await _as(app, sessionmaker, estate, "owner") as client:
            assert await _by_alias(client, ALIAS_HISTORICAL_NAME, "node-site-2") == {node.id}

    async def test_the_agent_id_an_incident_carries_still_resolves(self):
        app, sessionmaker, estate = await _tx3_stack()
        gone = await _resource(sessionmaker, LEGACY_DEVICE, "node-gone-1")
        async with sessionmaker() as session:
            session.add(CCIncident(
                incident_id="inc-gone-1", tenant_id=TENANT, site_id=estate.sites["site-1"],
                device_agent_id="node-gone-1", title="before it was removed",
                status="resolved", kind="device",
            ))
            await session.commit()
        async with await _as(app, sessionmaker, estate, "owner") as client:
            listed = (await client.get("/api/incidents/", params={"status": "all"})).json()
            incident = next(i for i in listed["incidents"] if i["incident_id"] == "inc-gone-1")
            found = (await client.get(RESOURCES, params={
                "source_system": EXTERNAL_SOURCE_HARKENIQ,
                "namespace": NAMESPACE_HARKEN_NODE,
                "external_id": incident["device_agent_id"], "lifecycle_state": "any",
            })).json()["resources"]
        assert [r["id"] for r in found] == [gone.id]

    async def test_a_tombstone_confers_nothing(self):
        app, sessionmaker, estate = await _tx3_stack()
        gone = await _resource(sessionmaker, LEGACY_DEVICE, "node-gone-3")
        async with await _as(app, sessionmaker, estate, "gone_device") as client:
            assert (await client.get(f"{RESOURCES}{gone.id}")).status_code == 404
            assert await _visible(client) == set()
            assert await _by_alias(client, ALIAS_SERVICE_TAG, "TAG-GONE-3") == set()

    def test_no_production_path_deletes_a_catalogue_row(self):
        """A33.6: only a migration downgrade deletes. Every `delete(...)` of
        a catalogue model, every raw DELETE of a catalogue table and every
        `session.delete` in the catalogue's own modules is an offender."""
        models = {"CCResource", "CCResourceRelationship", "CCResourceExternalId",
                  "CCResourceAlias"}
        offenders = []
        for path in sorted(CC_SRC.rglob("*.py")):
            rel = path.relative_to(CC_SRC).as_posix()
            if "__pycache__" in rel or rel.startswith("db/migrations/"):
                continue
            source = path.read_text()
            if "DELETE FROM cc_resource" in source:
                offenders.append(f"{rel}: raw DELETE")
            for node in ast.walk(ast.parse(source)):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
                if name in ("delete", "sa_delete") and node.args:
                    target = getattr(node.args[0], "id", "")
                    if target in models:
                        offenders.append(f"{rel}: {name}({target})")
                if (name == "delete" and rel in ("resource_mirror.py", "api/resources.py")
                        and isinstance(node.func, ast.Attribute)):
                    offenders.append(f"{rel}: session.delete")
        repo_source = ast.get_source_segment(
            pathlib.Path(repos_module.__file__).read_text(),
            next(n for n in ast.parse(pathlib.Path(repos_module.__file__).read_text()).body
                 if isinstance(n, ast.ClassDef) and n.name == "ResourceRepo"),
        )
        if ".delete(" in repo_source or "sa_delete" in repo_source:
            offenders.append("db/repos.py: ResourceRepo deletes")
        assert offenders == [], offenders


# ---------------------------------------------------------------------------
# 7. The surface
# ---------------------------------------------------------------------------


class TestSurface:
    async def test_the_detail_carries_provenance_aliases_and_candidates(self):
        app, sessionmaker, estate = await _tx3_stack()
        node1 = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-1")
        node3 = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-3")
        async with await _as(app, sessionmaker, estate, "owner") as client:
            d = await _detail(client, node1.id)
        assert set(d["provenance"]) == {
            "source_system", "source_namespace", "first_observed_at", "last_observed_at"}
        assert d["provenance"]["source_namespace"] == FEED_FLEET_SNAPSHOT
        (binding,) = d["external_ids"]
        assert binding["external_id"] == "node-site-1"
        assert binding["first_observed_at"] and binding["last_observed_at"]
        (alias,) = d["aliases"]
        assert (alias["alias_type"], alias["value"], alias["lifecycle_state"]) == (
            ALIAS_SERVICE_TAG, "TAG-SHARED", LIFECYCLE_ACTIVE)
        assert alias["provenance"]["source_namespace"] == FEED_FLEET_SNAPSHOT
        assert d["identity_candidates"] == {
            "basis": sorted(IDENTITY_EVIDENCE_ALIAS_TYPES),
            "candidates": [{
                "resource": {"id": node3.id, "resource_kind": KIND_PHYSICAL_DEVICE,
                             "resource_type": TYPE_SERVER, "display_name": "node-site-3",
                             "lifecycle_state": LIFECYCLE_ACTIVE},
                "shared": [{"alias_type": ALIAS_SERVICE_TAG, "value": "tag-shared"}],
            }],
            "truncated": False,
        }

    async def test_the_alias_lookup(self):
        app, sessionmaker, estate = await _tx3_stack()
        node1 = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-1")
        node3 = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-3")
        async with await _as(app, sessionmaker, estate, "owner") as client:
            for params in ({"alias_type": "service_tag"}, {"alias": "TAG-SHARED"}):
                assert (await client.get(RESOURCES, params=params)).status_code == 422
            assert (await client.get(RESOURCES, params={
                "alias_type": "mac_address", "alias": "x"})).status_code == 422
            assert await _by_alias(client, ALIAS_SERVICE_TAG, "TAG-SHARED") == {
                node1.id, node3.id}
            # The same value under another type is another alias.
            assert await _by_alias(client, ALIAS_HOSTNAME, "TAG-SHARED") == set()
        async with await _as(app, sessionmaker, estate, "cluster_b1") as client:
            assert await _by_alias(client, ALIAS_SERVICE_TAG, "tag-shared") == {node3.id}
        async with await _as(app, sessionmaker, estate, "site_1") as client:
            assert await _by_alias(client, ALIAS_SERVICE_TAG, "TAG-SHARED") == {node1.id}
            assert await _by_alias(client, ALIAS_SERVICE_TAG, "TAG-GONE-3") == set()

    async def test_every_edge_summary_carries_its_provenance(self):
        app, sessionmaker, estate = await _tx3_stack()
        node1 = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-1")
        node2 = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-2")
        site1 = await _resource(sessionmaker, LEGACY_SITE, estate.sites["site-1"])
        async with await _as(app, sessionmaker, estate, "owner") as client:
            rels = (await client.get(f"{RESOURCES}{node1.id}/relationships")).json()
            parents = (await client.get(f"{RESOURCES}{node1.id}/parents")).json()
            kids = (await client.get(f"{RESOURCES}{site1.id}/children")).json()
            tree = (await client.get(f"{RESOURCES}{site1.id}/topology")).json()
            under = (await client.get(f"{RESOURCES}{node2.id}/relationships")).json()
        summaries = ([r for r in rels["relationships"]]
                     + [p["relationship"] for p in parents["parents"]]
                     + [c["relationship"] for c in kids["children"]]
                     + [n["relationship"] for n in tree["nodes"]])
        assert summaries
        for s in summaries:
            assert s["provenance"]["source_system"] == EXTERNAL_SOURCE_HARKENIQ
            assert s["provenance"]["last_observed_at"]
        assert rels["relationships"][0]["provenance"]["source_namespace"] == FEED_SITE_TOPOLOGY
        assert under["relationships"][0]["provenance"]["source_namespace"] == FEED_FLEET_SNAPSHOT

    async def test_hidden_and_nonexistent_still_read_alike(self):
        app, sessionmaker, estate = await _tx3_stack()
        node3 = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-3")
        async with await _as(app, sessionmaker, estate, "site_1") as client:
            hidden = await client.get(f"{RESOURCES}{node3.id}")
            missing = await client.get(f"{RESOURCES}{'0' * 32}")
        assert hidden.status_code == missing.status_code == 404
        assert hidden.json() == missing.json()

    async def test_a_machine_principal_is_refused_with_aliases_present(self):
        app, sessionmaker, estate = await _tx3_stack()
        node1 = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-1")

        async def _machine():
            return UserContext(
                user_id="agent-tx3", email="", tenant_id=TENANT, role="",
                permissions=["fleet.view", "incident.view", "proposal.submit"],
                species=SPECIES_AGENT,
                machine_jobs=frozenset({"self", "attention", "incidents",
                                        "proposals", "autonomy"}),
            )

        app.dependency_overrides[get_current_user] = _machine
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            for path, params in ((f"{RESOURCES}{node1.id}", None),
                                 (RESOURCES, {"alias_type": "service_tag",
                                              "alias": "TAG-SHARED"})):
                res = await client.get(path, params=params)
                assert res.status_code == 403, (path, res.status_code)
                assert "External Agent API plane" in res.text

    def test_no_route_was_added(self):
        assert sum(1 for _m, p in ROUTE_CONTRACT if p.startswith(RESOURCES)) == 6
        assert len(ROUTE_CONTRACT) == 105 and len(MACHINE_SURFACE) == 14
        assert not any(p.startswith(RESOURCES) for _m, p in MACHINE_SURFACE)


# ---------------------------------------------------------------------------
# 8. The A33 invariants
# ---------------------------------------------------------------------------


async def _grant_all(app, sessionmaker, estate):
    for persona in INVARIANT_PERSONAS:
        await (await _as(app, sessionmaker, estate, persona)).aclose()


async def _canonical(app):
    """Every canonical read, for every persona, byte for byte."""
    out = {}
    for persona in INVARIANT_PERSONAS:
        async with _client(app, TOKEN_ROLE[persona], f"kc-tx3-{persona}") as client:
            for path in CANONICAL_READS:
                res = await client.get(path)
                out[(persona, path)] = (res.status_code, res.text)
    return out


async def _visible_sets(app):
    out = {}
    for persona in INVARIANT_PERSONAS:
        async with _client(app, TOKEN_ROLE[persona], f"kc-tx3-{persona}") as client:
            out[persona] = await _visible(client)
    return out


class TestInvariants:
    async def test_identity_aliases_and_provenance_grant_nothing(self):
        """The same estate with TX-3's facts present, and with every alias
        gone and every provenance column empty: every canonical read for
        every persona -- the resolved scope included -- is byte-identical,
        and so is what each persona sees in the catalogue."""
        app, sessionmaker, estate = await _tx3_stack()
        await _grant_all(app, sessionmaker, estate)
        present = await _canonical(app)
        seen = await _visible_sets(app)
        nulls = dict(source_system=None, source_namespace=None,
                     first_observed_at=None, last_observed_at=None)
        async with sessionmaker() as session:
            assert (await session.execute(select(CCResourceAlias))).scalars().first()
            await session.execute(delete(CCResourceAlias))
            await session.execute(update(CCResource).values(**nulls))
            await session.execute(update(CCResourceRelationship).values(**nulls))
            await session.execute(update(CCResourceExternalId).values(
                first_observed_at=None, last_observed_at=None))
            await session.commit()
        assert await _canonical(app) == present
        assert await _visible_sets(app) == seen
        await _reconcile(sessionmaker)
        assert await _canonical(app) == present

    async def test_forged_identity_and_aliases_move_nothing_and_show_nothing_hidden(self):
        """Lies in every direction: a hidden device carrying a visible
        site's id, a visible device's agent id and a visible rack's name as
        aliases; identifiers from systems nobody integrates naming hidden
        devices with visible ids; provenance claiming another system."""
        app, sessionmaker, estate = await _tx3_stack()
        await _grant_all(app, sessionmaker, estate)
        canonical = await _canonical(app)
        visible = await _visible_sets(app)
        region = await _forge_aliases(sessionmaker, estate)
        node3 = await _resource(sessionmaker, LEGACY_DEVICE, "node-site-3")
        site3 = await _resource(sessionmaker, LEGACY_SITE, estate.sites["site-3"])
        rk1a = await _rack(sessionmaker, "rk-1a")
        async with sessionmaker() as session:
            for alias_type, value in ((ALIAS_HOSTNAME, estate.sites["site-1"]),
                                      (ALIAS_OPERATOR_LABEL, "node-site-1"),
                                      (ALIAS_SERVICE_TAG, rk1a.display_name)):
                session.add(CCResourceAlias(
                    tenant_id=TENANT, resource_id=node3.id, alias_type=alias_type,
                    value=value, normalized_value=normalize_alias(value),
                    source_system="lie", source_namespace="raw", source="lie",
                ))
            for target, value in ((node3, estate.sites["site-1"]), (site3, "node-site-1")):
                session.add(CCResourceExternalId(
                    tenant_id=TENANT, resource_id=target.id, source_system="servicenow",
                    namespace="sys_id", external_id=value, source="lie",
                ))
            await session.execute(update(CCResource).values(
                source_system="servicenow", source_namespace="cmdb_ci"))
            await session.commit()
        assert await _canonical(app) == canonical
        # The forged region is a new, tenant-owned RESOURCE (A32.6): the
        # tenant-wide reader sees it and nobody else does. No lie moved
        # anything else for anyone.
        assert await _visible_sets(app) == {
            **visible, "owner": visible["owner"] | {region.id}}
        async with _client(app, TOKEN_ROLE["site_1"], "kc-tx3-site_1") as client:
            assert await _by_alias(client, ALIAS_HOSTNAME, estate.sites["site-1"]) == set()
            assert await _by_alias(client, ALIAS_OPERATOR_LABEL, "node-site-1") == set()
            body = (await client.get(RESOURCES, params={
                "source_system": "servicenow", "namespace": "sys_id",
                "external_id": estate.sites["site-1"], "lifecycle_state": "any",
            })).json()
            assert body["resources"] == [] and body["total"] == 0
            assert (await client.get(f"{RESOURCES}{node3.id}")).status_code == 404

    async def test_a_reserved_or_foreign_type_written_raw_moves_nothing(self):
        app, sessionmaker, estate = await _tx3_stack()
        await _grant_all(app, sessionmaker, estate)
        canonical = await _canonical(app)
        visible = await _visible_sets(app)
        async with sessionmaker() as session:
            await session.execute(update(CCResource).where(
                CCResource.legacy_kind == LEGACY_DEVICE).values(
                resource_type="harkeniq:compute.gpu_server"))
            await session.execute(update(CCResource).where(
                CCResource.legacy_kind != LEGACY_DEVICE).values(
                resource_type="vmware:compute.cluster"))
            await session.commit()
        assert await _canonical(app) == canonical
        assert await _visible_sets(app) == visible

    async def test_device_class_is_untouched_by_the_mirror(self):
        app, sessionmaker, estate = await _tx3_stack(mirror=False)

        async def _classes():
            return sorted((r.site_id, r.agent_id, r.device_class, r.service_tag)
                          for r in await _rows(sessionmaker, CCFleetCache))

        before = await _classes()
        await _reconcile(sessionmaker)
        await _reconcile(sessionmaker)
        assert await _classes() == before
        assert DEVICE_CLASSES == frozenset({"server", "switch"})

    async def test_capabilities_are_not_inferred_from_aliases_or_types(self):
        app, sessionmaker, estate = await _tx3_stack(mirror=False)
        await _grant_row(sessionmaker, "kc-tx3-cap", SCOPE_TENANT, role="tenant_owner")
        async with _client(app, "tenant_owner", "kc-tx3-cap") as client:
            before = (await client.get("/api/capabilities/")).text
            await _reconcile(sessionmaker)
            await _forge_aliases(sessionmaker, estate)
            async with sessionmaker() as session:
                await session.execute(update(CCResource).values(
                    resource_type="harkeniq:network.ethernet_switch"))
                await session.commit()
            after = (await client.get("/api/capabilities/")).text
        assert after == before



# ---------------------------------------------------------------------------
# 9. Structure
# ---------------------------------------------------------------------------

#: TX-3's write primitives: the mirror is their one caller.
TX3_WRITERS = {"create_alias", "retire_alias", "revive_alias", "record_observation"}


class TestStructure:
    def test_visibility_reads_nothing_tx3_stores(self):
        source = ast.get_source_segment(
            pathlib.Path(repos_module.__file__).read_text(),
            next(n for n in ast.parse(pathlib.Path(repos_module.__file__).read_text()).body
                 if isinstance(n, ast.FunctionDef) and n.name == "scope_resources"),
        )
        body = source.split('"""')[-1]
        for forbidden in ("CCResourceAlias", "CCResourceExternalId", "alias",
                          "normalized_value", "source_system", "source_namespace",
                          "observed", "IDENTITY_EVIDENCE", "candidate"):
            assert forbidden not in body, forbidden
        assert scope_resources is repos_module.scope_resources

    def test_only_the_mirror_writes_aliases_and_observations(self):
        callers = set()
        for path in sorted(CC_SRC.rglob("*.py")):
            rel = path.relative_to(CC_SRC).as_posix()
            if "__pycache__" in rel or rel == "db/repos.py":
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in TX3_WRITERS):
                    callers.add((rel, node.func.attr))
        assert {f for f, _a in callers} == {"resource_mirror.py"}, callers
        # Every one of them has that caller: nothing here is declared and unused.
        assert {a for _f, a in callers} == TX3_WRITERS

    def test_the_permissions_and_the_ceiling_are_unchanged(self):
        from harkeniq_cc.machine_identity import MACHINE_PRINCIPAL_CEILING
        from harkeniq_console.permissions import PERMISSIONS

        assert len(PERMISSIONS) == 25
        assert set(MACHINE_PRINCIPAL_CEILING) == {
            "fleet.view", "incident.view", "proposal.submit",
        }
        assert not {p for p in PERMISSIONS
                    if any(w in p for w in ("resource", "alias", "identity", "reconcil"))}
