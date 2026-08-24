"""FastAPI application factory.

Routers land with the API phase; the health endpoint is here from the
start so the runtime TaskGroup is complete.
"""

from __future__ import annotations

from fastapi import FastAPI

from harkeniq_cc.api import agents as agents_api
from harkeniq_cc.api import approvals as approvals_api
from harkeniq_cc.api import audit as audit_api
from harkeniq_cc.api import fleet as fleet_api
from harkeniq_cc.api import outcomes as outcomes_api
from harkeniq_cc.api import policies as policies_api
from harkeniq_cc.api import sites as sites_api


def create_app(state) -> FastAPI:
    app = FastAPI(title="HarkenIQ Central Command", version="0.1.0")
    app.state.cc = state

    app.include_router(fleet_api.router)
    app.include_router(approvals_api.router)
    app.include_router(audit_api.router)
    app.include_router(sites_api.router)
    app.include_router(agents_api.router)
    app.include_router(policies_api.router)
    app.include_router(outcomes_api.router)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok", "tenant": state.config.tenant_id}

    return app
