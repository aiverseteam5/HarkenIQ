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
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response

from harkeniq_console.api import me as me_api
from harkeniq_console.auth import UserContext, get_current_user
from harkeniq_console.api import admin as admin_api
from harkeniq_console.api import audit as audit_api
from harkeniq_console.api import billing as billing_api
from harkeniq_console.api import internal as internal_api
from harkeniq_console.api import invoices as invoices_api
from harkeniq_console.api import licenses as licenses_api
from harkeniq_console.api import marketplace as marketplace_api
from harkeniq_console.api import support as support_api
from harkeniq_console.api import tenant_services as tenant_services_api
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
    app.include_router(tenant_services_api.router)
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
    app.include_router(me_api.router)
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
    # The tenant is in the PATH (/api/t/{tenant_id}/fleet/...), not in a
    # header and not in browser storage. One global cc_url served every
    # tenant the same Central Command, so a per-tenant URL would have been
    # a promise the backend did not keep. Placement now resolves through
    # the tenant_services registry, and resolution is fail-closed: a tenant
    # with no active placement is refused, never quietly handed a shared
    # endpoint. Requests are authorized BEFORE proxying, so the tenant
    # scope and support-access gate cover infrastructure too.
    import httpx

    from harkeniq_console.api.deps import get_session, tenant_scope
    from harkeniq_console.db.repos import TenantServiceRepo

    cc_client = httpx.AsyncClient(timeout=30.0)

    async def _proxy_cc(
        request: Request, tenant_id: str, rest: str, prefix: str,
        session,
    ) -> Response:
        placement = await TenantServiceRepo(session).resolve(
            tenant_id, "central_command",
        )

        if placement is None:
            return JSONResponse(
                status_code=503,
                content={
                    "detail": (
                        "no central_command registered for this tenant — "
                        "register a service placement before using this page"
                    ),
                },
            )

        base = placement.endpoint_url.rstrip("/")
        # rest="" targets the collection root: CC's canonical form is
        # the trailing slash (returning its 307 verbatim would send
        # the browser into a redirect loop between the two origins).
        suffix = f"/api/{prefix}/{rest}" if rest else f"/api/{prefix}/"
        upstream = await cc_client.request(
            request.method,
            f"{base}{suffix}",
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
            # tenant_scope as a real dependency, not a hand-rolled call:
            # FastAPI resolves tenant_id from the path and the session for
            # it, so the membership check and the PR #8 support-access gate
            # both run before a single byte reaches the tenant's stack.
            async def handler(
                request: Request,
                tenant_id: str,
                rest: str = "",
                _user: UserContext = Depends(tenant_scope),
                session=Depends(get_session),
            ) -> Response:
                return await _proxy_cc(request, tenant_id, rest, prefix, session)
            return handler

        methods = ["GET", "POST", "PATCH", "PUT", "DELETE"]
        app.add_api_route(
            f"/api/t/{{tenant_id}}/{_prefix}", _make(_prefix), methods=methods,
            include_in_schema=False,
        )
        app.add_api_route(
            f"/api/t/{{tenant_id}}/{_prefix}/{{rest:path}}", _make(_prefix),
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
            # An explicit choice wins. tenant_scope still authorizes
            # downstream, so a tenant user naming someone else's tenant gets
            # 403 rather than access — the header selects, it never grants.
            header = request.headers.get("x-harken-tenant", "").strip()
            claim = ""
            if not header:
                # request.state.user is never populated by anything, so the
                # original claim branch here was dead: every request fell
                # through to the sole-tenant lookup, which happened to be
                # right only because the demo has exactly one tenant.
                try:
                    claim = (await get_current_user(request)).tenant_id or ""
                except Exception:
                    claim = ""
            if header:
                resolved = header
            elif claim:
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
            else:
                # Unresolved "current" used to fall through to the routes,
                # where behaviour split: /usage/estimate 404'd because it
                # validates the tenant, while /audit filtered on the literal
                # string "current" and returned 200 with an empty list — a
                # platform admin reads that as "no audit entries", not "no
                # tenant selected". Refuse once, here, so no route can serve
                # phantom-tenant data.
                return JSONResponse(
                    status_code=400,
                    content={
                        "detail": "tenant not resolved: select a tenant "
                                  "(send X-Harken-Tenant)",
                    },
                )
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
