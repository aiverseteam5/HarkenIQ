"""QA-035: the CC<->Console internal API key, actually enforced.

CC has sent ``Authorization: Bearer <console_api_key>`` since R5-2; the
Console shipped with "No auth (internal network)" and never checked it.
"""

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from harkeniq_console.api.internal import require_internal_key
from harkeniq_console.config import ConsoleConfig
from harkeniq_console.runtime import AppState

KEY = "shared-cc-key"


def make_app(insecure: bool, key: str) -> FastAPI:
    from fastapi import Depends

    app = FastAPI()
    app.state.console = AppState(
        config=ConsoleConfig(insecure=insecure, internal_api_key=key)
    )

    @app.get("/probe", dependencies=[Depends(require_internal_key)])
    async def probe() -> dict:
        return {"ok": True}

    return app


async def call(app: FastAPI, headers: dict | None = None) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get("/probe", headers=headers or {})


class TestInternalKey:
    @pytest.mark.asyncio
    async def test_correct_key_accepted(self):
        resp = await call(
            make_app(False, KEY), {"Authorization": f"Bearer {KEY}"}
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_missing_key_rejected(self):
        assert (await call(make_app(False, KEY))).status_code == 401

    @pytest.mark.asyncio
    async def test_wrong_key_rejected(self):
        resp = await call(
            make_app(False, KEY), {"Authorization": "Bearer wrong"}
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_unconfigured_key_fails_closed(self):
        # Secure mode + no key = 503, never open.
        resp = await call(
            make_app(False, ""), {"Authorization": "Bearer anything"}
        )
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_insecure_mode_allows(self):
        assert (await call(make_app(True, ""))).status_code == 200


class TestTenantOwnersByRealm:
    """A23-5 (spec A23.14 D4): the subject CC seeds the first grant on.

    Central Command holds `cc_scope_grants` but not the Keycloak subject
    of the tenant's owner -- that is recorded here, at the identity
    plane, in `users.keycloak_user_id`. This is the read half of the
    channel CC already uses, resolved by REALM because that is the one
    identifier the two services agree on (CC's `tenant_id` is the realm
    name, never the Console's row id).
    """

    async def _app(self, sessionmaker):
        from fastapi import FastAPI

        from harkeniq_console.api.deps import get_session
        from harkeniq_console.api.internal import router

        app = FastAPI()
        app.state.console = AppState(config=ConsoleConfig(insecure=True))

        async def _session():
            async with sessionmaker() as s:
                yield s

        app.dependency_overrides[get_session] = _session
        app.include_router(router)
        return app

    async def _seed(self, sessionmaker, *, subject="kc-owner-subject"):
        from harkeniq_console.db.repos import TenantRepo, UserRepo

        async with sessionmaker() as session:
            tenant = await TenantRepo(session).create(
                name="Acme", slug="acme", billing_country="US", currency="USD",
            )
            await TenantRepo(session).update(tenant, keycloak_realm="acme")
            await UserRepo(session).create(
                tenant_id=tenant.id, email="owner@acme.com",
                role="tenant_owner", keycloak_user_id=subject,
                status="invited",
            )
            await session.commit()
            return tenant.id

    @pytest.mark.asyncio
    async def test_it_returns_the_owner_subject(self, db):
        await self._seed(db)
        app = await self._app(db)
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as c:
            resp = await c.get("/api/internal/tenants/by-realm/acme/owners")
        assert resp.status_code == 200
        body = resp.json()
        assert body["keycloak_realm"] == "acme"
        assert [o["keycloak_user_id"] for o in body["owners"]] == [
            "kc-owner-subject"
        ]

    @pytest.mark.asyncio
    async def test_an_owner_without_a_subject_is_omitted(self, db):
        """A grant keyed on an email is a guess, not an authorization.

        Better for CC to report the tenant unadministered than to seed
        an identity it cannot authenticate.
        """
        await self._seed(db, subject=None)
        app = await self._app(db)
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as c:
            resp = await c.get("/api/internal/tenants/by-realm/acme/owners")
        assert resp.status_code == 200
        assert resp.json()["owners"] == []

    @pytest.mark.asyncio
    async def test_an_unknown_realm_is_404(self, db):
        app = await self._app(db)
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as c:
            resp = await c.get("/api/internal/tenants/by-realm/nope/owners")
        assert resp.status_code == 404


class TestPrincipalAuthorityByRealm:
    """A30.23: the CURRENT identity facts Central Command re-derives a
    wave approver's permission basis from at dispatch.

    Identity FACTS only -- found, enabled, effective realm roles. No
    scope, no ledger, no interpretation: turning roles into permissions
    is CC's `auth.role_basis` and deciding what they authorize is CC's
    resolver. An absent subject is a 200 fact, a Keycloak failure a 502,
    so the caller can tell "gone" from "could not ask".
    """

    async def _app(self, sessionmaker, keycloak):
        from fastapi import FastAPI

        from harkeniq_console.api.deps import get_session
        from harkeniq_console.api.internal import router

        app = FastAPI()
        app.state.console = AppState(config=ConsoleConfig(insecure=True))
        app.state.console.keycloak_admin = keycloak

        async def _session():
            async with sessionmaker() as s:
                yield s

        app.dependency_overrides[get_session] = _session
        app.include_router(router)
        return app

    async def _tenant(self, sessionmaker, realm="acme"):
        from harkeniq_console.db.repos import TenantRepo

        async with sessionmaker() as session:
            repo = TenantRepo(session)
            tenant = await repo.create(
                name="Acme", slug=realm, billing_country="US", currency="USD",
            )
            await repo.update(tenant, keycloak_realm=realm)
            await session.commit()

    async def _keycloak(self, realm="acme"):
        from harkeniq_console.keycloak_admin import MockKeycloakAdminClient

        kc = MockKeycloakAdminClient()
        await kc.create_realm(realm)
        return kc

    @staticmethod
    def _url(subject, realm="acme"):
        return f"/api/internal/tenants/by-realm/{realm}/principals/{subject}/authority"

    @pytest.mark.asyncio
    async def test_it_reports_found_enabled_and_effective_realm_roles(self, db):
        await self._tenant(db)
        kc = await self._keycloak()
        uid = await kc.create_user("acme", "op@acme.com")
        await kc.assign_realm_role("acme", uid, "operator")
        await kc.assign_realm_role("acme", uid, "auditor")
        app = await self._app(db, kc)
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as c:
            resp = await c.get(self._url(uid))
        assert resp.status_code == 200
        assert resp.json() == {
            "realm": "acme", "subject": uid, "found": True, "enabled": True,
            "realm_roles": ["auditor", "operator"],
        }

    @pytest.mark.asyncio
    async def test_a_demoted_user_reports_the_roles_they_hold_now(self, db):
        await self._tenant(db)
        kc = await self._keycloak()
        uid = await kc.create_user("acme", "op@acme.com")
        await kc.assign_realm_role("acme", uid, "operator")
        kc._role_mappings[("acme", uid)].remove("operator")  # the admin console
        app = await self._app(db, kc)
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as c:
            resp = await c.get(self._url(uid))
        assert resp.json()["found"] is True
        assert resp.json()["realm_roles"] == []

    @pytest.mark.asyncio
    async def test_a_disabled_user_is_found_and_disabled(self, db):
        await self._tenant(db)
        kc = await self._keycloak()
        uid = await kc.create_user("acme", "op@acme.com")
        kc._users["acme"][uid]["enabled"] = False
        app = await self._app(db, kc)
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as c:
            resp = await c.get(self._url(uid))
        assert resp.status_code == 200
        assert resp.json()["found"] is True and resp.json()["enabled"] is False

    @pytest.mark.asyncio
    async def test_an_absent_subject_is_a_fact_not_an_error(self, db):
        await self._tenant(db)
        app = await self._app(db, await self._keycloak())
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as c:
            resp = await c.get(self._url("nobody"))
        assert resp.status_code == 200
        assert resp.json() == {
            "realm": "acme", "subject": "nobody", "found": False,
            "enabled": False, "realm_roles": [],
        }

    @pytest.mark.asyncio
    async def test_a_realm_that_is_not_a_tenants_is_404(self, db):
        """Tenant mismatch: a subject from another realm is never asked
        about under a name the Console does not hold."""
        await self._tenant(db)
        kc = await self._keycloak()
        await kc.create_realm("other")
        uid = await kc.create_user("other", "op@other.com")
        await kc.assign_realm_role("other", uid, "tenant_owner")
        app = await self._app(db, kc)
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as c:
            resp = await c.get(self._url(uid, realm="other"))
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_no_keycloak_admin_is_503(self, db):
        await self._tenant(db)
        app = await self._app(db, None)
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as c:
            resp = await c.get(self._url("anyone"))
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_a_keycloak_failure_is_502_not_a_fact(self, db):
        from harkeniq_console.keycloak_admin import KeycloakError

        class Broken:
            async def get_user_authority(self, realm, user_id):
                raise KeycloakError("admin authentication failed", status_code=401)

        await self._tenant(db)
        app = await self._app(db, Broken())
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as c:
            resp = await c.get(self._url("anyone"))
        assert resp.status_code == 502
        assert "keycloak" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_the_shared_key_guards_it_like_every_internal_read(self, db):
        from fastapi import FastAPI

        from harkeniq_console.api.deps import get_session
        from harkeniq_console.api.internal import router

        await self._tenant(db)
        app = FastAPI()
        app.state.console = AppState(
            config=ConsoleConfig(insecure=False, internal_api_key=KEY),
        )
        app.state.console.keycloak_admin = await self._keycloak()

        async def _session():
            async with db() as s:
                yield s

        app.dependency_overrides[get_session] = _session
        app.include_router(router)
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test",
        ) as c:
            assert (await c.get(self._url("x"))).status_code == 401
            assert (await c.get(
                self._url("x"), headers={"Authorization": f"Bearer {KEY}"},
            )).status_code == 200
