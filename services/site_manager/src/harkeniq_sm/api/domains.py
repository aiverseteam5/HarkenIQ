"""Fault-domain listing, confirmation, and member editing (A1.1, R-S6 audit)."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from harkeniq_sm.api.deps import require_site_token
from harkeniq_sm.api.site_scope import SiteScope, resolve_site
from harkeniq_sm.db.repos import AuditRepo, DeviceRepo, DomainRepo, SiteRepo

router = APIRouter(
    prefix="/api/fault-domains", dependencies=[Depends(require_site_token)]
)


class DomainPatch(BaseModel):
    confirm: bool = False
    actor: str = "operator"
    members: Optional[list[str]] = None  # agent_ids


def _domain_dict(domain, member_agent_ids: list[str]) -> dict:
    return {
        "id": domain.id,
        "name": domain.name,
        "kind": domain.kind,
        "status": domain.status,
        "confidence": domain.confidence,
        "source": domain.source,
        "confirmed_by": domain.confirmed_by,
        "members": member_agent_ids,
    }


async def _members_as_agent_ids(session, domain_repo, domain_id: str) -> list[str]:
    device_repo = DeviceRepo(session)
    agent_ids = []
    for device_id in await domain_repo.members(domain_id):
        device = await device_repo.get(device_id)
        if device:
            agent_ids.append(device.agent_id)
    return sorted(agent_ids)


@router.get("")
async def list_domains(request: Request,
    scope: SiteScope = Depends(resolve_site),
) -> list[dict]:
    state = request.app.state.sm
    async with state.sessionmaker() as session:
        # E1.3: fault domains belong to ONE site. A blast radius that
        # spanned sites would be a physical claim about buildings that
        # do not touch.
        domain_repo = DomainRepo(session)
        result = [
            _domain_dict(
                domain, await _members_as_agent_ids(session, domain_repo, domain.id)
            )
            for domain in await domain_repo.list_for_site(scope.id)
        ]
        await session.commit()
    return result


@router.patch("/{domain_id}")
async def patch_domain(domain_id: str, patch: DomainPatch, request: Request) -> dict:
    state = request.app.state.sm
    async with state.sessionmaker() as session:
        domain_repo = DomainRepo(session)
        domain = await domain_repo.get(domain_id)
        if domain is None:
            raise HTTPException(status_code=404, detail="unknown fault domain")
        audit = AuditRepo(session)

        if patch.members is not None:
            device_repo = DeviceRepo(session)
            member_ids = []
            for agent_id in patch.members:
                device = await device_repo.get_by_agent_id(agent_id)
                if device is None:
                    raise HTTPException(
                        status_code=422, detail=f"unknown agent_id: {agent_id}"
                    )
                member_ids.append(device.id)
            await domain_repo.set_members(domain.id, member_ids, added_by=patch.actor)
            await audit.append(
                patch.actor, "domain.set_members", domain.name,
                {"members": patch.members},
            )

        if patch.confirm:
            domain.source = "operator" if domain.source == "inference" else domain.source
            await domain_repo.confirm(domain, by=patch.actor)
            await audit.append(patch.actor, "domain.confirm", domain.name)

        result = _domain_dict(
            domain, await _members_as_agent_ids(session, domain_repo, domain.id)
        )
        await session.commit()
    return result
