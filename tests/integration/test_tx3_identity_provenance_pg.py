"""TX-3 (A33) on a REAL PostgreSQL: identity, aliases and provenance.

The sqlite proofs live in tests/unit/cc/test_tx3_identity_provenance.py.
This file asks what sqlite cannot answer honestly:

* **The keys are real** -- migration 0032 adds the composite (resource,
  tenant) foreign keys for identifier bindings and aliases with ALTER; the
  database must refuse a row naming another tenant's resource, a dangling
  one, and a second row for one alias.
* **Through the app, on the real engine** -- identity candidates and the
  alias lookup agree with visibility for a tenant-wide, a site and a device
  reader; provenance is timestamptz and round-trips with its zone.
* **Two replicas** -- concurrent passes changing a node's serial each round
  leave one row per alias and one ACTIVE service tag per device.
* **The upgrade** -- 0031 -> 0032 on a database holding resources and
  edges: the columns arrive NULL, nothing is backfilled, the keys are
  present and validated, the next pass records provenance; a row that
  already crosses tenants refuses the upgrade loudly and is deleted by
  nobody but the operator.

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
from harkeniq_cc.db.models import (
    CCFleetCache,
    CCResource,
    CCResourceAlias,
    CCResourceExternalId,
    CCSite,
)
from harkeniq_cc.db.repos import ResourceRepo, ScopeGrantRepo, TenantSettingsRepo
from harkeniq_cc.resource_catalogue import (
    EXTERNAL_SOURCE_HARKENIQ,
    FEED_FLEET_SNAPSHOT,
    KIND_PHYSICAL_DEVICE,
    LEGACY_DEVICE,
    IdentityConflict,
    normalize_alias,
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


def _alembic(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("HARKEN_")}
    env["HARKEN_CC_DSN"] = CC_DSN
    env["HARKEN_CC_TENANT_ID"] = "tenant-demo"
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO / "services" / "central_command", env=env,
        capture_output=True, text=True,
    )
    if check:
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    return result


async def _estate(maker, tenant: str) -> dict:
    """Two sites. srv-a (A) and srv-b (B) report ONE serial in different
    case and spacing; sw-a (A) reports its MAC."""
    ids = {"tag": tenant[-8:]}
    tag = ids["tag"]
    async with maker() as session:
        for key in ("a", "b"):
            site = CCSite(tenant_id=tenant, site_name=f"tx3-{key}-{tenant}",
                          sm_endpoint="sm:50051", sm_token="tok")
            session.add(site)
            await session.flush()
            ids[key] = site.id
        for site, agent, cls, serial in (
            ("a", f"srv-a-{tag}", "server", f"SHARED-{tag}"),
            ("a", f"sw-a-{tag}", "switch", f"aa:bb:{tag}"),
            ("b", f"srv-b-{tag}", "server", f"  shared-{tag} "),
        ):
            session.add(CCFleetCache(
                site_id=ids[site], agent_id=agent, agent_name=agent, vendor="Dell",
                model="R750", health="ok", device_class=cls, service_tag=serial,
            ))
        await session.commit()
        await TenantSettingsRepo(session).set_enforcement(tenant, ENFORCEMENT_STRICT, "tx3-pg")
        await session.commit()
    return ids


async def _cleanup(maker, tenant: str) -> None:
    async with maker() as session:
        for stmt in (
            "DELETE FROM cc_resource_aliases WHERE tenant_id = :t",
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


async def _reconcile(maker, tenant):
    async with maker() as session:
        report = await reconcile(session, tenant)
        await session.commit()
    return report


async def _device(maker, tenant, agent):
    async with maker() as session:
        return await ResourceRepo(session).get_by_legacy(tenant, LEGACY_DEVICE, agent)


def _row(tenant, name):
    return CCResource(tenant_id=tenant, resource_kind=KIND_PHYSICAL_DEVICE,
                      resource_type="harkeniq:compute.server", display_name=name,
                      source="pg", lifecycle_state="active")


async def test_the_binding_and_alias_keys_are_real():
    tenant = f"tx3pg-{uuid.uuid4().hex[:8]}"
    other = f"tx3pg-{uuid.uuid4().hex[:8]}"
    engine = make_engine(CC_DSN)
    maker = make_sessionmaker(engine)
    try:
        async with maker() as session:
            mine, theirs = _row(tenant, "mine"), _row(other, "theirs")
            session.add_all([mine, theirs])
            await session.flush()
            session.add(CCResourceAlias(
                tenant_id=tenant, resource_id=mine.id, alias_type="hostname",
                value="h", normalized_value="h", source_system="pg",
                source_namespace="ci", source="pg",
            ))
            await session.commit()
            ids = {"mine": mine.id, "theirs": theirs.id}

        def _binding(resource_id):
            return CCResourceExternalId(
                tenant_id=tenant, resource_id=resource_id, source_system="cmdb",
                namespace="sys_id", external_id=uuid.uuid4().hex, source="pg")

        def _alias(resource_id, value="h2"):
            return CCResourceAlias(
                tenant_id=tenant, resource_id=resource_id, alias_type="hostname",
                value=value, normalized_value=value, source_system="pg",
                source_namespace="ci", source="pg")

        refused = [
            _binding(ids["theirs"]),          # another tenant's resource
            _binding("0" * 32),               # dangling
            _alias(ids["theirs"]),
            _alias("0" * 32),
            _alias(ids["mine"], "h"),         # one row per alias, for life
        ]
        for row in refused:
            async with maker() as session:
                session.add(row)
                with pytest.raises(IntegrityError):
                    await session.commit()
        async with engine.connect() as conn:
            keys = set((await conn.execute(sa.text(
                "SELECT conname FROM pg_constraint WHERE contype = 'f' AND convalidated "
                "AND conname IN ('fk_resource_external_id_tenant', 'fk_resource_alias_tenant')"
            ))).scalars())
        assert keys == {"fk_resource_external_id_tenant", "fk_resource_alias_tenant"}
    finally:
        await _cleanup(maker, tenant)
        await _cleanup(maker, other)
        await engine.dispose()


async def test_identity_aliases_and_candidates_through_the_app_on_postgres():
    tenant = f"tx3pg-{uuid.uuid4().hex[:8]}"
    engine = make_engine(CC_DSN)
    maker = make_sessionmaker(engine)
    try:
        ids = await _estate(maker, tenant)
        tag = ids["tag"]
        await _reconcile(maker, tenant)
        srv_a = await _device(maker, tenant, f"srv-a-{tag}")
        srv_b = await _device(maker, tenant, f"srv-b-{tag}")
        # One identifier per resource, on the real engine too.
        async with maker() as session:
            with pytest.raises(IdentityConflict, match="reconciliation"):
                await ResourceRepo(session).bind_external_id(
                    tenant, srv_a.id, source_system="openmanage", namespace="device",
                    external_id="1", source="pg")
        configure_auth("", "", "", insecure=True)
        state = AppState(config=CCConfig(tenant_id=tenant, insecure=True),
                         engine=engine, sessionmaker=maker)
        app = create_app(state)
        personas = {
            "owner": ("tenant_owner", SCOPE_TENANT, ""),
            "site_a": ("operator", SCOPE_SITE, ids["a"]),
            "device_b": ("viewer", SCOPE_DEVICE, f"srv-b-{tag}"),
        }
        seen = {}
        for name, (role, scope_type, ref) in personas.items():
            user = f"kc-{name}-{tag}"
            async with maker() as session:
                await ScopeGrantRepo(session).grant(
                    tenant_id=tenant, principal_type="user", principal_ref=user,
                    scope_type=scope_type, scope_ref=ref, permission_subset=None,
                    role=role, granted_by="tx3-pg")
                await session.commit()
            async with _client(app, tenant, role, user) as client:
                found = (await client.get("/api/resources/", params={
                    "alias_type": "service_tag", "alias": f"Shared-{tag}",
                    "lifecycle_state": "any"})).json()["resources"]
                seen[name] = {r["id"] for r in found}
                mine = srv_b if name == "device_b" else srv_a
                detail = (await client.get(f"/api/resources/{mine.id}")).json()
                seen[name + ":candidates"] = [
                    c["resource"]["id"] for c in detail["identity_candidates"]["candidates"]]
                if name == "owner":
                    (alias,) = detail["aliases"]
                    assert alias["value"] == f"SHARED-{tag}"
                    assert alias["provenance"]["source_namespace"] == FEED_FLEET_SNAPSHOT
                    assert detail["provenance"]["last_observed_at"].endswith("+00:00")
        assert seen["owner"] == {srv_a.id, srv_b.id}
        assert seen["site_a"] == {srv_a.id}
        assert seen["device_b"] == {srv_b.id}
        assert seen["owner:candidates"] == [srv_b.id]
        assert seen["site_a:candidates"] == [] and seen["device_b:candidates"] == []
        async with engine.connect() as conn:
            kinds = set((await conn.execute(sa.text(
                "SELECT data_type FROM information_schema.columns WHERE table_name IN "
                "('cc_resources', 'cc_resource_relationships', 'cc_resource_external_ids', "
                "'cc_resource_aliases') AND column_name IN "
                "('first_observed_at', 'last_observed_at')"))).scalars())
            backwards = (await conn.execute(sa.text(
                "SELECT count(*) FROM cc_resources WHERE tenant_id = :t "
                "AND first_observed_at > last_observed_at"), {"t": tenant})).scalar_one()
        assert kinds == {"timestamp with time zone"} and backwards == 0
    finally:
        await _cleanup(maker, tenant)
        await engine.dispose()


async def test_two_replicas_assert_aliases_at_once():
    tenant = f"tx3pg-{uuid.uuid4().hex[:8]}"
    engines = [make_engine(CC_DSN), make_engine(CC_DSN)]
    makers = [make_sessionmaker(e) for e in engines]
    try:
        ids = await _estate(makers[0], tenant)
        tag = ids["tag"]
        await _reconcile(makers[0], tenant)            # its first serial, recorded
        for flip in range(3):
            # The node's serial changes every round, so concurrent passes
            # create, retire and revive aliases while they race.
            serial = f"BOARD-{flip % 2}-{tag}"
            async with makers[0]() as session:
                await session.execute(sa.update(CCFleetCache).where(
                    CCFleetCache.agent_id == f"srv-a-{tag}").values(service_tag=serial))
                await session.commit()
            reports = await asyncio.gather(*(_reconcile(m, tenant) for m in makers * 3))
            assert all(r.conflicts == 0 for r in reports)
        async with makers[0]() as session:
            per_alias = (await session.execute(sa.text(
                "SELECT resource_id, alias_type, normalized_value, count(*) "
                "FROM cc_resource_aliases WHERE tenant_id = :t "
                "GROUP BY resource_id, alias_type, normalized_value, source_system, "
                "source_namespace"), {"t": tenant})).all()
            active = (await session.execute(sa.text(
                "SELECT resource_id, count(*) FROM cc_resource_aliases WHERE tenant_id = :t "
                "AND alias_type = 'service_tag' AND lifecycle_state = 'active' "
                "GROUP BY resource_id"), {"t": tenant})).all()
        assert per_alias and all(n == 1 for *_k, n in per_alias), per_alias
        assert len(active) == 3 and all(n == 1 for _r, n in active), active
        srv_a = await _device(makers[0], tenant, f"srv-a-{tag}")
        mine = {v for r, _t, v, _n in per_alias if r == srv_a.id}
        # Its first serial, the two boards it swapped between -- each once.
        assert mine == {normalize_alias(f"SHARED-{tag}"), normalize_alias(f"BOARD-0-{tag}"),
                        normalize_alias(f"BOARD-1-{tag}")}
    finally:
        await _cleanup(makers[0], tenant)
        for engine in engines:
            await engine.dispose()


async def test_cc_0031_to_0032_with_rows_and_back():
    tenant = f"tx3pg-{uuid.uuid4().hex[:8]}"
    other = f"tx3pg-{uuid.uuid4().hex[:8]}"
    engine = make_engine(CC_DSN)
    maker = make_sessionmaker(engine)
    crossing = uuid.uuid4().hex
    try:
        await _estate(maker, tenant)
        await _reconcile(maker, tenant)
        _alembic("downgrade", "0031")
        async with engine.connect() as conn:
            gone = (await conn.execute(sa.text(
                "SELECT count(*) FROM information_schema.columns WHERE table_name = "
                "'cc_resources' AND column_name = 'last_observed_at'"))).scalar()
            aliases_gone = (await conn.execute(sa.text(
                "SELECT to_regclass('cc_resource_aliases') IS NULL"))).scalar()
            before = (await conn.execute(sa.text(
                "SELECT id, legacy_kind, legacy_ref, display_name FROM cc_resources "
                "WHERE tenant_id = :t ORDER BY id"), {"t": tenant})).all()
            edges_before = (await conn.execute(sa.text(
                "SELECT id, from_resource_id, to_resource_id FROM cc_resource_relationships "
                "WHERE tenant_id = :t ORDER BY id"), {"t": tenant})).all()
        assert gone == 0 and aliases_gone and before and edges_before

        # A binding that already crosses tenants: the upgrade refuses it
        # loudly, and deletes nothing to make the key fit.
        foreign = uuid.uuid4().hex
        async with maker() as session:
            # Raw SQL: at 0031 the table has none of TX-3's columns, which
            # the current model would try to write.
            await session.execute(sa.text(
                "INSERT INTO cc_resources (id, tenant_id, resource_kind, resource_type, "
                "display_name, lifecycle_state, source, created_at, updated_at) VALUES "
                "(:id, :t, 'physical_device', 'harkeniq:compute.server', 'foreign', "
                "'active', 'pg', now(), now())"), {"id": foreign, "t": other})
            await session.execute(sa.text(
                "INSERT INTO cc_resource_external_ids (id, tenant_id, resource_id, "
                "source_system, namespace, external_id, source, created_at) VALUES "
                "(:id, :t, :r, 'cmdb', 'sys_id', :v, 'corrupt', now())"),
                {"id": crossing, "t": tenant, "r": foreign, "v": crossing})
            await session.commit()
        refused = _alembic("upgrade", "head", check=False)
        assert refused.returncode != 0
        assert "refusing to add fk_resource_external_id_tenant" in (
            refused.stdout + refused.stderr)
        async with engine.connect() as conn:
            kept = (await conn.execute(sa.text(
                "SELECT count(*) FROM cc_resource_external_ids WHERE id = :id"),
                {"id": crossing})).scalar_one()
        assert kept == 1, "the migration deleted the row instead of refusing"
        async with maker() as session:
            await session.execute(sa.text(
                "DELETE FROM cc_resource_external_ids WHERE id = :id"), {"id": crossing})
            await session.commit()

        _alembic("upgrade", "head")
        async with engine.connect() as conn:
            version = (await conn.execute(sa.text(
                "SELECT version_num FROM alembic_version"))).scalar_one()
            after = (await conn.execute(sa.text(
                "SELECT id, legacy_kind, legacy_ref, display_name FROM cc_resources "
                "WHERE tenant_id = :t ORDER BY id"), {"t": tenant})).all()
            recorded = (await conn.execute(sa.text(
                "SELECT count(*) FROM cc_resources WHERE tenant_id = :t AND "
                "(source_system IS NOT NULL OR last_observed_at IS NOT NULL)"),
                {"t": tenant})).scalar_one()
            aliases = (await conn.execute(sa.text(
                "SELECT count(*) FROM cc_resource_aliases WHERE tenant_id = :t"),
                {"t": tenant})).scalar_one()
            keys = set((await conn.execute(sa.text(
                "SELECT conname FROM pg_constraint WHERE contype = 'f' AND convalidated "
                "AND conname IN ('fk_resource_external_id_tenant', 'fk_resource_alias_tenant')"
            ))).scalars())
        assert version >= "0032", version
        assert after == before, "an additive migration moved a resource"
        assert recorded == 0 and aliases == 0, "the migration must not backfill: the mirror does"
        assert keys == {"fk_resource_external_id_tenant", "fk_resource_alias_tenant"}
        _alembic("upgrade", "head")                    # idempotent

        # The next pass records what it asserts.
        report = await _reconcile(maker, tenant)
        assert report.observed > 0 and report.aliased == 3
        async with engine.connect() as conn:
            unrecorded = (await conn.execute(sa.text(
                "SELECT count(*) FROM cc_resources WHERE tenant_id = :t AND "
                "(source_system IS DISTINCT FROM :s OR last_observed_at IS NULL)"),
                {"t": tenant, "s": EXTERNAL_SOURCE_HARKENIQ})).scalar_one()
        assert unrecorded == 0

        _alembic("downgrade", "0031")
        async with engine.connect() as conn:
            left = (await conn.execute(sa.text(
                "SELECT id, legacy_kind, legacy_ref, display_name FROM cc_resources "
                "WHERE tenant_id = :t ORDER BY id"), {"t": tenant})).all()
        assert left == before, "the downgrade drops only what 0032 added"
    finally:
        async with maker() as session:
            await session.execute(sa.text(
                "DELETE FROM cc_resource_external_ids WHERE id = :id"), {"id": crossing})
            await session.commit()
        _alembic("upgrade", "head")
        await _cleanup(maker, tenant)
        await _cleanup(maker, other)
        await engine.dispose()
