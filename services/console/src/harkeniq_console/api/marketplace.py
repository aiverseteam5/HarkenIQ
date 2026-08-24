"""Skill marketplace API (R4-3 P17, OQ-22).

Tenant-facing router: browse published skills, submit new ones,
install. Admin router: review queue, approve/reject, promote
community -> verified via the 95% gate. Every review/promote/install is
audit-logged (and therefore on the R4-2 hash chain).
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_console.api.deps import get_session, require_permission
from harkeniq_console.auth import UserContext
from harkeniq_console.db.repos import (
    AuditRepo,
    MarketplaceInstallRepo,
    MarketplaceRepo,
)
from harkeniq_console.marketplace import check_promotion, validate_submission

router = APIRouter(prefix="/api/marketplace", tags=["marketplace"])
admin_router = APIRouter(prefix="/api/admin/marketplace", tags=["marketplace-admin"])


def _entry_dict(e, include_yaml: bool = False) -> dict:
    data = {
        "id": e.id,
        "skill_name": e.skill_name,
        "version": e.version,
        "author_email": e.author_email,
        "tenant_id": e.tenant_id,
        "description": e.description,
        "target": e.target,
        "tier": e.tier,
        "review_status": e.review_status,
        "published": e.published,
        "validation_report": e.validation_report,
        "rejection_reason": e.rejection_reason,
        "install_count": e.install_count,
        "success_count": e.success_count,
        "failure_count": e.failure_count,
        "total_executions": e.total_executions,
        "device_count": e.device_count,
        "success_rate": (
            round(e.success_count / e.total_executions, 4)
            if e.total_executions else None
        ),
        "promoted_at": e.promoted_at.isoformat() if e.promoted_at else None,
        "created_at": e.created_at.isoformat() if e.created_at else None,
    }
    if include_yaml:
        data["yaml_content"] = e.yaml_content
    return data


# ── tenant-facing ───────────────────────────────────────────────────


@router.get("/skills")
async def browse_skills(
    tier: str | None = None,
    target: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Browse published marketplace skills."""
    items, total = await MarketplaceRepo(session).list_filtered(
        tier=tier, target=target, published=True,
        page=page, page_size=page_size,
    )
    return {"items": [_entry_dict(e) for e in items], "total": total,
            "page": page}


@router.post("/skills")
async def submit_skill(
    payload: dict = Body(...),
    user: UserContext = Depends(require_permission("skill.submit")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Submit a community skill: {"yaml_content": "..."}.

    Schema validation runs immediately; invalid submissions are
    rejected outright (never enter the review queue).
    """
    yaml_content = payload.get("yaml_content", "")
    if not yaml_content or not isinstance(yaml_content, str):
        raise HTTPException(status_code=400, detail="yaml_content is required")
    validation = validate_submission(yaml_content)
    if not validation.passed:
        raise HTTPException(
            status_code=422,
            detail={"message": "skill failed validation",
                    "validation": validation.to_dict()},
        )
    repo = MarketplaceRepo(session)
    existing = await repo.get_by_name_version(
        validation.skill_name, validation.version
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"{validation.skill_name} v{validation.version} "
                   "already submitted",
        )
    entry = await repo.submit(
        skill_name=validation.skill_name,
        version=validation.version,
        yaml_content=yaml_content,
        author_email=user.email,
        tenant_id=user.tenant_id,
        description=validation.description,
        target=validation.target,
        validation_report=validation.to_dict(),
    )
    await AuditRepo(session).append(
        actor_id=user.user_id, actor_email=user.email,
        action="marketplace.skill.submit", subject_type="marketplace_skill",
        subject_id=entry.id, tenant_id=user.tenant_id,
        detail={"skill_name": entry.skill_name, "version": entry.version},
    )
    await session.commit()
    return _entry_dict(entry)


@router.get("/skills/{entry_id}")
async def get_skill(
    entry_id: str,
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    entry = await MarketplaceRepo(session).get_by_id(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="skill not found")
    if not entry.published and entry.tenant_id != user.tenant_id \
            and not user.is_platform_user:
        raise HTTPException(status_code=404, detail="skill not found")
    return _entry_dict(entry, include_yaml=entry.published
                       or entry.tenant_id == user.tenant_id
                       or user.is_platform_user)


@router.post("/skills/{entry_id}/install")
async def install_skill(
    entry_id: str,
    user: UserContext = Depends(require_permission("skill.install")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Install a published skill: returns the YAML for distribution
    (CC/SM push transport is the integration layer; the marketplace is
    the source of truth for what a tenant installed)."""
    repo = MarketplaceRepo(session)
    entry = await repo.get_by_id(entry_id)
    if entry is None or not entry.published:
        raise HTTPException(status_code=404, detail="skill not available")
    await repo.record_install(entry)
    # R5-2: install event -- CC pulls these to deliver to the tenant's sites
    if user.tenant_id:
        await MarketplaceInstallRepo(session).record(
            tenant_id=user.tenant_id, skill_entry_id=entry.id,
            installed_by=user.email,
        )
    await AuditRepo(session).append(
        actor_id=user.user_id, actor_email=user.email,
        action="marketplace.skill.install", subject_type="marketplace_skill",
        subject_id=entry.id, tenant_id=user.tenant_id,
        detail={"skill_name": entry.skill_name, "version": entry.version,
                "tier": entry.tier},
    )
    await session.commit()
    return {
        "skill_name": entry.skill_name,
        "version": entry.version,
        "tier": entry.tier,
        "yaml_content": entry.yaml_content,
        "install_count": entry.install_count,
    }


# ── admin (review + promotion) ──────────────────────────────────────


@admin_router.get("/skills")
async def review_queue(
    review_status: str | None = Query("submitted"),
    tier: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user: UserContext = Depends(require_permission("skill.review")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    items, total = await MarketplaceRepo(session).list_filtered(
        review_status=review_status or None, tier=tier,
        page=page, page_size=page_size,
    )
    return {"items": [_entry_dict(e, include_yaml=True) for e in items],
            "total": total, "page": page}


@admin_router.post("/skills/{entry_id}/review")
async def review_skill(
    entry_id: str,
    payload: dict = Body(...),
    user: UserContext = Depends(require_permission("skill.review")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Approve or reject a submission: {"approve": bool, "reason": str}."""
    repo = MarketplaceRepo(session)
    entry = await repo.get_by_id(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="skill not found")
    if entry.review_status != "submitted":
        raise HTTPException(
            status_code=409,
            detail=f"skill already {entry.review_status}",
        )
    approve = bool(payload.get("approve"))
    reason = str(payload.get("reason", ""))
    if not approve and not reason:
        raise HTTPException(
            status_code=400, detail="rejection requires a reason"
        )
    # Re-validate at review time: the parser may have tightened since
    # submission, and the reviewer's approval must reflect current rules.
    validation = validate_submission(entry.yaml_content)
    if approve and not validation.passed:
        raise HTTPException(
            status_code=422,
            detail={"message": "skill no longer passes validation",
                    "validation": validation.to_dict()},
        )
    await repo.review(entry, approve=approve, reviewer_email=user.email,
                      reason=reason)
    await AuditRepo(session).append(
        actor_id=user.user_id, actor_email=user.email,
        action="marketplace.skill.approve" if approve
               else "marketplace.skill.reject",
        subject_type="marketplace_skill", subject_id=entry.id,
        detail={"skill_name": entry.skill_name, "version": entry.version,
                "reason": reason},
    )
    await session.commit()
    return _entry_dict(entry)


@admin_router.post("/skills/{entry_id}/stats")
async def report_stats(
    entry_id: str,
    payload: dict = Body(...),
    user: UserContext = Depends(require_permission("skill.review")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Record fleet execution stats: {"successes": n, "failures": n,
    "devices": n}. Reported by CC deployments / operators."""
    repo = MarketplaceRepo(session)
    entry = await repo.get_by_id(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="skill not found")
    await repo.record_stats(
        entry,
        successes=int(payload.get("successes", 0)),
        failures=int(payload.get("failures", 0)),
        devices=int(payload.get("devices", 0)),
    )
    await session.commit()
    gate = check_promotion(entry.total_executions, entry.success_count,
                           entry.device_count)
    return {**_entry_dict(entry), "promotion": gate.to_dict()}


@admin_router.post("/skills/{entry_id}/promote")
async def promote_skill(
    entry_id: str,
    user: UserContext = Depends(require_permission("skill.review")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Promote community -> verified (OQ-22 gate: >= 95% over >= 50
    executions on >= 50 devices)."""
    repo = MarketplaceRepo(session)
    entry = await repo.get_by_id(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="skill not found")
    if entry.tier != "community" or not entry.published:
        raise HTTPException(
            status_code=409,
            detail="only published community skills can be promoted",
        )
    gate = check_promotion(entry.total_executions, entry.success_count,
                           entry.device_count)
    if not gate.eligible:
        raise HTTPException(
            status_code=422,
            detail={"message": "promotion gate not met",
                    "promotion": gate.to_dict()},
        )
    await repo.promote(entry)
    await AuditRepo(session).append(
        actor_id=user.user_id, actor_email=user.email,
        action="marketplace.skill.promote", subject_type="marketplace_skill",
        subject_id=entry.id,
        detail={"skill_name": entry.skill_name, "version": entry.version,
                "success_rate": gate.success_rate,
                "executions": entry.total_executions,
                "devices": entry.device_count},
    )
    await session.commit()
    return _entry_dict(entry)
