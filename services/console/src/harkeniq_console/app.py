"""FastAPI application factory.

Routers land with the API phase; the health endpoint is here from the
start so the runtime TaskGroup is complete.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from harkeniq_console.api import admin as admin_api
from harkeniq_console.api import audit as audit_api
from harkeniq_console.api import billing as billing_api
from harkeniq_console.api import internal as internal_api
from harkeniq_console.api import invoices as invoices_api
from harkeniq_console.api import licenses as licenses_api
from harkeniq_console.api import marketplace as marketplace_api
from harkeniq_console.api import support as support_api
from harkeniq_console.api import tenants as tenants_api
from harkeniq_console.api import users as users_api
from harkeniq_console.api import apikeys as apikeys_api
from harkeniq_console.api import webhooks as webhooks_api


def create_app(state) -> FastAPI:
    app = FastAPI(title="HarkenIQ Console", version="0.1.0")
    app.state.console = state
    app.include_router(tenants_api.router)
    app.include_router(users_api.router)
    app.include_router(users_api.roles_router)
    app.include_router(licenses_api.router)
    app.include_router(licenses_api.validate_router)
    app.include_router(billing_api.router)
    app.include_router(invoices_api.router)
    app.include_router(invoices_api.admin_router)
    app.include_router(support_api.router)
    app.include_router(support_api.admin_router)
    app.include_router(support_api.support_mode_router)
    app.include_router(audit_api.router)
    app.include_router(audit_api.admin_router)
    app.include_router(admin_api.router)
    app.include_router(internal_api.router)
    app.include_router(webhooks_api.router)
    app.include_router(invoices_api.payments_router)
    app.include_router(apikeys_api.router)
    app.include_router(apikeys_api.impersonation_router)
    app.include_router(marketplace_api.router)
    app.include_router(marketplace_api.admin_router)

    # QA-010: a hardcoded ok reported healthy while the database had no
    # schema. Real probe via the R4-0 HealthChecker (same pattern as SM).
    from harkeniq.metrics import HealthChecker

    checker = HealthChecker("console")

    async def _db_probe() -> bool:
        from sqlalchemy import text

        async with state.sessionmaker() as session:
            await session.execute(text("SELECT 1 FROM tenants LIMIT 1"))
        return True

    checker.add_probe("database", _db_probe)

    @app.get("/healthz")
    async def healthz():
        from fastapi.responses import JSONResponse

        status = await checker.check()
        payload = status.to_dict()
        payload["service"] = "console"
        payload["status"] = "ok" if status.healthy else "degraded"
        return JSONResponse(payload, status_code=200 if status.healthy else 503)

    # QA-029: the SPA calls L3 (Central Command) surfaces against its own
    # origin; without this proxy half the Console screens 404 in every
    # shipped configuration. The bearer token is forwarded — CC validates
    # the same Keycloak-issued token (QA-005). Prefixes are CC-owned and
    # collision-free with Console routes.
    _CC_PREFIXES = (
        "fleet", "approvals", "agents", "policies", "outcomes",
        "predictive", "warranty", "firmware", "sites", "audit",
    )
    if state.config.cc_url:
        import httpx
        from fastapi import Request
        from fastapi.responses import Response

        cc_base = state.config.cc_url.rstrip("/")
        cc_client = httpx.AsyncClient(base_url=cc_base, timeout=30.0)

        async def _proxy_cc(request: Request, rest: str, prefix: str) -> Response:
            upstream = await cc_client.request(
                request.method,
                f"/api/{prefix}/{rest}" if rest else f"/api/{prefix}",
                params=request.query_params,
                content=await request.body(),
                headers={
                    key: value
                    for key, value in request.headers.items()
                    if key.lower() in ("authorization", "content-type")
                },
            )
            return Response(
                content=upstream.content,
                status_code=upstream.status_code,
                media_type=upstream.headers.get("content-type"),
            )

        for _prefix in _CC_PREFIXES:
            def _make(prefix: str):
                async def handler(request: Request, rest: str = "") -> Response:
                    return await _proxy_cc(request, rest, prefix)
                return handler

            methods = ["GET", "POST", "PATCH", "PUT", "DELETE"]
            app.add_api_route(
                f"/api/{_prefix}", _make(_prefix), methods=methods,
                include_in_schema=False,
            )
            app.add_api_route(
                f"/api/{_prefix}/{{rest:path}}", _make(_prefix),
                methods=methods, include_in_schema=False,
            )

    # Dashboard build (ui/dist) — mounted last so API routes win.
    if state.config.ui_dist:
        dist = Path(state.config.ui_dist)
    else:
        dist = Path(__file__).resolve().parents[2] / "ui" / "dist"
    if dist.is_dir():
        from fastapi.staticfiles import StaticFiles
        app.mount("/", StaticFiles(directory=dist, html=True), name="ui")

    return app
