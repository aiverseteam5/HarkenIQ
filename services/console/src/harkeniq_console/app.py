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

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok", "service": "console"}

    # Dashboard build (ui/dist) — mounted last so API routes win.
    if state.config.ui_dist:
        dist = Path(state.config.ui_dist)
    else:
        dist = Path(__file__).resolve().parents[2] / "ui" / "dist"
    if dist.is_dir():
        from fastapi.staticfiles import StaticFiles
        app.mount("/", StaticFiles(directory=dist, html=True), name="ui")

    return app
