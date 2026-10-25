"""TX-2 (A32) on a REAL PostgreSQL: relationships, topology and racks.

The sqlite proofs live in tests/unit/cc/test_tx2_resource_topology.py. This
file asks what sqlite cannot answer honestly:

* **The keys are real** -- migration 0031 adds the composite (resource,
  tenant) foreign keys with ALTER, which sqlite cannot do; PostgreSQL must
  refuse a cross-tenant edge and a dangling one at the database.
* **One ACTIVE primary parent** -- the partial index's predicate now reads
  `lifecycle_state`; PostgreSQL must admit a retired former placement
  beside the current one and refuse a second current one.
* **Racks on the real engine** -- every persona's racks equal the racks at
  its authoritative sites, and the topology reads agree, through the app.
* **Two replicas** -- two engines reconciling racks at once serialize on
  the advisory lock: one resource per rack, one active primary each.
* **The upgrade** -- 0030 -> 0031 on a database holding resources and edges:
  every edge `active`, the keys and the index present and valid, nothing
  backfilled; the downgrade with a retired edge present drops only it.

Each test owns a tenant on the shared, migrated database. Gated on
``HARKEN_TEST_CC_PG_DSN`` (an alembic-managed database).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.app import create_app
from harkeniq_cc.auth import ROLE_PERMISSIONS, UserContext, configure_auth
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import make_engine, make_sessionmaker
from harkeniq_cc.db.models import CCFleetCache, CCResource, CCResourceRelationship, CCSite
from harkeniq_cc.db.repos import ScopeGrantRepo, SiteTopologyRepo, TenantSettingsRepo
from harkeniq_cc.resource_catalogue import (
    KIND_PHYSICAL_CONTAINER,
    KIND_PHYSICAL_DEVICE,
    LEGACY_RACK,
    LIFECYCLE_RETIRED,
    REL_CONTAINS,
    TYPE_RACK,
    TYPE_SITE,
)
from harkeniq_cc.resource_mirror import reconcile
from harkeniq_cc.runtime import AppState
from harkeniq_cc.scope import ENFORCEMENT_STRICT, SCOPE_DEVICE, SCOPE_SITE, SCOPE_TENANT

CC_DSN = os.environ.get("HARKEN_TEST_CC_PG_DSN", "")
REPO = Path(__file__).resolve().parents[2]

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not CC_DSN, reason="HARKEN_TEST_CC_PG_DSN not set"),
]


def _alembic(*args: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("HARKEN_")}
    env["HARKEN_CC_DSN"] = CC_DSN
    env["HARKEN_CC_TENANT_ID"] = "tenant-demo"
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO / "services" / "central_command", env=env,
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


async def _estate(maker, tenant: str) -> dict:
    """Two sites. A reports rack-1 (holding srv-a) and rack-2 (empty); B
    reports rack-b (holding srv-b); sw-a at A is unplaced."""
    ids = {"tag": tenant[-8:]}
    tag = ids["tag"]
    async with maker() as session:
        for key in ("a", "b"):
            site = CCSite(tenant_id=tenant, site_name=f"tx2-{key}-{tenant}",
                          sm_endpoint="sm:50051", sm_token="tok")
            session.add(site)
            await session.flush()
            ids[key] = site.id
        for site, agent, cls in (("a", f"srv-a-{tag}", "server"),
                                 ("a", f"sw-a-{tag}", "switch"),
                                 ("b", f"srv-b-{tag}", "server")):
            session.add(CCFleetCache(
                site_id=ids[site], agent_id=agent, agent_name=agent,
                vendor="Dell", model="R750", health="ok", device_class=cls,
            ))
        await session.flush()
        repo = SiteTopologyRepo(session)
        await repo.upsert(tenant, ids["a"], {"reported": True, "racks": [
            {"rack_id": f"rk1-{tag}", "name": "A01", "device_agent_ids": [f"srv-a-{tag}"]},
            {"rack_id": f"rk2-{tag}", "name": "A02", "device_agent_ids": []},
        ]})
        await repo.upsert(tenant, ids["b"], {"reported": True, "racks": [
            {"rack_id": f"rkb-{tag}", "name": "B01", "device_agent_ids": [f"srv-b-{tag}"]},
        ]})
        await session.commit()
        await TenantSettingsRepo(session).set_enforcement(tenant, ENFORCEMENT_STRICT, "tx2-pg")
        await session.commit()
    return ids


async def _cleanup(maker, tenant: str) -> None:
    async with maker() as session:
        for stmt in (
            "DELETE FROM cc_resource_external_ids WHERE tenant_id = :t",
            "DELETE FROM cc_resource_relationships WHERE tenant_id = :t",
            "DELETE FROM cc_resources WHERE tenant_id = :t",
            "DELETE FROM cc_site_topology WHERE tenant_id = :t",
            "DELETE FROM cc_fleet_cache WHERE site_id IN "
            "(SELECT id FROM cc_sites WHERE tenant_id = :t)",
            "DELETE FROM cc_scope_grants WHERE tenant_id = :t",
            "DELETE FROM cc_tenant_settings WHERE tenant_id = :t",
            "DELETE FROM cc_sites WHERE tenant_id = :t",
        ):
            try:
                await session.execute(sa.text(stmt), {"t": tenant})
                await session.commit()
            except Exception:  # noqa: BLE001 - best effort on a shared db
                await session.rollback()


def _client(app, tenant, role, user_id):
    async def _fake():
        return UserContext(
            user_id=user_id, email=f"{user_id}@example.com", tenant_id=tenant,
            role=role, permissions=list(ROLE_PERMISSIONS[role]),
        )

    app.dependency_overrides[get_current_user] = _fake
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _resources(maker, tenant, kind=None):
    async with maker() as session:
        rows = (await session.execute(sa.select(CCResource).where(
            CCResource.tenant_id == tenant))).scalars().all()
    return [r for r in rows if kind is None or r.legacy_kind == kind]


def _row(tenant, kind, rtype, name):
    return CCResource(tenant_id=tenant, resource_kind=kind, resource_type=rtype,
                      display_name=name, source="pg", lifecycle_state="active")


async def test_the_tenant_keys_and_the_active_primary_index_are_real():
    tenant = f"tx2pg-{uuid.uuid4().hex[:8]}"
    other = f"tx2pg-{uuid.uuid4().hex[:8]}"
    engine = make_engine(CC_DSN)
    maker = make_sessionmaker(engine)
    try:
        async with maker() as session:
            p1, p2, p3 = (_row(tenant, KIND_PHYSICAL_CONTAINER, TYPE_SITE, n)
                          for n in ("p1", "p2", "p3"))
            child = _row(tenant, KIND_PHYSICAL_DEVICE, "harkeniq:compute.server", "c")
            foreign = _row(other, KIND_PHYSICAL_DEVICE, "harkeniq:compute.server", "f")
            session.add_all([p1, p2, p3, child, foreign])
            await session.flush()
            ids = {n: r.id for n, r in (("p1", p1), ("p2", p2), ("p3", p3),
                                        ("child", child), ("foreign", foreign))}
            # A former placement beside the current one: admitted.
            session.add_all([
                CCResourceRelationship(
                    tenant_id=tenant, from_resource_id=ids["p1"], to_resource_id=ids["child"],
                    relationship_type=REL_CONTAINS, is_primary=True, source="pg",
                    lifecycle_state=LIFECYCLE_RETIRED),
                CCResourceRelationship(
                    tenant_id=tenant, from_resource_id=ids["p2"], to_resource_id=ids["child"],
                    relationship_type=REL_CONTAINS, is_primary=True, source="pg"),
            ])
            await session.commit()

        def _edge(frm, to, *, tenant_id=tenant, primary=False):
            return CCResourceRelationship(
                tenant_id=tenant_id, from_resource_id=frm, to_resource_id=to,
                relationship_type=REL_CONTAINS, is_primary=primary, source="pg")

        refused = [
            # a second CURRENT primary parent
            _edge(ids["p3"], ids["child"], primary=True),
            # cross-tenant, both directions, and claiming either tenant
            _edge(ids["p3"], ids["foreign"]),
            _edge(ids["foreign"], ids["p3"]),
            _edge(ids["p3"], ids["foreign"], tenant_id=other),
            # dangling
            _edge(ids["p3"], "0" * 32),
        ]
        for edge in refused:
            async with maker() as session:
                session.add(edge)
                with pytest.raises(IntegrityError):
                    await session.commit()
        async with engine.connect() as conn:
            definition = (await conn.execute(sa.text(
                "SELECT indexdef FROM pg_indexes WHERE indexname = "
                "'uq_resource_active_primary_parent'"))).scalar_one()
            keys = set((await conn.execute(sa.text(
                "SELECT conname FROM pg_constraint WHERE conrelid = "
                "'cc_resource_relationships'::regclass AND contype = 'f' AND convalidated"
            ))).scalars())
        assert "UNIQUE" in definition and "lifecycle_state" in definition, definition
        assert {"fk_resource_relationship_from_tenant",
                "fk_resource_relationship_to_tenant"} <= keys
    finally:
        await _cleanup(maker, tenant)
        await _cleanup(maker, other)
        await engine.dispose()


async def test_racks_and_the_topology_reads_on_postgres():
    tenant = f"tx2pg-{uuid.uuid4().hex[:8]}"
    engine = make_engine(CC_DSN)
    maker = make_sessionmaker(engine)
    try:
        ids = await _estate(maker, tenant)
        tag = ids["tag"]
        async with maker() as session:
            await reconcile(session, tenant)
            await session.commit()
        configure_auth("", "", "", insecure=True)
        state = AppState(config=CCConfig(tenant_id=tenant, insecure=True),
                         engine=engine, sessionmaker=maker)
        app = create_app(state)
        personas = {
            "owner": ("tenant_owner", SCOPE_TENANT, ""),
            "site_a": ("operator", SCOPE_SITE, ids["a"]),
            "device_a": ("viewer", SCOPE_DEVICE, f"srv-a-{tag}"),
        }
        racks = {r.legacy_ref: r for r in await _resources(maker, tenant, LEGACY_RACK)}
        seen = {}
        for name, (role, scope_type, ref) in personas.items():
            user = f"kc-{name}-{tag}"
            async with maker() as session:
                await ScopeGrantRepo(session).grant(
                    tenant_id=tenant, principal_type="user", principal_ref=user,
                    scope_type=scope_type, scope_ref=ref, permission_subset=None,
                    role=role, granted_by="tx2-pg")
                await session.commit()
            async with _client(app, tenant, role, user) as client:
                rows = (await client.get("/api/resources/", params={
                    "page_size": 200, "lifecycle_state": "any"})).json()["resources"]
                seen[name] = {r["legacy"]["ref"] for r in rows
                              if r["resource_type"] == TYPE_RACK}
                srv = next((r for r in rows if r["legacy"]["ref"] == f"srv-a-{tag}"), None)
                if srv is not None:
                    chain = (await client.get(
                        f"/api/resources/{srv['id']}/parents")).json()["parents"]
                    seen[name + ":chain"] = [p["resource"]["resource_type"] for p in chain]
                if name == "site_a":
                    rack = racks[f"rk1-{tag}"]
                    kids = (await client.get(f"/api/resources/{rack.id}/children")).json()
                    assert [c["resource"]["display_name"] for c in kids["children"]] == [
                        f"srv-a-{tag}"]
                    hidden = racks[f"rkb-{tag}"]
                    for tail in ("", "/parents", "/children", "/topology"):
                        assert (await client.get(
                            f"/api/resources/{hidden.id}{tail}")).status_code == 404
        assert seen["owner"] == {f"rk1-{tag}", f"rk2-{tag}", f"rkb-{tag}"}
        assert seen["site_a"] == {f"rk1-{tag}", f"rk2-{tag}"}
        assert seen["device_a"] == set()
        assert seen["owner:chain"] == seen["site_a:chain"] == [TYPE_RACK, TYPE_SITE]
        assert seen["device_a:chain"] == []
    finally:
        await _cleanup(maker, tenant)
        await engine.dispose()


async def test_two_replicas_reconcile_racks_at_once():
    tenant = f"tx2pg-{uuid.uuid4().hex[:8]}"
    engines = [make_engine(CC_DSN), make_engine(CC_DSN)]
    makers = [make_sessionmaker(e) for e in engines]
    try:
        ids = await _estate(makers[0], tenant)
        tag = ids["tag"]

        async def _replica(maker):
            async with maker() as session:
                report = await reconcile(session, tenant)
                await session.commit()
                return report

        for flip in range(3):
            # Move srv-a between the two racks every round, so concurrent
            # passes retire and revive edges while they race.
            members = ([f"srv-a-{tag}"], []) if flip % 2 == 0 else ([], [f"srv-a-{tag}"])
            async with makers[0]() as session:
                await SiteTopologyRepo(session).upsert(tenant, ids["a"], {
                    "reported": True, "racks": [
                        {"rack_id": f"rk1-{tag}", "name": "A01", "device_agent_ids": members[0]},
                        {"rack_id": f"rk2-{tag}", "name": "A02", "device_agent_ids": members[1]},
                    ]})
                await session.commit()
            reports = await asyncio.gather(*(_replica(m) for m in makers * 3))
            assert all(r.conflicts == 0 and r.refused == 0 for r in reports)
        async with makers[0]() as session:
            counts = (await session.execute(sa.text(
                "SELECT legacy_kind, legacy_ref, count(*) FROM cc_resources "
                "WHERE tenant_id = :t GROUP BY legacy_kind, legacy_ref"), {"t": tenant})).all()
            active = (await session.execute(sa.text(
                "SELECT to_resource_id, count(*) FROM cc_resource_relationships "
                "WHERE tenant_id = :t AND is_primary AND lifecycle_state = 'active' "
                "GROUP BY to_resource_id"), {"t": tenant})).all()
            rows = (await session.execute(sa.text(
                "SELECT count(*) FROM cc_resource_relationships WHERE tenant_id = :t"),
                {"t": tenant})).scalar_one()
        assert counts and all(n == 1 for _k, _r, n in counts), counts
        assert sorted(k for k, _r, _n in counts).count(LEGACY_RACK) == 3
        assert all(n == 1 for _c, n in active)
        # 3 racks + 3 devices placed, plus ONE former placement for srv-a:
        # moved back and forth, it never gained a third row.
        assert rows == 3 + 3 + 1
    finally:
        await _cleanup(makers[0], tenant)
        for engine in engines:
            await engine.dispose()


async def test_cc_0030_to_0031_with_rows_and_back():
    tenant = f"tx2pg-{uuid.uuid4().hex[:8]}"
    engine = make_engine(CC_DSN)
    maker = make_sessionmaker(engine)
    try:
        ids = await _estate(maker, tenant)
        async with maker() as session:
            await reconcile(session, tenant)
            await session.commit()
        _alembic("downgrade", "0030")
        async with engine.connect() as conn:
            gone = (await conn.execute(sa.text(
                "SELECT count(*) FROM information_schema.columns WHERE table_name = "
                "'cc_resource_relationships' AND column_name = 'lifecycle_state'"))).scalar()
            before = (await conn.execute(sa.text(
                "SELECT id, from_resource_id, to_resource_id, is_primary FROM "
                "cc_resource_relationships WHERE tenant_id = :t ORDER BY id"),
                {"t": tenant})).all()
        assert gone == 0 and before

        _alembic("upgrade", "head")
        async with engine.connect() as conn:
            version = (await conn.execute(sa.text(
                "SELECT version_num FROM alembic_version"))).scalar_one()
            after = (await conn.execute(sa.text(
                "SELECT id, from_resource_id, to_resource_id, is_primary FROM "
                "cc_resource_relationships WHERE tenant_id = :t ORDER BY id"),
                {"t": tenant})).all()
            states = set((await conn.execute(sa.text(
                "SELECT lifecycle_state FROM cc_resource_relationships WHERE tenant_id = :t"),
                {"t": tenant})).scalars())
            valid = (await conn.execute(sa.text(
                "SELECT i.indisvalid AND i.indisunique FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = 'uq_resource_active_primary_parent'"))).scalar_one()
            old = (await conn.execute(sa.text(
                "SELECT count(*) FROM pg_class WHERE relname = 'uq_resource_primary_parent'"
            ))).scalar()
            topology = (await conn.execute(sa.text(
                "SELECT count(*) FROM cc_site_topology WHERE tenant_id = :t"),
                {"t": tenant})).scalar()
            kind = (await conn.execute(sa.text(
                "SELECT data_type FROM information_schema.columns WHERE table_name = "
                "'cc_resource_relationships' AND column_name = 'retired_at'"))).scalar()
        assert version >= "0031", version
        assert after == before, "an additive migration moved an edge"
        assert states == {"active"} and valid and old == 0
        assert topology == 0, "the downgrade dropped the report; nothing re-created it"
        assert kind == "timestamp with time zone"
        _alembic("upgrade", "head")                    # idempotent

        # A move leaves a retired edge; the downgrade removes only that.
        tag = ids["tag"]
        async with maker() as session:
            await SiteTopologyRepo(session).upsert(tenant, ids["a"], {"reported": True, "racks": [
                {"rack_id": f"rk1-{tag}", "name": "A01", "device_agent_ids": []},
                {"rack_id": f"rk2-{tag}", "name": "A02", "device_agent_ids": [f"srv-a-{tag}"]},
            ]})
            await session.commit()
            await reconcile(session, tenant)
            await session.commit()
        async with engine.connect() as conn:
            retired = (await conn.execute(sa.text(
                "SELECT count(*) FROM cc_resource_relationships WHERE tenant_id = :t "
                "AND lifecycle_state = 'retired'"), {"t": tenant})).scalar()
            total = (await conn.execute(sa.text(
                "SELECT count(*) FROM cc_resource_relationships WHERE tenant_id = :t"),
                {"t": tenant})).scalar()
        assert retired == 1
        _alembic("downgrade", "0030")
        async with engine.connect() as conn:
            left = (await conn.execute(sa.text(
                "SELECT count(*) FROM cc_resource_relationships WHERE tenant_id = :t"),
                {"t": tenant})).scalar()
            restored = (await conn.execute(sa.text(
                "SELECT count(*) FROM pg_class WHERE relname = 'uq_resource_primary_parent'"
            ))).scalar()
        assert left == total - 1 and restored == 1
    finally:
        _alembic("upgrade", "head")
        await _cleanup(maker, tenant)
        await engine.dispose()
