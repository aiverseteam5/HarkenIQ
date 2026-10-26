"""TX-1 (A31) on a REAL PostgreSQL: the Resource Catalogue foundation.

The sqlite proofs live in tests/unit/cc/test_tx1_resource_catalogue.py.
This file asks what sqlite cannot answer honestly:

* **Parity on the real engine** -- the visibility predicate is a pair of
  correlated `EXISTS` and an `IN (...)` PostgreSQL actually plans; every
  persona's catalogue must equal its fleet and authoritative sites, and a
  lapsed grant must lapse by `timestamptz` arithmetic.
* **The partial unique index** -- "at most one primary containment parent"
  is a `WHERE is_primary` index; PostgreSQL must refuse the second one.
* **Two replicas** -- two engines (two pools) reconciling the same tenant at
  once must serialize on the advisory lock and leave exactly one resource
  per canonical object.
* **The upgrade** -- 0029 -> 0030 on a database holding sites and fleet rows
  creates the three tables, backfills NOTHING, is idempotent, and
  downgrades cleanly.

Each test owns a tenant on the shared, migrated database. Gated on
``HARKEN_TEST_CC_PG_DSN`` (an alembic-managed database).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
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
from harkeniq_cc.db.models import (
    CCFleetCache,
    CCResource,
    CCResourceRelationship,
    CCSite,
)
from harkeniq_cc.db.repos import ScopeGrantRepo, TenantSettingsRepo
from harkeniq_cc.resource_catalogue import (
    KIND_PHYSICAL_CONTAINER,
    KIND_PHYSICAL_DEVICE,
    LEGACY_DEVICE,
    LEGACY_SITE,
    REL_CONTAINS,
    TYPE_SITE,
)
from harkeniq_cc.resource_mirror import reconcile
from harkeniq_cc.runtime import AppState
from harkeniq_cc.scope import (
    ENFORCEMENT_STRICT,
    SCOPE_DEVICE,
    SCOPE_DEVICE_CLASS,
    SCOPE_SITE,
    SCOPE_TENANT,
)

CC_DSN = os.environ.get("HARKEN_TEST_CC_PG_DSN", "")
REPO = Path(__file__).resolve().parents[2]
TABLES = ("cc_resource_external_ids", "cc_resource_relationships", "cc_resources")

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
    """Two sites; a server and a switch at A, a server at B, one node at
    both (a move in flight)."""
    ids = {}
    async with maker() as session:
        for key in ("a", "b"):
            site = CCSite(tenant_id=tenant, site_name=f"tx1-{key}-{tenant}",
                          sm_endpoint="sm:50051", sm_token="tok")
            session.add(site)
            await session.flush()
            ids[key] = site.id
        tag = tenant[-8:]
        for site, agent, cls in (
            ("a", f"srv-a-{tag}", "server"), ("a", f"sw-a-{tag}", "switch"),
            ("b", f"srv-b-{tag}", "server"),
            ("a", f"dual-{tag}", "server"), ("b", f"dual-{tag}", "server"),
        ):
            session.add(CCFleetCache(
                site_id=ids[site], agent_id=agent, agent_name=agent,
                vendor="Dell", model="R750", health="ok", device_class=cls,
            ))
        await session.commit()
        await TenantSettingsRepo(session).set_enforcement(
            tenant, ENFORCEMENT_STRICT, "tx1-pg"
        )
        await session.commit()
    ids["tag"] = tenant[-8:]
    return ids


async def _cleanup(maker, tenant: str) -> None:
    async with maker() as session:
        for stmt in (
            # TX-3 (A33.3): aliases name a resource, so they go first.
            "DELETE FROM cc_resource_aliases WHERE tenant_id = :t",
            "DELETE FROM cc_resource_external_ids WHERE tenant_id = :t",
            "DELETE FROM cc_resource_relationships WHERE tenant_id = :t",
            "DELETE FROM cc_resources WHERE tenant_id = :t",
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


async def _grant(maker, tenant, principal, scope_type, ref="", *, role, subset=None,
                 expired=False):
    async with maker() as session:
        row = await ScopeGrantRepo(session).grant(
            tenant_id=tenant, principal_type="user", principal_ref=principal,
            scope_type=scope_type, scope_ref=ref, permission_subset=subset,
            role=role, granted_by="tx1-pg",
        )
        if expired:
            row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        await session.commit()


def _client(app, tenant, role, user_id):
    async def _fake():
        return UserContext(
            user_id=user_id, email=f"{user_id}@example.com", tenant_id=tenant,
            role=role, permissions=list(ROLE_PERMISSIONS[role]),
        )

    app.dependency_overrides[get_current_user] = _fake
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://test")


async def _views(client):
    fleet = {d["agent_id"] for d in
             (await client.get("/api/fleet/", params={"page_size": 200})).json()["devices"]}
    sites = {s["id"] for s in (await client.get("/api/sites/")).json()["sites"]
             if not s.get("contextual")}
    rows = (await client.get("/api/resources/", params={
        "page_size": 200, "lifecycle_state": "active"})).json()["resources"]
    return (
        fleet, sites,
        {r["legacy"]["ref"] for r in rows if r["resource_kind"] == KIND_PHYSICAL_DEVICE},
        {r["legacy"]["ref"] for r in rows if r["resource_type"] == TYPE_SITE},
    )


async def test_parity_on_postgres():
    tenant = f"tx1pg-{uuid.uuid4().hex[:8]}"
    engine = make_engine(CC_DSN)
    maker = make_sessionmaker(engine)
    try:
        ids = await _estate(maker, tenant)
        tag = ids["tag"]
        async with maker() as session:
            await reconcile(session, tenant)
            await session.commit()
        personas = {
            "owner": ("tenant_owner", [(SCOPE_TENANT, "", None, False)]),
            "site_a": ("operator", [(SCOPE_SITE, ids["a"], None, False)]),
            "device_b": ("viewer", [(SCOPE_DEVICE, f"srv-b-{tag}", None, False)]),
            "class_switch": ("viewer", [(SCOPE_DEVICE_CLASS, "switch", None, False)]),
            "narrowed_a": ("auditor", [(SCOPE_SITE, ids["a"], ["incident.view"], False)]),
            "expired_b": ("site_admin", [(SCOPE_SITE, ids["b"], None, True)]),
        }
        configure_auth("", "", "", insecure=True)
        state = AppState(config=CCConfig(tenant_id=tenant, insecure=True),
                         engine=engine, sessionmaker=maker)
        app = create_app(state)
        seen = {}
        for name, (role, grants) in personas.items():
            user = f"kc-{name}-{tag}"
            for scope_type, ref, subset, expired in grants:
                await _grant(maker, tenant, user, scope_type, ref, role=role,
                             subset=subset, expired=expired)
            async with _client(app, tenant, role, user) as client:
                fleet, sites, devices, site_refs = await _views(client)
            assert devices == fleet, name
            assert site_refs == sites, name
            seen[name] = devices
        assert seen["owner"] == {f"srv-a-{tag}", f"sw-a-{tag}", f"srv-b-{tag}", f"dual-{tag}"}
        assert seen["site_a"] == {f"srv-a-{tag}", f"sw-a-{tag}", f"dual-{tag}"}
        assert seen["device_b"] == {f"srv-b-{tag}"}
        assert seen["class_switch"] == {f"sw-a-{tag}"}
        assert seen["narrowed_a"] == set() and seen["expired_b"] == set()
    finally:
        await _cleanup(maker, tenant)
        await engine.dispose()


async def test_the_partial_unique_index_holds_on_postgres():
    tenant = f"tx1pg-{uuid.uuid4().hex[:8]}"
    engine = make_engine(CC_DSN)
    maker = make_sessionmaker(engine)
    try:
        async with maker() as session:
            rows = [
                CCResource(tenant_id=tenant, resource_kind=KIND_PHYSICAL_CONTAINER,
                           resource_type=TYPE_SITE, display_name=n, source="pg",
                           lifecycle_state="active")
                for n in ("p1", "p2")
            ] + [CCResource(tenant_id=tenant, resource_kind=KIND_PHYSICAL_DEVICE,
                            resource_type="harkeniq:compute.server", display_name="c",
                            source="pg", lifecycle_state="active")]
            session.add_all(rows)
            await session.flush()
            p1, p2, child = rows
            session.add(CCResourceRelationship(
                tenant_id=tenant, from_resource_id=p1.id, to_resource_id=child.id,
                relationship_type=REL_CONTAINS, is_primary=True, source="pg"))
            # A secondary edge to the same child is fine.
            session.add(CCResourceRelationship(
                tenant_id=tenant, from_resource_id=p2.id, to_resource_id=child.id,
                relationship_type="MEMBER_OF", is_primary=False, source="pg"))
            await session.commit()
        async with maker() as session:
            session.add(CCResourceRelationship(
                tenant_id=tenant, from_resource_id=p2.id, to_resource_id=child.id,
                relationship_type=REL_CONTAINS, is_primary=True, source="pg"))
            with pytest.raises(IntegrityError):
                await session.commit()
        # TX-2 (A32.3) narrowed the guarantee to ACTIVE edges, under a new
        # name; "one primary parent" is still the database's to keep.
        async with engine.connect() as conn:
            definition = (await conn.execute(sa.text(
                "SELECT indexdef FROM pg_indexes WHERE indexname = "
                "'uq_resource_active_primary_parent'"))).scalar_one()
        assert "UNIQUE" in definition and "WHERE (is_primary" in definition, definition
    finally:
        await _cleanup(maker, tenant)
        await engine.dispose()


async def test_two_replicas_reconcile_at_once_and_one_resource_results():
    tenant = f"tx1pg-{uuid.uuid4().hex[:8]}"
    engines = [make_engine(CC_DSN), make_engine(CC_DSN)]
    makers = [make_sessionmaker(e) for e in engines]
    try:
        await _estate(makers[0], tenant)

        async def _replica(maker):
            async with maker() as session:
                report = await reconcile(session, tenant)
                await session.commit()
                return report

        for _round in range(3):
            reports = await asyncio.gather(*(_replica(m) for m in makers * 3))
            assert all(r.conflicts == 0 for r in reports)
        async with makers[0]() as session:
            counts = (await session.execute(sa.text(
                "SELECT legacy_kind, legacy_ref, count(*) FROM cc_resources "
                "WHERE tenant_id = :t GROUP BY legacy_kind, legacy_ref"),
                {"t": tenant})).all()
            primaries = (await session.execute(sa.text(
                "SELECT to_resource_id, count(*) FROM cc_resource_relationships "
                "WHERE tenant_id = :t AND is_primary GROUP BY to_resource_id"),
                {"t": tenant})).all()
        assert counts and all(n == 1 for _k, _r, n in counts), counts
        assert {k for k, _r, _n in counts} == {LEGACY_SITE, LEGACY_DEVICE}
        assert len(counts) == 2 + 4
        assert all(n == 1 for _c, n in primaries)
    finally:
        await _cleanup(makers[0], tenant)
        for engine in engines:
            await engine.dispose()


async def test_cc_0029_to_0030_with_rows_backfills_nothing():
    tenant = f"tx1pg-{uuid.uuid4().hex[:8]}"
    engine = make_engine(CC_DSN)
    maker = make_sessionmaker(engine)
    try:
        await _estate(maker, tenant)
        async with engine.connect() as conn:
            before = (await conn.execute(sa.text(
                "SELECT (SELECT count(*) FROM cc_sites WHERE tenant_id = :t), "
                "(SELECT count(*) FROM cc_fleet_cache f JOIN cc_sites s "
                " ON s.id = f.site_id WHERE s.tenant_id = :t)"), {"t": tenant})).one()
            for table in TABLES:
                await conn.execute(sa.text(f"DROP TABLE IF EXISTS {table}"))
            await conn.execute(sa.text("UPDATE alembic_version SET version_num = '0029'"))
            await conn.commit()

        _alembic("upgrade", "head")
        async with engine.connect() as conn:
            version = (await conn.execute(sa.text(
                "SELECT version_num FROM alembic_version"))).scalar_one()
            present = set((await conn.execute(sa.text(
                "SELECT tablename FROM pg_tables WHERE tablename = ANY(:n)"),
                {"n": list(TABLES)})).scalars())
            valid = (await conn.execute(sa.text(
                "SELECT i.indisvalid AND i.indisunique FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = 'uq_resource_active_primary_parent'"))).scalar_one()
            resources = (await conn.execute(sa.text(
                "SELECT count(*) FROM cc_resources"))).scalar_one()
            after = (await conn.execute(sa.text(
                "SELECT (SELECT count(*) FROM cc_sites WHERE tenant_id = :t), "
                "(SELECT count(*) FROM cc_fleet_cache f JOIN cc_sites s "
                " ON s.id = f.site_id WHERE s.tenant_id = :t)"), {"t": tenant})).one()
            kinds = dict((await conn.execute(sa.text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = 'cc_resources'"))).all())
        assert version >= "0030", version
        assert present == set(TABLES) and valid
        assert resources == 0, "the migration must not backfill: the mirror does"
        assert tuple(after) == tuple(before)
        assert kinds["retired_at"] == "timestamp with time zone"

        _alembic("upgrade", "head")              # idempotent
        _alembic("downgrade", "0029")
        async with engine.connect() as conn:
            gone = set((await conn.execute(sa.text(
                "SELECT tablename FROM pg_tables WHERE tablename = ANY(:n)"),
                {"n": list(TABLES)})).scalars())
        assert gone == set()
        _alembic("upgrade", "head")
        # And the mirror populates what the migration did not.
        async with maker() as session:
            report = await reconcile(session, tenant)
            await session.commit()
        assert report.created == 2 + 4
    finally:
        _alembic("upgrade", "head")
        await _cleanup(maker, tenant)
        await engine.dispose()
