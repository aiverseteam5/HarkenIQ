"""FastAPI application factory.

Routers land with the API phase; the health endpoint is here from the
start so the runtime TaskGroup is complete.
"""

from __future__ import annotations

from pathlib import Path

# Request/Response at MODULE level: `from __future__ import annotations`
# turns handler annotations into strings, and FastAPI resolves them against
# module globals — a function-local Request import made the proxy handlers
# grow a required `request` QUERY param (QA-029 live finding).
from fastapi import FastAPI, Request
from fastapi.responses import Response

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

    # QA-026: X-Request-Id propagation (R4-0 P3, finally wired) so a
    # partner incident can be traced across service logs.
    from harkeniq.logging_config import request_id_middleware

    app.add_middleware(request_id_middleware(app))
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

        cc_base = state.config.cc_url.rstrip("/")
        cc_client = httpx.AsyncClient(base_url=cc_base, timeout=30.0)

        async def _proxy_cc(request: Request, rest: str, prefix: str) -> Response:
            # rest="" targets the collection root: CC's canonical form is
            # the trailing slash (returning its 307 verbatim would send
            # the browser into a redirect loop between the two origins).
            upstream = await cc_client.request(
                request.method,
                f"/api/{prefix}/{rest}" if rest else f"/api/{prefix}/",
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

    # QA ISSUE-007: the SPA mount at "/" pre-empts Starlette's
    # trailing-slash redirect, so every collection-root API route
    # (registered at ".../"; 12 of them — audit, invoices, payments,
    # users, tenants, api keys, ...) 404'd when the UI called it without
    # the slash. Register a no-slash alias for each. No router-level
    # dependencies exist (verified), so re-adding from the endpoint
    # signature preserves auth/session dependencies.
    from fastapi.routing import APIRoute

    def _api_routes(routes):
        for route in routes:
            inner = getattr(route, "original_router", None)
            if inner is not None:  # lazily included router (FastAPI >= 0.12x)
                yield from _api_routes(inner.routes)
            elif isinstance(route, APIRoute):
                yield route

    for route in list(_api_routes(app.router.routes)):
        if route.path.endswith("/") and len(route.path) > 1:
            app.add_api_route(
                route.path.rstrip("/"),
                route.endpoint,
                methods=sorted(route.methods or []),
                include_in_schema=False,
                name=f"{route.name}_noslash",
            )

    # QA ISSUE-006: every tenant-scoped SPA page hardcodes tenantId
    # "current", an alias the backend never implemented — Audit Logs,
    # Billing, Usage, Reports, Support, and API Keys all 404'd (and
    # usage/estimate 500'd) since R2b. Resolve "current" to the caller's
    # tenant claim when present, else to the sole tenant when exactly one
    # exists (the demo and the sovereign single-tenant shape). Ambiguous
    # multi-tenant platform users keep an honest 404; tenant_scope still
    # authorizes downstream either way.
    @app.middleware("http")
    async def resolve_current_tenant(request: Request, call_next):
        path = request.scope.get("path", "")
        marker = "/api/tenants/current"
        if path == marker or path.startswith(marker + "/"):
            resolved = ""
            claim = getattr(
                getattr(request.state, "user", None), "tenant_id", ""
            )
            if claim:
                resolved = claim
            else:
                from sqlalchemy import select

                from harkeniq_console.db.models import Tenant

                async with state.sessionmaker() as session:
                    ids = (
                        await session.execute(select(Tenant.id).limit(2))
                    ).scalars().all()
                if len(ids) == 1:
                    resolved = ids[0]
            if resolved:
                new_path = path.replace(marker, f"/api/tenants/{resolved}", 1)
                request.scope["path"] = new_path
                request.scope["raw_path"] = new_path.encode()
        return await call_next(request)

    # Dashboard build (ui/dist) — mounted last so API routes win.
    if state.config.ui_dist:
        dist = Path(state.config.ui_dist)
    else:
        dist = Path(__file__).resolve().parents[2] / "ui" / "dist"
    if dist.is_dir():
        from fastapi.staticfiles import StaticFiles
        from starlette.exceptions import HTTPException as StarletteHTTPException

        class SPAStaticFiles(StaticFiles):
            """Serve index.html for client-side routes (QA ISSUE-001).

            Plain StaticFiles(html=True) resolves index.html only at "/",
            so the OIDC redirect to /callback — and any refresh on a SPA
            route — returned the API's JSON 404 and login dead-ended.
            API routes still win: this mount is last.
            """

            async def get_response(self, path, scope):
                try:
                    response = await super().get_response(path, scope)
                except StarletteHTTPException as e:
                    if e.status_code == 404 and not path.startswith("api/"):
                        return await super().get_response("index.html", scope)
                    raise
                if response.status_code == 404 and not path.startswith("api/"):
                    return await super().get_response("index.html", scope)
                return response

        app.mount("/", SPAStaticFiles(directory=dist, html=True), name="ui")

    return app
