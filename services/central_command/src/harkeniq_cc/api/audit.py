"""Audit API: paginated audit log entries + chain verification (R4-2)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_cc.api.deps import forbid_out_of_scope, get_current_user, get_scope, get_session, require_permission
from harkeniq_cc.auth import UserContext
from harkeniq_cc.db.repos import AuditRepo

router = APIRouter(prefix="/api/audit", tags=["audit"])


def _entry_dict(row) -> dict:
    return {
        "id": row.id,
        "ts": row.ts.isoformat() if row.ts else None,
        "actor": row.actor,
        "action": row.action,
        "subject": row.subject,
        "tenant_id": row.tenant_id,
        # E1.2: authorization/indexing metadata, outside the chain
        # payload. Null means tenant-level (and every pre-E1.2 row).
        "site_id": row.site_id,
        "detail": row.detail,
        "seq": row.seq,
        "prev_hash": row.prev_hash,
        "entry_hash": row.entry_hash,
    }


@router.get("/", dependencies=[Depends(require_permission("audit.view"))])
async def list_audit(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    actor: str | None = None,
    action: str | None = None,
    user: UserContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    """Paginated audit entries."""
    repo = AuditRepo(session)
    rows = await repo.list_filtered(
        tenant_id=user.tenant_id,
        actor=actor,
        action=action,
        page=page,
        page_size=page_size,
        scope=scope,
    )
    total = await repo.count_filtered(
        tenant_id=user.tenant_id, scope=scope, actor=actor, action=action,
    )
    return {
        "entries": [_entry_dict(r) for r in rows],
        "page": page,
        "page_size": page_size,
        "total": total,
        "tenant_id": user.tenant_id,
    }


@router.get("/verify", dependencies=[Depends(require_permission("audit.view"))])
async def verify_audit_chain(
    user: UserContext = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Verify the SHA-256 audit hash chain (R4-2 P12, OQ-20: on-demand)."""
    result = await AuditRepo(session).verify_chain()
    return {
        "valid": result.valid,
        "length": result.length,
        "first_bad_seq": result.first_bad_seq,
        "error": result.error,
        "tenant_id": user.tenant_id,
    }
