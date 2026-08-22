"""API key management endpoints (tenant-scoped) + impersonation log (admin)."""

from __future__ import annotations

import hashlib
import secrets
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_console.api.deps import get_session, require_super_admin, tenant_scope
from harkeniq_console.auth import UserContext
from harkeniq_console.db.models import utcnow
from harkeniq_console.db.repos import (
    ApiKeyRepo,
    AuditRepo,
    ImpersonationLogRepo,
)

router = APIRouter(prefix="/api/tenants/{tenant_id}/api-keys", tags=["api-keys"])
impersonation_router = APIRouter(prefix="/api/admin/impersonation", tags=["impersonation"])

_VALID_SCOPES = {"read", "write", "admin"}


def _key_dict(k, *, include_raw: str | None = None) -> dict:
    d = {
        "id": k.id,
        "tenant_id": k.tenant_id,
        "name": k.name,
        "key_prefix": k.key_prefix,
        "scope": k.scope,
        "status": k.status,
        "created_by": k.created_by,
        "created_at": k.created_at.isoformat() if k.created_at else None,
        "expires_at": k.expires_at.isoformat() if k.expires_at else None,
        "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
        "revoked_at": k.revoked_at.isoformat() if k.revoked_at else None,
    }
    if include_raw:
        d["key"] = include_raw
    return d


def _imp_dict(i) -> dict:
    return {
        "id": i.id,
        "admin_user_id": i.admin_user_id,
        "admin_email": i.admin_email,
        "tenant_id": i.tenant_id,
        "started_at": i.started_at.isoformat() if i.started_at else None,
        "ended_at": i.ended_at.isoformat() if i.ended_at else None,
        "actions_count": i.actions_count,
    }


# ── Request models ───────────────────────────────────────────────────


class CreateApiKeyRequest(BaseModel):
    name: str
    scope: str = "read"
    expires_in_days: int | None = None


# ── API Key endpoints ────────────────────────────────────────────────


@router.get("/")
async def list_api_keys(
    tenant_id: str,
    status: str | None = None,
    page: int = 1,
    page_size: int = 50,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(tenant_scope),
) -> dict:
    items, total = await ApiKeyRepo(session).list_by_tenant(
        tenant_id, status=status, page=page, page_size=page_size,
    )
    return {"items": [_key_dict(k) for k in items], "total": total, "page": page}


@router.post("/")
async def create_api_key(
    tenant_id: str,
    body: CreateApiKeyRequest,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(tenant_scope),
) -> dict:
    if body.scope not in _VALID_SCOPES:
        raise HTTPException(400, f"scope must be one of {_VALID_SCOPES}")

    # Generate key: hiq_<32 hex chars>
    raw_key = f"hiq_{secrets.token_hex(16)}"
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    key_prefix = raw_key[:12]

    expires_at = None
    if body.expires_in_days:
        expires_at = utcnow() + timedelta(days=body.expires_in_days)

    api_key = await ApiKeyRepo(session).create(
        tenant_id=tenant_id,
        name=body.name,
        key_hash=key_hash,
        key_prefix=key_prefix,
        scope=body.scope,
        created_by=user.user_id,
        expires_at=expires_at,
    )

    await AuditRepo(session).append(
        actor_id=user.user_id,
        actor_email=user.email,
        action="api_key.created",
        subject_type="api_key",
        subject_id=api_key.id,
        tenant_id=tenant_id,
        detail={"name": body.name, "scope": body.scope},
    )
    await session.commit()

    # Return the raw key only on creation -- never again
    return _key_dict(api_key, include_raw=raw_key)


@router.post("/{key_id}/revoke")
async def revoke_api_key(
    tenant_id: str,
    key_id: str,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(tenant_scope),
) -> dict:
    repo = ApiKeyRepo(session)
    key = await repo.get_by_id(key_id)
    if key is None or key.tenant_id != tenant_id:
        raise HTTPException(404, "API key not found")
    if key.status == "revoked":
        raise HTTPException(400, "API key already revoked")

    await repo.revoke(key)

    await AuditRepo(session).append(
        actor_id=user.user_id,
        actor_email=user.email,
        action="api_key.revoked",
        subject_type="api_key",
        subject_id=key_id,
        tenant_id=tenant_id,
    )
    await session.commit()
    return _key_dict(key)


# ── Impersonation Log endpoints ──────────────────────────────────────


@impersonation_router.get("/")
async def list_impersonation_logs(
    admin_user_id: str | None = None,
    tenant_id: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    page: int = 1,
    page_size: int = 50,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_super_admin),
) -> dict:
    df = datetime.combine(date_from, datetime.min.time()) if date_from else None
    dt = datetime.combine(date_to, datetime.max.time()) if date_to else None
    items, total = await ImpersonationLogRepo(session).list_filtered(
        admin_user_id=admin_user_id, tenant_id=tenant_id,
        date_from=df, date_to=dt, page=page, page_size=page_size,
    )
    return {"items": [_imp_dict(i) for i in items], "total": total, "page": page}


@impersonation_router.get("/{log_id}")
async def get_impersonation_detail(
    log_id: str,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_super_admin),
) -> dict:
    entry = await ImpersonationLogRepo(session).get_by_id(log_id)
    if entry is None:
        raise HTTPException(404, "impersonation log not found")

    # Get audit trail for actions during this session
    audit_entries, _ = await AuditRepo(session).list_filtered(
        actor=entry.admin_email,
        date_from=entry.started_at,
        date_to=entry.ended_at,
        page=1,
        page_size=100,
    )

    return {
        "impersonation": _imp_dict(entry),
        "audit_trail": [
            {
                "id": e.id,
                "ts": e.ts.isoformat() if e.ts else None,
                "action": e.action,
                "subject_type": e.subject_type,
                "subject_id": e.subject_id,
            }
            for e in audit_entries
        ],
    }
