"""User and custom role management endpoints."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/tenants/{tenant_id}/users", tags=["users"])


@router.post("/invite")
async def invite_user(tenant_id: str) -> dict:
    return {"stub": "invite_user", "tenant_id": tenant_id}


@router.get("/")
async def list_users(tenant_id: str) -> dict:
    return {"stub": "list_users", "tenant_id": tenant_id}


@router.get("/{user_id}")
async def get_user(tenant_id: str, user_id: str) -> dict:
    return {"stub": "get_user", "tenant_id": tenant_id, "user_id": user_id}


@router.patch("/{user_id}")
async def update_user(tenant_id: str, user_id: str) -> dict:
    return {"stub": "update_user", "tenant_id": tenant_id, "user_id": user_id}


@router.delete("/{user_id}")
async def delete_user(tenant_id: str, user_id: str) -> dict:
    return {"stub": "delete_user", "tenant_id": tenant_id, "user_id": user_id}


# ── custom roles ────────────────────────────────────────────────────

roles_router = APIRouter(prefix="/api/tenants/{tenant_id}/roles", tags=["roles"])


@roles_router.post("/")
async def create_role(tenant_id: str) -> dict:
    return {"stub": "create_role", "tenant_id": tenant_id}


@roles_router.get("/")
async def list_roles(tenant_id: str) -> dict:
    return {"stub": "list_roles", "tenant_id": tenant_id}


@roles_router.get("/{role_id}")
async def get_role(tenant_id: str, role_id: str) -> dict:
    return {"stub": "get_role", "tenant_id": tenant_id, "role_id": role_id}


@roles_router.patch("/{role_id}")
async def update_role(tenant_id: str, role_id: str) -> dict:
    return {"stub": "update_role", "tenant_id": tenant_id, "role_id": role_id}


@roles_router.delete("/{role_id}")
async def delete_role(tenant_id: str, role_id: str) -> dict:
    return {"stub": "delete_role", "tenant_id": tenant_id, "role_id": role_id}
