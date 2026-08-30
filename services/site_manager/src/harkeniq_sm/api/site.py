"""Site-model YAML round-trip endpoints (A1.1)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from harkeniq_sm.api.deps import require_site_token
from harkeniq_sm.db.repos import AuditRepo, SiteRepo
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


class UnbindBody(BaseModel):
    """Break-glass recovery for a site binding (E0.2).

    Deliberately verbose. Clearing a binding is how a Site Manager
    forgets which Central Command site it serves, and the next
    RegisterSite will bind it to whatever asks first, so it must be an
    explicit, attributable act rather than a convenience.
    """

    actor: str = Field(min_length=1, max_length=255)
    #: Must equal the site's own name. A confirmation the operator has to
    #: type, so an unbind cannot be a mistyped path parameter.
    confirm_site_name: str = Field(min_length=1, max_length=255)
    reason: str = Field(min_length=1, max_length=512)


@router.get("/bindings")
async def list_bindings(request: Request) -> dict:
    """Which Central Command site each site here is bound to.

    The read an operator needs before deciding whether an unbind is
    warranted, and the evidence that a binding exists at all.
    """
    state = request.app.state.sm
    async with state.sessionmaker() as session:
        sites = await SiteRepo(session).list_all()
        return {
            "sites": [
                {
                    "id": s.id,
                    "name": s.name,
                    "cc_site_id": s.cc_site_id or None,
                    "bound": bool(s.cc_site_id),
                    "status": s.status,
                    "bound_at": s.bound_at.isoformat() if s.bound_at else None,
                }
                for s in sites
            ],
            "total": len(sites),
        }


@router.post("/{site_name}/unbind")
async def unbind_site(request: Request, site_name: str, body: UnbindBody) -> dict:
    """Clear a site's Central Command binding so it can be re-bound.

    The sanctioned recovery when Central Command legitimately changed its
    site ids -- a restore from backup, for instance. Registration itself
    never overwrites a binding (E0.2 requirement 2), so this is the only
    way back, and it is audited with a named actor and a stated reason.

    Everything the site owns -- devices, incidents, actions, outcomes --
    is left exactly where it is. Only the tenant-plane identity is
    cleared, and until it is re-bound the site's fleet snapshot returns
    an explicit empty result rather than anything belonging to another
    site.
    """
    if body.confirm_site_name != site_name:
        raise HTTPException(
            status_code=400,
            detail=(
                "confirm_site_name must match the site being unbound; "
                "this is the confirmation, not a duplicate field"
            ),
        )
    state = request.app.state.sm
    async with state.sessionmaker() as session:
        repo = SiteRepo(session)
        site = await repo.get_by_name(site_name)
        if site is None:
            raise HTTPException(status_code=404, detail=f"unknown site {site_name!r}")
        if not site.cc_site_id:
            raise HTTPException(
                status_code=409, detail=f"site {site_name!r} is not bound",
            )
        previous = site.cc_site_id
        await repo.unbind(site)
        await AuditRepo(session).append(
            actor=body.actor,
            action="site.unbound",
            subject=site.id,
            detail={
                "site_name": site.name,
                "previous_cc_site_id": previous,
                "reason": body.reason,
                "source": "sm.api.break_glass",
            },
        )
        await session.commit()
    return {
        "unbound": site_name,
        "previous_cc_site_id": previous,
        "note": (
            "the next RegisterSite naming this site will bind it; until "
            "then its fleet snapshot returns an explicit empty result"
        ),
    }
