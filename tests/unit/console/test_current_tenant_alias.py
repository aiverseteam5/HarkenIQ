"""The implicit-tenant paths are gone, and must stay gone.

`/api/tenants/current` resolved a tenant from an X-Harken-Tenant header, a
token claim, or "the only tenant in the table" — three implicit sources,
none of them visible in the URL. Tenant context is a path segment now, so
these assert the absence rather than the behaviour: nothing should be able
to acquire a tenant without saying which one.
"""

import httpx
import pytest

from harkeniq_console.app import create_app
from harkeniq_console.auth import UserContext, get_current_user
from harkeniq_console.config import ConsoleConfig
from harkeniq_console.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_console.db.models import Tenant
from harkeniq_console.runtime import AppState


async def _client(tenants: int = 1):
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

    state = AppState(
        config=ConsoleConfig(insecure=False), engine=engine, sessionmaker=sm,
    )
    app = create_app(state)

    async def _fake_user() -> UserContext:
        return UserContext(
            user_id="kc-1", email="admin@example.com", tenant_id=None,
            role="platform_super_admin", permissions=[], is_platform_user=True,
        )

    app.dependency_overrides[get_current_user] = _fake_user
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
        follow_redirects=True,
    )
    return client, engine, ids


async def test_current_is_no_longer_a_tenant():
    """"current" is now just an unknown tenant id, not a resolvable alias.

    Critically it must not resolve to the sole tenant: that was convenient
    with one tenant and silently wrong with two.
    """
    client, engine, _ids = await _client(tenants=1)
    try:
        resp = await client.get("/api/tenants/current/audit/")
        # Pinned to the SPECIFIC refusal: 'current' is an unknown tenant id
        # and tenant_scope answers 404. A bare '!= 200' also passed on a
        # 500 crash (testing-pass finding).
        assert resp.status_code == 404
    finally:
        await client.aclose()
        await engine.dispose()


async def test_tenant_header_no_longer_selects_anything():
    """X-Harken-Tenant is not consulted; the path is the only source."""
    client, engine, ids = await _client(tenants=2)
    try:
        resp = await client.get(
            "/api/tenants/current/audit/",
            headers={"x-harken-tenant": ids[1]},
        )
        assert resp.status_code == 404
    finally:
        await client.aclose()
        await engine.dispose()


async def test_explicit_tenant_id_still_works():
    client, engine, ids = await _client(tenants=2)
    try:
        resp = await client.get(f"/api/tenants/{ids[0]}/audit/")
        assert resp.status_code == 200
    finally:
        await client.aclose()
        await engine.dispose()
