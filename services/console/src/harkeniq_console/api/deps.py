"""Shared FastAPI dependencies."""

from __future__ import annotations

from functools import wraps
from typing import Callable

from fastapi import Depends, HTTPException, Request

from harkeniq_console.auth import UserContext, get_current_user
from harkeniq_console.permissions import has_permission


def get_console_state(request: Request):
    return request.app.state.console


async def get_session(request: Request):
    """Yield an AsyncSession from the runtime sessionmaker."""
    state = request.app.state.console
    async with state.sessionmaker() as session:
        yield session


def require_role(*roles: str) -> Callable:
    """Dependency that checks the authenticated user has one of *roles*."""
    async def _check(user: UserContext = Depends(get_current_user)) -> UserContext:
        if user.role not in roles:
            raise HTTPException(
                status_code=403,
                detail=f"requires one of roles: {', '.join(roles)}",
            )
        return user
    return _check


def require_permission(permission: str) -> Callable:
    """Dependency that checks the authenticated user has *permission*."""
    async def _check(user: UserContext = Depends(get_current_user)) -> UserContext:
        if not has_permission(user.role, permission, user.permissions):
            raise HTTPException(status_code=403, detail=f"missing permission: {permission}")
        return user
    return _check


async def require_super_admin(
    user: UserContext = Depends(get_current_user),
) -> UserContext:
    """Shortcut: only platform_super_admin may proceed."""
    if user.role != "platform_super_admin":
        raise HTTPException(status_code=403, detail="requires platform_super_admin")
    return user


async def tenant_scope(
    tenant_id: str,
    user: UserContext = Depends(get_current_user),
) -> UserContext:
    """Validate that the user belongs to *tenant_id* or is a platform user."""
    if user.is_platform_user:
        return user
    if user.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="tenant scope mismatch")
    return user
