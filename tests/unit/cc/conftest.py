"""Central Command test fixtures: in-memory aiosqlite database."""

import pytest

from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker


@pytest.fixture
async def db():
    """Fresh in-memory database; yields an async sessionmaker."""
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    yield make_sessionmaker(engine)
    await engine.dispose()


@pytest.fixture
async def session(db):
    async with db() as s:
        yield s


# ---------------------------------------------------------------------------
# A23-5 strict birth: what a tenant looks like after A23.11
# ---------------------------------------------------------------------------
#
# Before A23-5 a missing `cc_tenant_settings` row meant `legacy_open`, and
# `legacy_open` synthesized tenant-wide reach for a never-granted human.
# Most fixtures here relied on that without saying so: they built an app
# on a fresh database, wrote no settings row and no grant, and then acted
# as an administrator who was tenant-wide only by synthesis.
#
# That is no longer a state any real tenant can be in. A tenant is now
# either born strict WITH its first administrator (A23.14 D3/D4) or
# pinned to an explicit posture by migration 0021. These two helpers are
# those two shapes, so a fixture says which one it means.


async def seed_tenant_admin(
    sessionmaker, tenant: str, principal_ref: str = "lab-user",
    *, role: str = "tenant_owner", realm: str = "",
):
    """The tenant's first administrator, as tenant birth would seed it.

    Uses the same repository seam production uses, so a fixture cannot
    drift from what a real tenant holds.
    """
    from harkeniq_cc.db.repos import ScopeGrantRepo

    async with sessionmaker() as session:
        await ScopeGrantRepo(session).seed_first_grant(
            tenant_id=tenant,
            principal_ref=principal_ref,
            role=role,
            realm=realm,
            granted_by="system:tenant_birth",
            note="test fixture: the tenant's founding administrator",
        )
        await session.commit()


async def seed_legacy(sessionmaker, tenant: str):
    """Pin the tenant `legacy_open`, as migration 0021 pins an existing one.

    For tests that are ABOUT the legacy posture. A test that merely
    wants a working administrator wants `seed_tenant_admin` instead --
    pinning legacy_open to make a test pass would keep asserting the
    invariant A23-5 retired.
    """
    from harkeniq_cc.db.repos import TenantSettingsRepo
    from harkeniq_cc.scope import ENFORCEMENT_LEGACY_OPEN

    async with sessionmaker() as session:
        await TenantSettingsRepo(session).set_enforcement(
            tenant, ENFORCEMENT_LEGACY_OPEN, "migration:0021",
        )
        await session.commit()


async def seed_tenant_people(sessionmaker, tenant: str, people):
    """A tenant whose named people are all really granted.

    `people` is an iterable of ``(principal_ref, role)``. The first is
    seeded as the founding administrator the way tenant birth seeds one;
    the rest are granted the way an administrator grants them.

    For fixtures that switch personas. Before A23-5 every one of those
    personas was tenant-wide by `legacy_open` synthesis, so the suite
    was silently testing an ungoverned tenant; granting them explicitly
    keeps the tests' intent and runs them under the posture a real
    tenant has.
    """
    from harkeniq_cc.db.repos import ScopeGrantRepo

    people = list(people)
    if not people:
        return
    first_ref, first_role = people[0]
    await seed_tenant_admin(sessionmaker, tenant, first_ref, role=first_role)
    async with sessionmaker() as session:
        repo = ScopeGrantRepo(session)
        for ref, role in people[1:]:
            await repo.grant(
                tenant_id=tenant, principal_type="user", principal_ref=ref,
                scope_type="tenant", scope_ref="", role=role,
                granted_by="test fixture",
            )
        await session.commit()
