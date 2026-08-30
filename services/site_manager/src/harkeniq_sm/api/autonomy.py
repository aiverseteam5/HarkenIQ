"""Autonomy enforcement state + controls (QA-021, spec A2.2/A2.6).

Read the enforcer/suppression state, flip the SM-local stop switch, and
human-re-enable a suppressed fault domain. Every mutation requires a
named actor (QA-006 pattern) and lands on the audit chain.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from harkeniq_sm.api.deps import require_site_token
from harkeniq_sm.db.repos import AuditRepo, ErrorBudgetRepo, SiteRepo

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
    # S5: the persisted A2.2 error budgets. Same state Central Command
    # now reads over the fleet snapshot; this is the break-glass view.
    state = request.app.state.sm
    async with state.sessionmaker() as session:
        result["error_budgets"] = [
            {
                "action_type": r.action_type,
                "success_count": r.success_count,
                "failure_count": r.failure_count,
                "total_count": r.total_count,
                "min_success_rate": r.min_success_rate,
                "dropped_back": r.dropped_back,
                "dropped_back_at": (
                    r.dropped_back_at.isoformat() if r.dropped_back_at else None
                ),
            }
            for r in await ErrorBudgetRepo(session).list_all()
        ]
        # E0.2: budgets are per site now, so the break-glass view names
        # the site each row belongs to rather than implying one fleet.
        site_names = {
            site.id: site.name for site in await SiteRepo(session).list_all()
        }
        for row, entry in zip(
            await ErrorBudgetRepo(session).list_all(), result["error_budgets"],
        ):
            entry["site_id"] = row.site_id
            entry["site_name"] = site_names.get(row.site_id, "")
    return result


class RecoverBody(ActorBody):
    #: Which site's drop-back to lift. Required when this Site Manager
    #: serves more than one site: clearing every site's withdrawal at
    #: once would restore autonomy where nobody reviewed the failures.
    site: str = Field("", max_length=255)


@router.post("/error-budget/{action_type}/recover")
async def recover_error_budget(
    request: Request, action_type: str, body: RecoverBody,
) -> dict:
    """Operator reviews the failures and restores autonomy for a class
    AT ONE SITE.

    Recovery lives here, at the site, because it is the counterpart of a
    demotion the Site Manager made. A tenant-plane control for it is a
    named capability-registry candidate (design doc S11), not something
    S5 invents a second CC->SM command path for.

    E0.2: `site` names the site by name. It may be omitted only when
    this Site Manager serves exactly one site, where there is nothing to
    disambiguate.
    """
    state = request.app.state.sm
    async with state.sessionmaker() as session:
        repo = SiteRepo(session)
        if body.site:
            site = await repo.get_by_name(body.site)
            if site is None:
                raise HTTPException(
                    status_code=404, detail=f"unknown site {body.site!r}",
                )
        else:
            sites = list(await repo.list_all())
            if len(sites) != 1:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"this Site Manager serves {len(sites)} sites; name "
                        f"the one whose drop-back you are lifting"
                    ),
                )
            site = sites[0]

        if not await ErrorBudgetRepo(session).recover(site.id, action_type):
            raise HTTPException(
                status_code=404,
                detail=f"{action_type} is not dropped back at site {site.name!r}",
            )
        await AuditRepo(session).append(
            actor=body.actor,
            action="error_budget.recover",
            subject=f"action:{action_type}",
            detail={"source": "sm.api", "site_id": site.id,
                    "site_name": site.name},
        )
        await session.commit()
    return {"recovered": action_type, "site": site.name}


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
