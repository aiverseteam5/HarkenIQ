"""A26.11 on real PostgreSQL: the grant lifecycle decides the read.

The unit matrix drives the same handlers on sqlite. This runs the
authority decision on the engine production uses, because the parts that
carry it are not engine-neutral in the ways that have bitten this
codebase before:

* Lifecycle filtering is a **timestamp comparison** (`expires_at`,
  `revoked_at`) against a timezone-aware `now`. sqlite has no native
  timestamptz; PostgreSQL does, and a scope that silently compared naive
  to aware would fail open.
* `permission_subset` is a **JSON column** — `JSONVariant`, which is
  `JSONB` on PostgreSQL and TEXT-backed JSON on sqlite. A subset that
  round-tripped differently would change which permissions a grant
  carries.
* Identity columns are VARCHAR-width-checked on PostgreSQL and ignored on
  sqlite. That has cost this codebase three times (QA-040, E0.1, A20).

Gated on ``HARKEN_TEST_CC_PG_DSN``; skipped when unset.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from harkeniq_cc.auth import ROLE_PERMISSIONS
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.models import CCApprovalGroup, CCScopeGrant, CCSite
from harkeniq_cc.governance import load_scope
from harkeniq_cc.scope import SCOPE_ORG_UNIT, SCOPE_SITE, SCOPE_TENANT

DSN = os.environ.get("HARKEN_TEST_CC_PG_DSN", "")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not DSN, reason="HARKEN_TEST_CC_PG_DSN not set"),
    pytest.mark.asyncio,
]

PERMISSION = "governance.view"


async def _engine():
    engine = make_engine(DSN)
    await create_all(engine)
    return engine


async def _authority(sessionmaker, tenant: str, principal: str) -> bool:
    """The production question, through the production loader."""
    async with sessionmaker() as session:
        scope = await load_scope(
            session, tenant_id=tenant, principal_ref=principal,
            role_permissions=list(ROLE_PERMISSIONS["site_admin"]),
            realm="",
        )
        return scope.permits(PERMISSION, tenant_object=True)


@pytest.mark.asyncio
async def test_the_grant_lifecycle_decides_on_postgres():
    """Nine grant shapes, one question, on the real engine."""
    engine = await _engine()
    sm = make_sessionmaker(engine)
    tenant = f"t-a26-{uuid.uuid4().hex[:8]}"
    past = datetime.now(timezone.utc) - timedelta(days=1)
    future = datetime.now(timezone.utc) + timedelta(days=1)

    async with sm() as session:
        site = CCSite(tenant_id=tenant, site_name="s1", sm_endpoint="sm:1",
                      sm_token="t")
        session.add(site)
        await session.flush()
        site_id = site.id
        session.add(CCApprovalGroup(
            tenant_id=tenant, name="on-call", created_by="kc-owner"))

        rows = {
            # (principal, expected authority)
            "tenant-active":  (dict(scope_type=SCOPE_TENANT, scope_ref=""), True),
            "tenant-future":  (dict(scope_type=SCOPE_TENANT, scope_ref="",
                                    expires_at=future), True),
            "tenant-expired": (dict(scope_type=SCOPE_TENANT, scope_ref="",
                                    expires_at=past), False),
            "tenant-revoked": (dict(scope_type=SCOPE_TENANT, scope_ref="",
                                    revoked_at=past), False),
            "tenant-narrow":  (dict(scope_type=SCOPE_TENANT, scope_ref="",
                                    permission_subset=["fleet.view"]), False),
            "tenant-subset-ok": (dict(scope_type=SCOPE_TENANT, scope_ref="",
                                      permission_subset=[PERMISSION]), True),
            "site-scoped":    (dict(scope_type=SCOPE_SITE, scope_ref=site_id), False),
            "org-scoped":     (dict(scope_type=SCOPE_ORG_UNIT,
                                    scope_ref="unit-vanished"), False),
            "device-scoped":  (dict(scope_type="device", scope_ref="node-1"), False),
        }
        for principal, (kwargs, _expected) in rows.items():
            session.add(CCScopeGrant(
                tenant_id=tenant, principal_type="user",
                principal_ref=f"kc-{principal}", granted_by="test",
                role="site_admin", **kwargs,
            ))
        await session.commit()

    try:
        for principal, (_kwargs, expected) in rows.items():
            got = await _authority(sm, tenant, f"kc-{principal}")
            assert got is expected, (
                f"{principal}: effective {PERMISSION} over the tenant was "
                f"{got}, expected {expected}"
            )
        # And a principal with no row at all, under the strict default.
        assert await _authority(sm, tenant, "kc-nobody") is False
    finally:
        async with sm() as session:
            await session.execute(
                sa.delete(CCScopeGrant).where(CCScopeGrant.tenant_id == tenant))
            await session.execute(
                sa.delete(CCApprovalGroup).where(
                    CCApprovalGroup.tenant_id == tenant))
            await session.execute(
                sa.delete(CCSite).where(CCSite.tenant_id == tenant))
            await session.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_narrowed_subset_round_trips_through_jsonb():
    """The subset must mean the same thing after a PostgreSQL round trip.

    `permission_subset` is the field that decides A26.11's hardest case,
    and it is JSON. If it came back as a string, `permission not in
    grant.permissions` would be a substring test and `governance.view`
    would be found inside a subset that never named it.
    """
    engine = await _engine()
    sm = make_sessionmaker(engine)
    tenant = f"t-a26-{uuid.uuid4().hex[:8]}"
    async with sm() as session:
        session.add(CCScopeGrant(
            tenant_id=tenant, principal_type="user", principal_ref="kc-sub",
            scope_type=SCOPE_TENANT, scope_ref="", granted_by="test",
            role="site_admin",
            # Deliberately hostile: a value that CONTAINS the permission
            # name as a substring without being it.
            permission_subset=["fleet.view", "not-governance.view-either"],
        ))
        await session.commit()
    try:
        async with sm() as session:
            row = (await session.execute(
                sa.select(CCScopeGrant).where(
                    CCScopeGrant.principal_ref == "kc-sub")
            )).scalars().one()
            assert isinstance(row.permission_subset, list), row.permission_subset
        assert await _authority(sm, tenant, "kc-sub") is False, (
            "a substring match let a narrowed grant carry governance.view"
        )
    finally:
        async with sm() as session:
            await session.execute(
                sa.delete(CCScopeGrant).where(CCScopeGrant.tenant_id == tenant))
            await session.commit()
        await engine.dispose()
