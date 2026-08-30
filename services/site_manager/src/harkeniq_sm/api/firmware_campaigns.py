"""Firmware campaign API (R4-3 P19)."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from harkeniq_sm.api.deps import require_site_token
from harkeniq_sm.api.site_scope import SiteScope, resolve_site
from harkeniq_sm.db.repos import DeviceRepo, FirmwareCampaignRepo, SiteRepo
from harkeniq_sm.firmware_orchestrator import FirmwareOrchestrator

router = APIRouter(
    prefix="/api/firmware-campaigns", dependencies=[Depends(require_site_token)]
)


def _campaign_dict(c) -> dict:
    return {
        "id": c.id,
        "site_id": c.site_id,
        "component": c.component,
        "vendor": c.vendor,
        "target_version": c.target_version,
        "image_uri": c.image_uri,
        "status": c.status,
        "current_wave": c.current_wave,
        "wave_count": c.wave_count,
        "max_wave_size": c.max_wave_size,
        "created_by": c.created_by,
        "approved_by": c.approved_by,
        "halt_reason": c.halt_reason,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "completed_at": c.completed_at.isoformat() if c.completed_at else None,
    }


def _orchestrator(state) -> FirmwareOrchestrator:
    return FirmwareOrchestrator(
        state.sessionmaker, updater=getattr(state, "firmware_updater", None)
    )


@router.post("")
async def create_campaign(request: Request, payload: dict = Body(...),
    scope: SiteScope = Depends(resolve_site),
) -> dict:
    state = request.app.state.sm
    agent_ids = payload.get("agent_ids", [])
    target_version = str(payload.get("target_version", ""))
    if not agent_ids or not target_version:
        raise HTTPException(
            status_code=400, detail="agent_ids and target_version are required"
        )
    async with state.sessionmaker() as session:
        # E1.3: a campaign targets one site's devices; the wave planner
        # must never be handed targets from another estate.
        device_repo = DeviceRepo(session)
        device_ids = []
        for agent_id in agent_ids:
            device = await device_repo.get_by_agent_id(agent_id)
            if device is None:
                raise HTTPException(
                    status_code=404, detail=f"unknown agent {agent_id}"
                )
            device_ids.append(device.id)
        site_id = scope.id
        await session.commit()
    campaign_id = await _orchestrator(state).create_campaign(
        site_id=site_id,
        device_ids=device_ids,
        component=str(payload.get("component", "bmc")),
        target_version=target_version,
        vendor=str(payload.get("vendor", "")),
        image_uri=str(payload.get("image_uri", "")),
        image_sha256=str(payload.get("image_sha256", "")),
        created_by=str(payload.get("created_by", "operator")),
        max_wave_size=int(payload.get("max_wave_size", 5)),
    )
    async with state.sessionmaker() as session:
        campaign = await FirmwareCampaignRepo(session).get(campaign_id)
        return _campaign_dict(campaign)


@router.get("")
async def list_campaigns(request: Request,
    scope: SiteScope = Depends(resolve_site),
) -> dict:
    state = request.app.state.sm
    async with state.sessionmaker() as session:
        # E1.3: a campaign targets one site's devices; the wave planner
        # must never be handed targets from another estate.
        campaigns = await FirmwareCampaignRepo(session).list_for_site(scope.id)
        return {"campaigns": [_campaign_dict(c) for c in campaigns]}


@router.get("/{campaign_id}")
async def get_campaign(campaign_id: str, request: Request) -> dict:
    state = request.app.state.sm
    async with state.sessionmaker() as session:
        repo = FirmwareCampaignRepo(session)
        campaign = await repo.get(campaign_id)
        if campaign is None:
            raise HTTPException(status_code=404, detail="campaign not found")
        targets = await repo.targets(campaign_id)
        device_repo = DeviceRepo(session)
        target_dicts = []
        for t in targets:
            device = await device_repo.get(t.device_id)
            target_dicts.append({
                "device_id": t.device_id,
                "agent_id": device.agent_id if device else "",
                "wave_index": t.wave_index,
                "status": t.status,
                "pre_version": t.pre_version,
                "post_version": t.post_version,
                "error": t.error,
            })
        data = _campaign_dict(campaign)
        data["targets"] = target_dicts
        return data


@router.post("/{campaign_id}/approve")
async def approve_campaign(
    campaign_id: str, request: Request, payload: dict = Body(default={})
) -> dict:
    state = request.app.state.sm
    try:
        await _orchestrator(state).approve(
            campaign_id, actor=str(payload.get("actor", "operator"))
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    async with state.sessionmaker() as session:
        campaign = await FirmwareCampaignRepo(session).get(campaign_id)
        return _campaign_dict(campaign)


@router.post("/{campaign_id}/advance")
async def advance_campaign(campaign_id: str, request: Request) -> dict:
    state = request.app.state.sm
    orchestrator = _orchestrator(state)
    if orchestrator.updater is None:
        raise HTTPException(
            status_code=409,
            detail="no firmware update transport configured on this SM",
        )
    try:
        return await orchestrator.advance(campaign_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
