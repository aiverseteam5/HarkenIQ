"""Regression: ISSUE-006 — tenantId "current" was an unimplemented alias.

Found by /qa on 2026-08-26 (report: .gstack/qa-reports/qa-report-localhost-2026-08-26.md).
Seven Console SPA pages hardcode tenantId="current"; the backend routes
bind {tenant_id} literally, so Audit Logs, Billing, Usage, Reports,
Support, and API Keys 404'd — and /usage/estimate 500'd on the
unhandled ValueError — for every operator since R2b. The middleware now
resolves "current" to the caller's tenant claim, else to the sole
tenant when exactly one exists, else an explicit X-Harken-Tenant
selection; genuinely unresolved requests are refused with 400.
"""

import httpx
import pytest

from harkeniq_console.app import create_app
from harkeniq_console.config import ConsoleConfig
from harkeniq_console.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_console.db.models import Tenant
from harkeniq_console.runtime import AppState


async def _make_client(db_seed_tenants):
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sm = make_sessionmaker(engine)
    async with sm() as session:
        for i in range(db_seed_tenants):
            session.add(Tenant(name=f"T{i}", slug=f"t{i}", billing_country="US"))
        await session.commit()
    config = ConsoleConfig(insecure=True)
    state = AppState(config=config, engine=engine, sessionmaker=sm)
    app = create_app(state)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
        follow_redirects=True,
    )
    return client, engine


async def test_current_resolves_to_sole_tenant():
    client, engine = await _make_client(db_seed_tenants=1)
    try:
        resp = await client.get("/api/tenants/current/audit")
        assert resp.status_code == 200
        assert "items" in resp.json()

        # ISSUE-007: the exact shape the SPA sends — collection root,
        # NO trailing slash, no redirect available (SPA mount pre-empts
        # it in production). Must answer directly.
        resp = await httpx.AsyncClient(
            transport=client._transport, base_url="http://test",
            follow_redirects=False,
        ).get("/api/tenants/current/audit")
        assert resp.status_code == 200

        # The 500 path: usage/estimate on the resolved tenant answers.
        resp = await client.get("/api/tenants/current/usage/estimate")
        assert resp.status_code == 200
    finally:
        await client.aclose()
        await engine.dispose()


async def test_current_ambiguous_with_two_tenants_is_404_not_500():
    client, engine = await _make_client(db_seed_tenants=2)
    try:
        resp = await client.get("/api/tenants/current/usage/estimate")
        # Was 404, which this route produced only because it happens to
        # validate the tenant; /audit answered the same unresolved state
        # with 200 and an empty list. The middleware now refuses once,
        # uniformly, so no route serves phantom-tenant data.
        assert resp.status_code == 400  # honest, never a crash
        assert "select a tenant" in resp.json()["detail"]
    finally:
        await client.aclose()
        await engine.dispose()


async def test_explicit_tenant_id_untouched():
    client, engine = await _make_client(db_seed_tenants=1)
    try:
        resp = await client.get("/api/tenants/nonexistent/usage/estimate")
        assert resp.status_code == 404
    finally:
        await client.aclose()
        await engine.dispose()
