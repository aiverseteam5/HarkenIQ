"""Site enrollment and per-site halt. E1.3.

Two operator surfaces this slice needs, both behind the existing site
token. Neither introduces a user, a role or a second authorization
resolver: authority over a Site Manager remains the service identity it
already had, and human and agent authority stay governed above it at
Central Command (E1.2), unchanged.

**Enrollment** issues and revokes the site-bound credential a device
presents at registration, so a device's site is something this Site
Manager knows rather than something an agent claims.

**Halt** is per site by default (ratified D2). Stopping Site A leaves
Site B running. The Site Manager-wide emergency halt is a separate,
separately audited action and never what the site control means.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from harkeniq_sm.api.deps import require_site_token
from harkeniq_sm.db.repos import AuditRepo, SiteRepo
from harkeniq_sm.enrollment import EnrollmentService
from harkeniq_sm.stopswitch import (
    SCOPE_SITE,
    SCOPE_SITE_MANAGER,
    StopSwitchService,
)

logger = logging.getLogger("harkeniq.sm.api.sites")

router = APIRouter(
    prefix="/api/sites", dependencies=[Depends(require_site_token)]
)


class IssueTokenBody(BaseModel):
    actor: str = Field(min_length=1, max_length=255)
    label: str = Field("", max_length=255)
    expires_at: Optional[datetime] = None


class HaltBody(BaseModel):
    actor: str = Field(min_length=1, max_length=255)
    reason: str = Field("", max_length=512)


class ManagerHaltBody(BaseModel):
    """The Site Manager-wide emergency halt.

    Deliberately harder to fire than a site halt: it stops every site
    this process serves, so it takes an explicit reason and a typed
    confirmation rather than being the default meaning of "stop".
    """

    actor: str = Field(min_length=1, max_length=255)
    reason: str = Field(min_length=1, max_length=512)
    #: Must be the literal string "HALT ALL SITES".
    confirm: str = Field(min_length=1, max_length=64)


async def _site_or_404(session, site_name: str):
    site = await SiteRepo(session).get_by_name(site_name)
    if site is None:
        raise HTTPException(
            status_code=404,
            detail=f"this Site Manager does not serve a site named {site_name!r}",
        )
    return site


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


@router.get("/")
async def list_sites(request: Request) -> dict:
    """Every site this Site Manager serves, with its halt state."""
    state = request.app.state.sm
    switches = StopSwitchService(state.sessionmaker)
    async with state.sessionmaker() as session:
        sites = await SiteRepo(session).list_all()
        rows = await switches.rows(session)
        from harkeniq_sm.stopswitch import halt_state

        return {
            "sites": [
                {
                    "id": s.id,
                    "name": s.name,
                    "cc_site_id": s.cc_site_id,
                    "status": s.status,
                    "halt": halt_state(s.id, rows).as_dict(),
                }
                for s in sites
            ],
            "site_count": len(sites),
        }


@router.get("/{site_name}/enrollment-tokens")
async def list_tokens(site_name: str, request: Request) -> dict:
    """Credentials issued for this site. Never the secrets themselves."""
    from sqlalchemy import select

    from harkeniq_sm.db.models import SiteEnrollmentToken

    state = request.app.state.sm
    async with state.sessionmaker() as session:
        site = await _site_or_404(session, site_name)
        rows = (
            await session.execute(
                select(SiteEnrollmentToken)
                .where(SiteEnrollmentToken.site_id == site.id)
                .order_by(SiteEnrollmentToken.issued_at.desc())
            )
        ).scalars().all()
        return {
            "site": site.name,
            "tokens": [
                {
                    "id": r.id,
                    "label": r.label,
                    "issued_by": r.issued_by,
                    "issued_at": r.issued_at.isoformat() if r.issued_at else None,
                    "expires_at": (
                        r.expires_at.isoformat() if r.expires_at else None
                    ),
                    "revoked_at": (
                        r.revoked_at.isoformat() if r.revoked_at else None
                    ),
                    "use_count": r.use_count,
                    "last_used_at": (
                        r.last_used_at.isoformat() if r.last_used_at else None
                    ),
                }
                for r in rows
            ],
        }


# ---------------------------------------------------------------------------
# Enrollment
# ---------------------------------------------------------------------------


@router.post("/{site_name}/enrollment-tokens", status_code=201)
async def issue_token(
    site_name: str, body: IssueTokenBody, request: Request
) -> dict:
    """Mint a site-bound enrollment credential.

    The secret is returned ONCE. Only its hash is stored, so a leaked
    database yields nothing an attacker could enroll with, and this
    response is the only chance to copy it.
    """
    state = request.app.state.sm
    service = EnrollmentService(state.sessionmaker, state.config)
    async with state.sessionmaker() as session:
        site = await _site_or_404(session, site_name)
        secret, row = await service.issue(
            session,
            site_id=site.id,
            label=body.label,
            issued_by=body.actor,
            expires_at=body.expires_at,
        )
        await AuditRepo(session).append(
            actor=body.actor,
            action="enrollment.issued",
            subject=row.id,
            site_id=site.id,
            detail={"site": site.name, "label": body.label},
        )
        await session.commit()
        return {
            "id": row.id,
            "site": site.name,
            "token": secret,
            "shown_once": True,
            "note": (
                "Only the hash is stored. Copy this now; it cannot be "
                "recovered."
            ),
        }


@router.delete("/{site_name}/enrollment-tokens/{token_id}")
async def revoke_token(
    site_name: str, token_id: str, request: Request, actor: str = "operator"
) -> dict:
    """Revoke a credential. A timestamp, never a delete.

    An audit entry naming this credential has to stay resolvable, and a
    revoked credential must remain distinguishable from one that never
    existed.
    """
    from harkeniq_sm.db.models import SiteEnrollmentToken

    state = request.app.state.sm
    service = EnrollmentService(state.sessionmaker, state.config)
    async with state.sessionmaker() as session:
        site = await _site_or_404(session, site_name)
        row = await session.get(SiteEnrollmentToken, token_id)
        if row is None or row.site_id != site.id:
            raise HTTPException(status_code=404, detail="credential not found")
        await service.revoke(session, row, revoked_by=actor)
        await AuditRepo(session).append(
            actor=actor,
            action="enrollment.revoked",
            subject=row.id,
            site_id=site.id,
            detail={"site": site.name},
        )
        await session.commit()
        return {"id": row.id, "revoked": True}


# ---------------------------------------------------------------------------
# Halt
# ---------------------------------------------------------------------------


@router.post("/{site_name}/stop")
async def stop_site(site_name: str, body: HaltBody, request: Request) -> dict:
    """Stop ONE site. Every other site this process serves keeps running."""
    return await _set_site_halt(site_name, body, request, active=True)


@router.post("/{site_name}/stop/lift")
async def resume_site(
    site_name: str, body: HaltBody, request: Request
) -> dict:
    return await _set_site_halt(site_name, body, request, active=False)


async def _set_site_halt(
    site_name: str, body: HaltBody, request: Request, *, active: bool
) -> dict:
    state = request.app.state.sm
    switches = StopSwitchService(state.sessionmaker)
    async with state.sessionmaker() as session:
        site = await _site_or_404(session, site_name)
        await switches.set_halt(
            session, scope=SCOPE_SITE, site_id=site.id, active=active,
            actor=body.actor, reason=body.reason,
        )
        await AuditRepo(session).append(
            actor=body.actor,
            action="site.stopped" if active else "site.resumed",
            subject=site.name,
            site_id=site.id,
            detail={"reason": body.reason},
        )
        state_after = await switches.state_for(session, site.id)
        await session.commit()
        return {"site": site.name, "halt": state_after.as_dict()}


@router.post("/emergency-halt")
async def emergency_halt(body: ManagerHaltBody, request: Request) -> dict:
    """Stop EVERY site this Site Manager serves.

    Separate from the site control on purpose (ratified D2): making this
    the default meaning of "stop" would let one site's trouble halt
    another's unrelated work, which is the coupling E0.2 spent a slice
    removing.
    """
    if body.confirm != "HALT ALL SITES":
        raise HTTPException(
            status_code=400,
            detail=(
                "confirm must be the literal string 'HALT ALL SITES': this "
                "stops every site this Site Manager serves, so it is not a "
                "thing to fire by mistyping a site name"
            ),
        )
    state = request.app.state.sm
    switches = StopSwitchService(state.sessionmaker)
    async with state.sessionmaker() as session:
        sites = await SiteRepo(session).list_all()
        await switches.set_halt(
            session, scope=SCOPE_SITE_MANAGER, site_id=None, active=True,
            actor=body.actor, reason=body.reason,
        )
        await AuditRepo(session).append(
            actor=body.actor,
            action="site_manager.emergency_halt",
            subject="site_manager",
            detail={"reason": body.reason, "sites": [s.name for s in sites]},
        )
        await session.commit()
    return {
        "halted": True,
        "scope": "site_manager",
        "sites_affected": [s.name for s in sites],
    }


@router.post("/emergency-halt/lift")
async def lift_emergency_halt(body: HaltBody, request: Request) -> dict:
    """Lift the Site Manager-wide halt.

    Any per-site halt still in force stays in force -- lifting the
    emergency does not silently resume a site an operator stopped
    separately.
    """
    state = request.app.state.sm
    switches = StopSwitchService(state.sessionmaker)
    async with state.sessionmaker() as session:
        await switches.set_halt(
            session, scope=SCOPE_SITE_MANAGER, site_id=None, active=False,
            actor=body.actor, reason=body.reason,
        )
        await AuditRepo(session).append(
            actor=body.actor,
            action="site_manager.emergency_halt_lifted",
            subject="site_manager",
            detail={"reason": body.reason},
        )
        await session.commit()
    return {"halted": False, "scope": "site_manager"}
