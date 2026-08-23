"""Incident endpoints: parent cards with children, inferred labeling (R-S4/R-S5)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from harkeniq_sm.api.deps import require_site_token
from harkeniq_sm.db.repos import DeviceRepo, IncidentRepo

router = APIRouter(
    prefix="/api/incidents", dependencies=[Depends(require_site_token)]
)


async def _incident_dict(incident, device_repo) -> dict:
    agent_id = None
    if incident.device_id:
        device = await device_repo.get(incident.device_id)
        agent_id = device.agent_id if device else None
    return {
        "id": incident.id,
        "kind": incident.kind,
        "status": incident.status,
        "parent_id": incident.parent_id,
        "agent_id": agent_id,
        "domain_id": incident.domain_id,
        "subsystem": incident.subsystem,
        "confidence": incident.confidence,
        "inferred": incident.inferred,
        "title": incident.title,
        "correlation_meta": incident.correlation_meta,
        "explanation": incident.explanation,
        "opened_at": incident.opened_at.isoformat(),
        "resolved_at": (
            incident.resolved_at.isoformat() if incident.resolved_at else None
        ),
    }


@router.get("")
async def list_incidents(request: Request, status: str = "open") -> list[dict]:
    """Top-level incidents with nested children; ``status=all`` includes resolved."""
    state = request.app.state.sm
    async with state.sessionmaker() as session:
        repo = IncidentRepo(session)
        device_repo = DeviceRepo(session)
        incidents = (
            await repo.list_all() if status == "all" else await repo.list_open()
        )
        by_id = {i.id: i for i in incidents}
        cards = []
        children: dict[str, list] = {}
        for incident in incidents:
            entry = await _incident_dict(incident, device_repo)
            if incident.parent_id and incident.parent_id in by_id:
                children.setdefault(incident.parent_id, []).append(entry)
            else:
                entry["children"] = []
                cards.append(entry)
        for card in cards:
            card["children"] = children.get(card["id"], [])
    return cards


@router.get("/{incident_id}")
async def get_incident(incident_id: str, request: Request) -> dict:
    state = request.app.state.sm
    async with state.sessionmaker() as session:
        repo = IncidentRepo(session)
        incident = await repo.get(incident_id)
        if incident is None:
            raise HTTPException(status_code=404, detail="unknown incident")
        device_repo = DeviceRepo(session)
        result = await _incident_dict(incident, device_repo)
        result["children"] = [
            await _incident_dict(child, device_repo)
            for child in await repo.children(incident.id)
        ]
    return result
