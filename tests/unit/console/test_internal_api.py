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
