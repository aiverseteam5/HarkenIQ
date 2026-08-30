"""FastAPI application factory.

Routers land with the API phase; the health endpoint is here from the
start so the runtime TaskGroup is complete.
"""

from __future__ import annotations

from fastapi import FastAPI

from harkeniq_cc.api import agents as agents_api
from harkeniq_cc.api import approvals as approvals_api
from harkeniq_cc.api import audit as audit_api
from harkeniq_cc.api import firmware as firmware_api
from harkeniq_cc.api import fleet as fleet_api
from harkeniq_cc.api import learning as learning_api
from harkeniq_cc.api import outcomes as outcomes_api
from harkeniq_cc.api import operational_agents as operational_agents_api
from harkeniq_cc.api import policies as policies_api
from harkeniq_cc.api import attention as attention_api
from harkeniq_cc.api import autonomy as autonomy_api
from harkeniq_cc.api import incidents as incidents_api
from harkeniq_cc.api import predictive as predictive_api
from harkeniq_cc.api import sites as sites_api
from harkeniq_cc.api import warranty as warranty_api


def create_app(state) -> FastAPI:
    app = FastAPI(title="HarkenIQ Central Command", version="0.1.0")
    app.state.cc = state

    # E0.3: /metrics from the registry that shipped with R4-0 and had no
    # callers. Mounted before the routers so the middleware sees every
    # request, including the ones the routers reject.
    from harkeniq.metrics import mount_metrics

    mount_metrics(app, "central-command")

    # QA-026: X-Request-Id propagation (R4-0 P3, finally wired) so a
    # partner incident can be traced across service logs.
    from harkeniq.logging_config import request_id_middleware

    app.add_middleware(request_id_middleware(app))

    # QA-005: configure_auth existed since R2b and was called by nothing —
    # secure mode could only ever answer "auth not configured".
    from harkeniq_cc.auth import configure_auth

    configure_auth(
        keycloak_url=state.config.keycloak_url,
        realm=state.config.keycloak_realm or "harkeniq-platform",
        client_id=state.config.keycloak_client_id,
        insecure=state.config.insecure,
        keycloak_public_url=getattr(state.config, "keycloak_public_url", ""),
    )

    app.include_router(fleet_api.router)
    app.include_router(approvals_api.router)
    app.include_router(audit_api.router)
    app.include_router(sites_api.router)
    app.include_router(agents_api.router)
    app.include_router(policies_api.router)
    app.include_router(outcomes_api.router)
    app.include_router(firmware_api.router)
    app.include_router(warranty_api.router)
    app.include_router(predictive_api.router)
    app.include_router(attention_api.router)
    app.include_router(incidents_api.router)
    app.include_router(learning_api.router)
    app.include_router(autonomy_api.router)
    app.include_router(operational_agents_api.router)

    # QA-010: a hardcoded ok reported healthy while the database had no
    # schema. Real probe via the R4-0 HealthChecker (same pattern as SM).
    from harkeniq.metrics import HealthChecker

    checker = HealthChecker("central-command")

    async def _db_probe() -> bool:
        from sqlalchemy import text

        async with state.sessionmaker() as session:
            await session.execute(text("SELECT 1 FROM cc_sites LIMIT 1"))
        return True

    checker.add_probe("database", _db_probe)

    @app.get("/healthz")
    async def healthz():
        from fastapi.responses import JSONResponse

        status = await checker.check()
        payload = status.to_dict()
        payload["tenant"] = state.config.tenant_id
        payload["status"] = "ok" if status.healthy else "degraded"
        return JSONResponse(payload, status_code=200 if status.healthy else 503)

    return app
