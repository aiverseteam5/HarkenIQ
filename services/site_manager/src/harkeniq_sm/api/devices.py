"""Device inventory endpoints (dashboard Devices page)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from harkeniq_sm.api.deps import require_site_token
from harkeniq_sm.api.site_scope import SiteScope, resolve_site
from harkeniq_sm.coverage import coverage_entry
from harkeniq_sm.db.repos import (
    DeviceRepo,
    SiteRepo,
    StatusRepo,
    SubsystemStateRepo,
    TelemetryRepo,
)

router = APIRouter(
    prefix="/api/devices", dependencies=[Depends(require_site_token)]
)


def _device_dict(device, entry: dict) -> dict:
    return {
        **entry,
        "vendor": device.vendor,
        "model": device.model,
        "service_tag": device.service_tag,
        "rack_suggestion": device.rack_suggestion,
        "first_seen_at": device.first_seen_at.isoformat(),
    }


@router.get("")
async def list_devices(
    request: Request, scope: SiteScope = Depends(resolve_site)
) -> list[dict]:
    """Devices at ONE site. E1.3: never every site this process serves."""
    state = request.app.state.sm
    async with state.sessionmaker() as session:
        status_repo = StatusRepo(session)
        result = []
        for device in await DeviceRepo(session).list_for_site(scope.id):
            status = await status_repo.get(device.id)
            result.append(_device_dict(device, coverage_entry(device, status, state.config)))
        await session.commit()
    return result


@router.get("/{agent_id}")
async def get_device(
    agent_id: str, request: Request,
    scope: SiteScope = Depends(resolve_site),
) -> dict:
    state = request.app.state.sm
    async with state.sessionmaker() as session:
        device = await DeviceRepo(session).get_by_agent_id(agent_id)
        if device is None:
            raise HTTPException(status_code=404, detail="unknown device")
        # E1.3: a device at another site this Site Manager serves reads
        # as absent, not as forbidden -- 403 would confirm it exists.
        if device.site_id != scope.id:
            raise HTTPException(status_code=404, detail="unknown device")
        status = await StatusRepo(session).get(device.id)
        result = _device_dict(device, coverage_entry(device, status, state.config))
        result["subsystems"] = {
            s.subsystem: s.severity
            for s in await SubsystemStateRepo(session).non_ok()
            if s.device_id == device.id
        }
        result["recent_verdicts"] = [
            {
                "time": v.time.isoformat(),
                "sensor_id": v.sensor_id,
                "skill_name": v.skill_name,
                "severity": v.severity,
                "message": v.message,
            }
            for v in await TelemetryRepo(session).recent_verdicts(device.id, limit=20)
        ]
    return result
