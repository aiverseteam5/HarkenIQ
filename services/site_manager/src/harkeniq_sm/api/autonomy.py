"""Autonomy enforcement state + controls (QA-021, spec A2.2/A2.6).

Read the enforcer/suppression state, flip the SM-local stop switch, and
human-re-enable a suppressed fault domain. Every mutation requires a
named actor (QA-006 pattern) and lands on the audit chain.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from harkeniq_sm.api.deps import require_site_token
from harkeniq_sm.db.repos import AuditRepo

router = APIRouter(
    prefix="/api/autonomy", dependencies=[Depends(require_site_token)]
)


class ActorBody(BaseModel):
    actor: str = Field(min_length=1, max_length=255)


def _enforcer(request: Request):
    enforcer = getattr(request.app.state.sm, "autonomy", None)
    if enforcer is None:
        raise HTTPException(status_code=503, detail="autonomy enforcer not configured")
    return enforcer


def _suppression(request: Request):
    engine = getattr(request.app.state.sm, "suppression", None)
    if engine is None:
        raise HTTPException(status_code=503, detail="suppression engine not configured")
    return engine


@router.get("")
async def autonomy_state(request: Request) -> dict:
    result = _enforcer(request).get_state()
    engine = getattr(request.app.state.sm, "suppression", None)
    if engine is not None:
        result["suppression"] = engine.get_state()
    return result


@router.post("/stop-switch")
async def activate_stop_switch(request: Request, body: ActorBody) -> dict:
    return await _set_stop_switch(request, body.actor, active=True)


@router.post("/stop-switch/deactivate")
async def deactivate_stop_switch(request: Request, body: ActorBody) -> dict:
    return await _set_stop_switch(request, body.actor, active=False)


async def _set_stop_switch(request: Request, actor: str, active: bool) -> dict:
    state = request.app.state.sm
    enforcer = _enforcer(request)
    actor = f"sm-local:{actor}"
    if active:
        enforcer.activate_stop_switch(actor)
    else:
        enforcer.deactivate_stop_switch(actor)
    async with state.sessionmaker() as session:
        await AuditRepo(session).append(
            actor=actor,
            action="stop_switch.activate" if active else "stop_switch.deactivate",
            subject=f"site:{state.config.site_name}",
            detail={"source": "sm.api"},
        )
        await session.commit()
    return {"stop_switch": enforcer.stop_switch_active}


@router.post("/suppression/{domain_id}/re-enable")
async def re_enable_domain(request: Request, domain_id: str, body: ActorBody) -> dict:
    """Operator explicitly lifts suppression (required for S1/S2 per A2.6)."""
    state = request.app.state.sm
    engine = _suppression(request)
    if not engine.human_re_enable(domain_id, body.actor):
        raise HTTPException(status_code=404, detail="domain not suppressed")
    async with state.sessionmaker() as session:
        await AuditRepo(session).append(
            actor=body.actor,
            action="suppression.re_enable",
            subject=f"domain:{domain_id}",
            detail={"source": "sm.api"},
        )
        await session.commit()
    return {"re_enabled": domain_id}
