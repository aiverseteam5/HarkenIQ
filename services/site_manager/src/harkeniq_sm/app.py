"""FastAPI application factory.

Routers land with the API phase; the health endpoint and coverage map
are here from the start so the runtime TaskGroup is complete.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles

from harkeniq_sm.api import actions as actions_api
from harkeniq_sm.api import audit as audit_api
from harkeniq_sm.api import devices as devices_api
from harkeniq_sm.api import domains as domains_api
from harkeniq_sm.api import incidents as incidents_api
from harkeniq_sm.api import site as site_api
from harkeniq_sm.api.deps import require_site_token
from harkeniq_sm.coverage import coverage_entry
from harkeniq_sm.db.repos import DeviceRepo, SiteRepo, StatusRepo


def create_app(state) -> FastAPI:
    app = FastAPI(title="HarkenIQ Site Manager", version="0.1.0")
    app.state.sm = state
    app.include_router(site_api.router)
    app.include_router(domains_api.router)
    app.include_router(devices_api.router)
    app.include_router(incidents_api.router)
    app.include_router(actions_api.router)
    app.include_router(audit_api.router)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok", "site": state.config.site_name}

    @app.get("/api/coverage", dependencies=[Depends(require_site_token)])
    async def coverage() -> list[dict]:
        async with state.sessionmaker() as session:
            site = await SiteRepo(session).get_or_create(state.config.site_name)
            devices = await DeviceRepo(session).list_for_site(site.id)
            status_repo = StatusRepo(session)
            entries = []
            for device in devices:
                status = await status_repo.get(device.id)
                entries.append(coverage_entry(device, status, state.config))
            await session.commit()
        return entries

    # Dashboard build (ui/dist) — mounted last so API routes win. Absent in
    # source checkouts that haven't run `npm run build`; API stays usable.
    if state.config.ui_dist:
        dist = Path(state.config.ui_dist)
    else:
        dist = Path(__file__).resolve().parents[2] / "ui" / "dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="ui")

    return app
