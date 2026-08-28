"""Spec S4: "enforcement is server-side; the UI only reflects them."

The reflection half was never built. The SPA derived its own view of the
user by decoding the access token and got it wrong three ways at once:
it read realm_access.roles while Keycloak mints realm_roles, it filtered
for an "hiq_" prefix the roles do not carry, and no permissions /
tenant_id / is_platform_user claim is minted at all. Every user rendered
as "viewer", the platform super admin included.

/api/me is now the single source of truth the UI reflects, computed from
the same ROLE_PERMISSIONS the request guards use. These tests pin it per
role, so a nav entry and its endpoint can never disagree about who may
see it.
"""

import httpx
import pytest

from harkeniq_console.app import create_app
from harkeniq_console.auth import UserContext, get_current_user
from harkeniq_console.config import ConsoleConfig
from harkeniq_console.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_console.db.models import CustomRole, Tenant, User, UserCustomRole
from harkeniq_console.permissions import ROLE_PERMISSIONS
from harkeniq_console.runtime import AppState


async def _app_as(role: str, *, platform: bool = False, tenants: int = 1,
                  tenant_id: str | None = None, user_id: str = "kc-sub-1"):
    """Client authenticated as *role*, without minting real JWTs."""
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sm = make_sessionmaker(engine)
    ids = []
    async with sm() as session:
        for i in range(tenants):
            t = Tenant(name=f"T{i}", slug=f"t{i}", billing_country="US")
            session.add(t)
            await session.flush()
            ids.append(t.id)
        await session.commit()
    resolved_tenant = tenant_id or (ids[0] if ids and not platform else None)

    config = ConsoleConfig(insecure=False)
    state = AppState(config=config, engine=engine, sessionmaker=sm)
    app = create_app(state)

    async def _fake_user() -> UserContext:
        return UserContext(
            user_id=user_id,
            email=f"{role}@example.com",
            tenant_id=resolved_tenant,
            role=role,
            permissions=[],
            is_platform_user=platform,
        )

    app.dependency_overrides[get_current_user] = _fake_user
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
        follow_redirects=True,
    )
    return client, engine, sm, ids


class TestWhoAmI:
    @pytest.mark.parametrize("role", sorted(ROLE_PERMISSIONS))
    async def test_every_role_gets_its_exact_permission_set(self, role):
        platform = role.startswith("platform_")
        client, engine, _sm, _ids = await _app_as(role, platform=platform)
        try:
            r = await client.get("/api/me")
            assert r.status_code == 200
            body = r.json()
            assert body["role"] == role
            assert set(body["permissions"]) == ROLE_PERMISSIONS[role]
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_viewer_is_not_handed_admin_permissions(self):
        # The old UI defaulted everyone to "viewer"; the inverse mistake —
        # a viewer reading as privileged — must be impossible too.
        client, engine, _sm, _ids = await _app_as("viewer")
        try:
            perms = (await client.get("/api/me")).json()["permissions"]
            assert perms == ["fleet.view", "incident.view"]
            for forbidden in ("tenant.manage", "admin.dashboard",
                              "billing.manage", "action.approve"):
                assert forbidden not in perms
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_super_admin_holds_everything(self):
        client, engine, _sm, _ids = await _app_as(
            "platform_super_admin", platform=True
        )
        try:
            body = (await client.get("/api/me")).json()
            assert body["is_platform_user"] is True
            assert set(body["permissions"]) == set(
                ROLE_PERMISSIONS["platform_super_admin"]
            )
        finally:
            await client.aclose()
            await engine.dispose()


class TestCustomRoleGrants:
    """S4: tenants may define permission bundles and assign them like fixed
    roles. The tables and the assignment API shipped in R2b, but
    get_current_user hardcoded permissions=[], so an assigned bundle
    granted nothing and has_permission's custom branch was dead."""

    async def test_assigned_bundle_widens_the_permission_set(self):
        client, engine, sm, ids = await _app_as("viewer", user_id="kc-sub-9")
        try:
            async with sm() as session:
                user = User(
                    tenant_id=ids[0], keycloak_user_id="kc-sub-9",
                    email="v@example.com", role="viewer",
                )
                session.add(user)
                await session.flush()
                cr = CustomRole(
                    tenant_id=ids[0], name="incident-responder",
                    permissions=["action.approve", "incident.acknowledge"],
                    created_by="seed",
                )
                session.add(cr)
                await session.flush()
                session.add(
                    UserCustomRole(user_id=user.id, custom_role_id=cr.id)
                )
                await session.commit()

            from harkeniq_console.permissions import effective_permissions

            async with sm() as session:
                grants = await effective_permissions(session, "kc-sub-9")
            assert grants == ["action.approve", "incident.acknowledge"]
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_bundle_cannot_invent_permissions(self):
        # A bundle is a subset of the known atomic set, never an extension
        # point; an unknown string must not become a grant.
        client, engine, sm, ids = await _app_as("viewer", user_id="kc-sub-8")
        try:
            async with sm() as session:
                user = User(
                    tenant_id=ids[0], keycloak_user_id="kc-sub-8",
                    email="v8@example.com", role="viewer",
                )
                session.add(user)
                await session.flush()
                cr = CustomRole(
                    tenant_id=ids[0], name="sneaky",
                    permissions=["root.everything", "fleet.view"],
                    created_by="seed",
                )
                session.add(cr)
                await session.flush()
                session.add(
                    UserCustomRole(user_id=user.id, custom_role_id=cr.id)
                )
                await session.commit()

            from harkeniq_console.permissions import effective_permissions

            async with sm() as session:
                grants = await effective_permissions(session, "kc-sub-8")
            assert grants == ["fleet.view"]
            assert "root.everything" not in grants
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_unknown_subject_gets_no_extra_grants(self):
        client, engine, sm, _ids = await _app_as("viewer", user_id="kc-nobody")
        try:
            from harkeniq_console.permissions import effective_permissions

            async with sm() as session:
                assert await effective_permissions(session, "kc-nobody") == []
        finally:
            await client.aclose()
            await engine.dispose()


class TestTenantSelector:
    """QA-046 remainder: the "current" alias resolves a sole tenant, but a
    platform admin on a multi-tenant install had no way to say which."""

    async def test_platform_user_may_choose_among_tenants(self):
        client, engine, _sm, ids = await _app_as(
            "platform_super_admin", platform=True, tenants=3
        )
        try:
            body = (await client.get("/api/me/tenants")).json()
            assert body["selectable"] is True
            assert {t["id"] for t in body["tenants"]} == set(ids)
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_tenant_user_gets_only_their_own_and_no_choice(self):
        client, engine, _sm, ids = await _app_as("tenant_owner", tenants=3)
        try:
            body = (await client.get("/api/me/tenants")).json()
            assert body["selectable"] is False
            assert [t["id"] for t in body["tenants"]] == [ids[0]]
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_header_selects_a_tenant_the_alias_could_not_resolve(self):
        # Two tenants and a platform caller: "current" is ambiguous and used
        # to 404. The explicit header disambiguates it.
        client, engine, _sm, ids = await _app_as(
            "platform_super_admin", platform=True, tenants=2
        )
        try:
            # Refused explicitly. This used to return 200 with an empty
            # list, which reads as "no audit entries" rather than "no
            # tenant selected".
            ambiguous = await client.get("/api/tenants/current/audit")
            assert ambiguous.status_code == 400
            assert "select a tenant" in ambiguous.json()["detail"]

            chosen = await client.get(
                "/api/tenants/current/audit",
                headers={"x-harken-tenant": ids[1]},
            )
            assert chosen.status_code == 200
        finally:
            await client.aclose()
            await engine.dispose()


class TestSubjectlessTokens:
    """This deployment's realm import omitted Keycloak's `basic` client
    scope, which is where the `sub` mapper lives from Keycloak 24 on. Its
    access tokens therefore carry NO sub claim, so UserContext.user_id is
    empty and a lookup keyed on it can never match. The realm is fixed, but
    an existing deployment keeps minting subject-less tokens until it is
    re-imported, so the lookup falls back to email within the caller's own
    tenant.
    """

    async def test_email_fallback_resolves_grants_without_a_subject(self):
        client, engine, sm, ids = await _app_as("viewer", user_id="")
        try:
            async with sm() as session:
                user = User(
                    tenant_id=ids[0], keycloak_user_id=None,
                    email="nosub@example.com", role="viewer",
                )
                session.add(user)
                await session.flush()
                cr = CustomRole(
                    tenant_id=ids[0], name="approver",
                    permissions=["action.approve"], created_by="seed",
                )
                session.add(cr)
                await session.flush()
                session.add(
                    UserCustomRole(user_id=user.id, custom_role_id=cr.id)
                )
                await session.commit()

            from harkeniq_console.permissions import effective_permissions

            async with sm() as session:
                grants = await effective_permissions(
                    session, "", "nosub@example.com", ids[0]
                )
            assert grants == ["action.approve"]
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_fallback_is_tenant_scoped(self):
        # The same email in another tenant must not inherit its grants.
        client, engine, sm, ids = await _app_as("viewer", tenants=2, user_id="")
        try:
            async with sm() as session:
                other = User(
                    tenant_id=ids[1], keycloak_user_id=None,
                    email="shared@example.com", role="viewer",
                )
                session.add(other)
                await session.flush()
                cr = CustomRole(
                    tenant_id=ids[1], name="powerful",
                    permissions=["billing.manage"], created_by="seed",
                )
                session.add(cr)
                await session.flush()
                session.add(
                    UserCustomRole(user_id=other.id, custom_role_id=cr.id)
                )
                await session.commit()

            from harkeniq_console.permissions import effective_permissions

            async with sm() as session:
                # Asking as tenant 0 must not find tenant 1's user.
                grants = await effective_permissions(
                    session, "", "shared@example.com", ids[0]
                )
            assert grants == []
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_realm_import_now_mints_sub(self):
        # Root cause, pinned so a future realm edit cannot silently drop it.
        import json
        from pathlib import Path

        realm = json.loads(
            Path("deploy/r2b/keycloak-realm-platform.json").read_text()
        )
        for client_def in realm.get("clients", []):
            scopes = client_def.get("defaultClientScopes")
            if scopes is not None:
                assert "basic" in scopes, (
                    f"{client_def.get('clientId')} omits the 'basic' client "
                    "scope; its access tokens will carry no sub claim"
                )
