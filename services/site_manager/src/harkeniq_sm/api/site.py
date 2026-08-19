"""Site-model YAML round-trip endpoints (A1.1)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse

from harkeniq_sm.api.deps import require_site_token
from harkeniq_sm.sitemodel.yaml_io import SiteYaml

router = APIRouter(
    prefix="/api/site", dependencies=[Depends(require_site_token)]
)


@router.get("/yaml", response_class=PlainTextResponse)
async def export_yaml(request: Request) -> str:
    state = request.app.state.sm
    return await SiteYaml(state.sessionmaker, state.config).export()


@router.put("/yaml")
async def import_yaml(request: Request, actor: str = "operator") -> dict:
    state = request.app.state.sm
    text = (await request.body()).decode()
    counters = await SiteYaml(state.sessionmaker, state.config).import_yaml(
        text, actor=actor
    )
    return {"imported": counters}
