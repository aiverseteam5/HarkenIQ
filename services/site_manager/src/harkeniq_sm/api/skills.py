"""SM skill distribution API (R5): install marketplace skills on agents.

The marketplace (Console) is the source of truth for what a tenant
installed; this endpoint is the site-level delivery path -- it queues
skill_install directives that agents pick up on their next poll and
hot-load through their own SkillReceiver validation.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from harkeniq_sm.api.deps import require_site_token
from harkeniq_sm.db.repos import DeviceRepo, SiteRepo
from harkeniq_sm.skill_validation import SkillValidator

router = APIRouter(
    prefix="/api/skills", dependencies=[Depends(require_site_token)]
)


@router.post("/install")
async def install_skill(request: Request, payload: dict = Body(...)) -> dict:
    """Queue a skill for installation on agents.

    Body: {"skill_id", "skill_version", "yaml_content", "tier",
    "validation_state", "agent_ids": [...] | "all"}.
    """
    state = request.app.state.sm
    directives = getattr(state, "directives", None)
    if directives is None:
        raise HTTPException(
            status_code=409, detail="directive transport not configured"
        )
    yaml_content = str(payload.get("yaml_content", ""))
    skill_id = str(payload.get("skill_id", ""))
    if not yaml_content or not skill_id:
        raise HTTPException(
            status_code=400, detail="skill_id and yaml_content are required"
        )
    # Static validation before anything is queued -- a skill that cannot
    # parse must never reach an agent.
    result = SkillValidator().validate_static(yaml_content)
    if not result.passed:
        raise HTTPException(
            status_code=422,
            detail={"message": "skill failed validation",
                    "errors": result.errors},
        )

    agent_ids = payload.get("agent_ids", "all")
    async with state.sessionmaker() as session:
        site = await SiteRepo(session).get_or_create(state.config.site_name)
        device_repo = DeviceRepo(session)
        if agent_ids == "all":
            devices = list(await device_repo.list_for_site(site.id))
        else:
            devices = []
            for agent_id in agent_ids:
                device = await device_repo.get_by_agent_id(agent_id)
                if device is None:
                    raise HTTPException(
                        status_code=404, detail=f"unknown agent {agent_id}"
                    )
                devices.append(device)
        await session.commit()

    queued = []
    for device in devices:
        directive_id = await directives.enqueue_skill_install(
            device_id=device.id,
            skill_id=skill_id,
            skill_version=str(payload.get("skill_version", "1")),
            yaml_content=yaml_content,
            tier=str(payload.get("tier", "community")),
            validation_state=str(payload.get("validation_state", "tested")),
            issued_by=str(payload.get("issued_by", "marketplace")),
        )
        queued.append({"agent_id": device.agent_id,
                       "directive_id": directive_id})
    return {"queued": queued, "count": len(queued),
            "warnings": result.warnings}
