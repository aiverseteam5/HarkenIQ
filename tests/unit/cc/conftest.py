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


class ConsoleRealm:
    """A30.23: the identity plane, as the campaign runner reaches it at dispatch.

    The REAL Console internal router in SECURE mode (the shared key is
    enforced, so a Central Command that stopped sending it would be
    refused), over a real Console database with the tenant bound to
    `realm`, and the in-memory Keycloak double behind it. `wire()` routes
    Central Command's `identity_client` through this application
    in-process, so the production read -- URL, key, realm binding, the
    `found`/`enabled`/`realm_roles` answer and the role rule that turns it
    into a basis -- runs end to end. Only the HTTP socket is replaced.

    Subjects are CHOSEN here rather than minted, because the ledger
    names them: a test writes `approver_ref="kc-abc"` and this realm must
    hold exactly that subject. The double's storage is seeded directly
    for that reason, the way its own `create_user` seeds it.
    """

    KEY = "shared-cc-key"

    def __init__(self, realm: str) -> None:
        self.realm = realm
        self.app = None
        self.keycloak = None
        self._engine = None

    async def start(self) -> "ConsoleRealm":
        from fastapi import FastAPI

        from harkeniq_console.api.deps import get_session
        from harkeniq_console.api.internal import router
        from harkeniq_console.config import ConsoleConfig
        from harkeniq_console.db.base import create_all, make_engine, make_sessionmaker
        from harkeniq_console.db.repos import TenantRepo
        from harkeniq_console.keycloak_admin import MockKeycloakAdminClient
        from harkeniq_console.runtime import AppState

        self._engine = make_engine("sqlite+aiosqlite:///:memory:")
        await create_all(self._engine)
        sessionmaker = make_sessionmaker(self._engine)
        async with sessionmaker() as session:
            repo = TenantRepo(session)
            tenant = await repo.create(
                name=self.realm, slug=self.realm, billing_country="US", currency="USD",
            )
            await repo.update(tenant, keycloak_realm=self.realm)
            await session.commit()
        self.keycloak = MockKeycloakAdminClient()
        await self.keycloak.create_realm(self.realm)

        app = FastAPI()
        app.state.console = AppState(
            config=ConsoleConfig(insecure=False, internal_api_key=self.KEY),
        )
        app.state.console.keycloak_admin = self.keycloak

        async def _session():
            async with sessionmaker() as s:
                yield s

        app.dependency_overrides[get_session] = _session
        app.include_router(router)
        self.app = app
        return self

    async def stop(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()

    # -- the people the realm holds ------------------------------------------

    def person(self, subject: str, *roles: str, enabled: bool = True) -> str:
        self.keycloak._users[self.realm][subject] = {
            "id": subject, "username": f"{subject}@example.com",
            "email": f"{subject}@example.com", "firstName": subject,
            "lastName": "Person", "emailVerified": True, "enabled": enabled,
        }
        self.keycloak._role_mappings[(self.realm, subject)] = list(roles)
        return subject

    def roles_of(self, subject: str) -> list[str]:
        return list(self.keycloak._role_mappings.get((self.realm, subject), []))

    def demote(self, subject: str, role: str) -> None:
        """Remove ONE realm-role mapping -- what an administrator does in
        Keycloak. The subject, its grants and its ledger rows are untouched."""
        mapping = self.keycloak._role_mappings[(self.realm, subject)]
        assert role in mapping, f"{subject} does not hold {role}"
        mapping.remove(role)

    def promote(self, subject: str, role: str) -> None:
        self.keycloak._role_mappings[(self.realm, subject)].append(role)

    def set_enabled(self, subject: str, enabled: bool) -> None:
        self.keycloak._users[self.realm][subject]["enabled"] = enabled

    def delete(self, subject: str) -> None:
        del self.keycloak._users[self.realm][subject]
        self.keycloak._role_mappings.pop((self.realm, subject), None)

    # -- wiring Central Command to it ----------------------------------------

    def wire(self, monkeypatch) -> None:
        """Route `identity_client`'s HTTP through this application.

        Only `identity_client`'s view of httpx is replaced, and only the
        transport is injected: the URL it builds, the key it sends and
        the answer it parses are the production ones.
        """
        import types

        import httpx

        from harkeniq_cc import identity_client

        real = httpx.AsyncClient
        app = self.app

        def _client(*args, **kwargs):
            kwargs.setdefault("transport", httpx.ASGITransport(app=app))
            return real(*args, **kwargs)

        monkeypatch.setattr(
            identity_client, "httpx", types.SimpleNamespace(AsyncClient=_client),
        )

    def attach(self, state) -> None:
        """Point one Central Command at this realm and this Console."""
        state.config.keycloak_realm = self.realm
        state.config.console_url = "http://console.test"
        state.config.console_api_key = self.KEY
