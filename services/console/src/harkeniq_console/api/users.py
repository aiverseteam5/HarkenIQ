"""User and custom role management endpoints."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_console.api.deps import get_session, tenant_scope
from harkeniq_console.auth import UserContext
from harkeniq_console.db.repos import AuditRepo, CustomRoleRepo, TenantRepo, UserRepo
from harkeniq_console.permissions import PERMISSIONS, ROLE_PERMISSIONS, has_permission

router = APIRouter(prefix="/api/tenants/{tenant_id}/users", tags=["users"])


class InviteUserBody(BaseModel):
    email: str
    role: str = "viewer"
    custom_role_ids: list[str] = []


class UpdateUserBody(BaseModel):
    role: Optional[str] = None
    status: Optional[str] = None
    display_name: Optional[str] = None
    custom_role_ids: Optional[list[str]] = None


def _user_dict(u) -> dict:
    return {
        "id": u.id,
        "tenant_id": u.tenant_id,
        "email": u.email,
        "display_name": u.display_name,
        "role": u.role,
        "status": u.status,
        "is_platform_user": u.is_platform_user,
        "created_at": u.created_at.isoformat(),
        "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
    }


@router.post("/invite")
async def invite_user(
    tenant_id: str,
    body: InviteUserBody,
    user: UserContext = Depends(tenant_scope),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if not has_permission(user.role, "user.manage", user.permissions):
        raise HTTPException(status_code=403, detail="missing permission: user.manage")

    tenant = await TenantRepo(session).get_by_id(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="tenant not found")

    repo = UserRepo(session)
    existing = await repo.get_by_email(tenant_id, body.email)
    if existing:
        raise HTTPException(status_code=409, detail="user already exists in this tenant")

    if body.role not in ROLE_PERMISSIONS and body.role not in (
        "tenant_owner", "site_admin", "operator", "auditor", "viewer",
    ):
        raise HTTPException(status_code=400, detail=f"invalid role: {body.role}")

    new_user = await repo.create(
        tenant_id=tenant_id,
        email=body.email,
        role=body.role,
        status="invited",
        invited_by=user.user_id,
    )

    await AuditRepo(session).append(
        actor_id=user.user_id,
        actor_email=user.email,
        action="user.invite",
        subject_type="user",
        subject_id=new_user.id,
        tenant_id=tenant_id,
        detail={"email": body.email, "role": body.role},
    )
    await session.commit()
    return _user_dict(new_user)


@router.get("/")
async def list_users(
    tenant_id: str,
    search: Optional[str] = Query(None),
    role: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    _user: UserContext = Depends(tenant_scope),
    session: AsyncSession = Depends(get_session),
) -> dict:
    items, total = await UserRepo(session).list_by_tenant(
        tenant_id, search=search, role=role, status=status,
        page=page, page_size=page_size,
    )
    return {
        "items": [_user_dict(u) for u in items],
        "total": total, "page": page, "page_size": page_size,
    }


@router.get("/{user_id}")
async def get_user(
    tenant_id: str, user_id: str,
    _user: UserContext = Depends(tenant_scope),
    session: AsyncSession = Depends(get_session),
) -> dict:
    u = await UserRepo(session).get_by_id(user_id)
    if not u or u.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="user not found")
    custom_roles = await CustomRoleRepo(session).get_user_custom_roles(user_id)
    result = _user_dict(u)
    result["custom_roles"] = [{"id": cr.id, "name": cr.name} for cr in custom_roles]
    # Compute effective permissions
    base_perms = ROLE_PERMISSIONS.get(u.role, set())
    custom_perms = set()
    for cr in custom_roles:
        if cr.permissions:
            custom_perms.update(cr.permissions)
    result["effective_permissions"] = sorted(base_perms | custom_perms)
    return result


@router.patch("/{user_id}")
async def update_user(
    tenant_id: str, user_id: str,
    body: UpdateUserBody,
    user: UserContext = Depends(tenant_scope),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if not has_permission(user.role, "user.manage", user.permissions):
        raise HTTPException(status_code=403, detail="missing permission: user.manage")
    repo = UserRepo(session)
    u = await repo.get_by_id(user_id)
    if not u or u.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="user not found")
    updates = body.model_dump(exclude_unset=True, exclude={"custom_role_ids"})
    if updates:
        await repo.update(u, **updates)
    await AuditRepo(session).append(
        actor_id=user.user_id, actor_email=user.email,
        action="user.update", subject_type="user", subject_id=user_id,
        tenant_id=tenant_id, detail=updates,
    )
    await session.commit()
    return _user_dict(u)


@router.delete("/{user_id}")
async def disable_user(
    tenant_id: str, user_id: str,
    user: UserContext = Depends(tenant_scope),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if not has_permission(user.role, "user.manage", user.permissions):
        raise HTTPException(status_code=403, detail="missing permission: user.manage")
    repo = UserRepo(session)
    u = await repo.get_by_id(user_id)
    if not u or u.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="user not found")
    await repo.update(u, status="disabled")
    await AuditRepo(session).append(
        actor_id=user.user_id, actor_email=user.email,
        action="user.disable", subject_type="user", subject_id=user_id,
        tenant_id=tenant_id,
    )
    await session.commit()
    return {"id": user_id, "status": "disabled"}


@router.get("/permissions/list")
async def list_permissions(
    tenant_id: str,
    _user: UserContext = Depends(tenant_scope),
) -> dict:
    return {
        "permissions": [
            {"key": k, "description": v} for k, v in PERMISSIONS.items()
        ],
        "roles": {
            role: sorted(perms)
            for role, perms in ROLE_PERMISSIONS.items()
        },
    }


# ── custom roles ────────────────────────────────────────────────────

roles_router = APIRouter(prefix="/api/tenants/{tenant_id}/roles", tags=["roles"])


class CreateRoleBody(BaseModel):
    name: str
    permissions: list[str] = []


class UpdateRoleBody(BaseModel):
    name: Optional[str] = None
    permissions: Optional[list[str]] = None


def _role_dict(r) -> dict:
    return {
        "id": r.id,
        "tenant_id": r.tenant_id,
        "name": r.name,
        "permissions": r.permissions or [],
        "created_by": r.created_by,
        "created_at": r.created_at.isoformat(),
    }


@roles_router.get("/")
async def list_roles(
    tenant_id: str,
    _user: UserContext = Depends(tenant_scope),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    items = await CustomRoleRepo(session).list_by_tenant(tenant_id)
    return [_role_dict(r) for r in items]


@roles_router.post("/")
async def create_role(
    tenant_id: str,
    body: CreateRoleBody,
    user: UserContext = Depends(tenant_scope),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if not has_permission(user.role, "role.manage", user.permissions):
        raise HTTPException(status_code=403, detail="missing permission: role.manage")
    # Validate permissions don't exceed tenant_owner ceiling
    tenant_owner_perms = ROLE_PERMISSIONS.get("tenant_owner", set())
    invalid = set(body.permissions) - tenant_owner_perms
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"permissions exceed tenant_owner ceiling: {', '.join(sorted(invalid))}",
        )
    role = await CustomRoleRepo(session).create(
        tenant_id=tenant_id, name=body.name,
        permissions=body.permissions, created_by=user.user_id,
    )
    await AuditRepo(session).append(
        actor_id=user.user_id, actor_email=user.email,
        action="role.create", subject_type="custom_role", subject_id=role.id,
        tenant_id=tenant_id, detail={"name": body.name, "permissions": body.permissions},
    )
    await session.commit()
    return _role_dict(role)


@roles_router.patch("/{role_id}")
async def update_role(
    tenant_id: str, role_id: str,
    body: UpdateRoleBody,
    user: UserContext = Depends(tenant_scope),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if not has_permission(user.role, "role.manage", user.permissions):
        raise HTTPException(status_code=403, detail="missing permission: role.manage")
    repo = CustomRoleRepo(session)
    role = await repo.get_by_id(role_id)
    if not role or role.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="role not found")
    updates = body.model_dump(exclude_unset=True)
    if "permissions" in updates:
        tenant_owner_perms = ROLE_PERMISSIONS.get("tenant_owner", set())
        invalid = set(updates["permissions"]) - tenant_owner_perms
        if invalid:
            raise HTTPException(
                status_code=400,
                detail=f"permissions exceed tenant_owner ceiling: {', '.join(sorted(invalid))}",
            )
    if updates:
        await repo.update(role, **updates)
    await session.commit()
    return _role_dict(role)


@roles_router.delete("/{role_id}")
async def delete_role(
    tenant_id: str, role_id: str,
    user: UserContext = Depends(tenant_scope),
    session: AsyncSession = Depends(get_session),
) -> dict:
    if not has_permission(user.role, "role.manage", user.permissions):
        raise HTTPException(status_code=403, detail="missing permission: role.manage")
    repo = CustomRoleRepo(session)
    role = await repo.get_by_id(role_id)
    if not role or role.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="role not found")
    await repo.delete(role)
    await AuditRepo(session).append(
        actor_id=user.user_id, actor_email=user.email,
        action="role.delete", subject_type="custom_role", subject_id=role_id,
        tenant_id=tenant_id, detail={"name": role.name},
    )
    await session.commit()
    return {"deleted": True, "id": role_id}
