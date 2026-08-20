"""Agents API: fleet-wide agent listing and control placeholders."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_cc.api.deps import get_session, require_permission
from harkeniq_cc.auth import UserContext
from harkeniq_cc.db.repos import AuditRepo, FleetCacheRepo

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


@router.post(
    "/{agent_id}/enable",
    dependencies=[Depends(require_permission("site.manage"))],
)
async def enable_agent(
    agent_id: str,
    user: UserContext = Depends(require_permission("site.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Enable an agent (placeholder: log + audit)."""
    dev = await FleetCacheRepo(session).get_by_agent_id(agent_id)
    if dev is None:
        raise HTTPException(status_code=404, detail="agent not found")
    logger.info("Agent enable requested: %s by %s", agent_id, user.user_id)
    await AuditRepo(session).append(
        actor=user.user_id,
        action="agent.enable",
        subject=agent_id,
        tenant_id=user.tenant_id,
    )
    await session.commit()
    return {"agent_id": agent_id, "action": "enable", "status": "acknowledged"}


@router.post(
    "/{agent_id}/disable",
    dependencies=[Depends(require_permission("site.manage"))],
)
async def disable_agent(
    agent_id: str,
    user: UserContext = Depends(require_permission("site.manage")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Disable an agent (placeholder: log + audit)."""
    dev = await FleetCacheRepo(session).get_by_agent_id(agent_id)
    if dev is None:
        raise HTTPException(status_code=404, detail="agent not found")
    logger.info("Agent disable requested: %s by %s", agent_id, user.user_id)
    await AuditRepo(session).append(
        actor=user.user_id,
        action="agent.disable",
        subject=agent_id,
        tenant_id=user.tenant_id,
    )
    await session.commit()
    return {"agent_id": agent_id, "action": "disable", "status": "acknowledged"}
