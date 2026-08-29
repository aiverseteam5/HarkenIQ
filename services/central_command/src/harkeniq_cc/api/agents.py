"""Agents API: fleet-wide agent listing and control placeholders."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_cc.api.deps import get_session, require_permission
from harkeniq_cc.auth import UserContext
from harkeniq_cc.db.repos import FleetCacheRepo

logger = logging.getLogger("harkeniq.cc.api.agents")

router = APIRouter(prefix="/api/agents", tags=["agents"])


def _agent_dict(dev) -> dict:
    return {
        "agent_id": dev.agent_id,
        "agent_name": dev.agent_name,
        "vendor": dev.vendor,
        "model": dev.model,
        "observation": dev.observation,
        "health": dev.health,
        "site_id": dev.site_id,
        "snapshot_at": dev.snapshot_at.isoformat() if dev.snapshot_at else None,
    }


@router.get(
    "/",
    dependencies=[Depends(require_permission("fleet.view"))],
)
async def list_agents(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    site_id: str | None = None,
    search: str | None = None,
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """List agents from fleet cache."""
    devices, total = await FleetCacheRepo(session).list_filtered(
        tenant_id=user.tenant_id,
        site_id=site_id,
        search=search,
        page=page,
        page_size=page_size,
    )
    return {
        "agents": [_agent_dict(d) for d in devices],
        "page": page,
        "page_size": page_size,
        "total": total,
        "tenant_id": user.tenant_id,
    }


@router.get(
    "/{agent_id}",
    dependencies=[Depends(require_permission("fleet.view"))],
)
async def get_agent(
    agent_id: str,
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Agent detail."""
    dev = await FleetCacheRepo(session).get_by_agent_id(agent_id)
    if dev is None:
        raise HTTPException(status_code=404, detail="agent not found")
    # Verify tenant ownership via site
    from harkeniq_cc.db.models import CCSite

    site = await session.get(CCSite, dev.site_id)
    if site is None or site.tenant_id != user.tenant_id:
        raise HTTPException(status_code=404, detail="agent not found")
    result = _agent_dict(dev)
    result["site_name"] = site.site_name
    result["subsystems"] = dev.subsystems
    return result


# P0 2026-08-29 (final assessment §7): the enable/disable endpoints were
# PLACEBOS — they wrote an audit row, returned "acknowledged", and changed
# nothing anywhere (no directive, no SM call, no agent effect). A control
# that claims success while doing nothing is worse than no control, so they
# are removed until a real disable path exists (SM directive + agent state,
# a P1+ slice). The audit-only history they wrote remains in the chain.
