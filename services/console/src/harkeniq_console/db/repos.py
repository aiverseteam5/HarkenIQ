"""Thin data-access helpers. Each repo wraps one AsyncSession.

Commit responsibility stays with the caller (one commit per API
request) so multi-table updates remain atomic.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_console.db.models import (
    ConsoleAuditLog,
    CustomRole,
    PlatformSetting,
    Tenant,
    User,
    UserCustomRole,
    utcnow,
)


class TenantRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, tenant_id: str) -> Optional[Tenant]:
        return await self.session.get(Tenant, tenant_id)

    async def get_by_slug(self, slug: str) -> Optional[Tenant]:
        return (
            await self.session.execute(
                select(Tenant).where(Tenant.slug == slug)
            )
        ).scalar_one_or_none()

    async def list_filtered(
        self,
        search: Optional[str] = None,
        status: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[Sequence[Tenant], int]:
        stmt = select(Tenant)
        count_stmt = select(func.count()).select_from(Tenant)
        if status:
            stmt = stmt.where(Tenant.status == status)
            count_stmt = count_stmt.where(Tenant.status == status)
        if search:
            pattern = f"%{search}%"
            flt = or_(Tenant.name.ilike(pattern), Tenant.slug.ilike(pattern))
            stmt = stmt.where(flt)
            count_stmt = count_stmt.where(flt)
        total = (await self.session.execute(count_stmt)).scalar_one()
        stmt = stmt.order_by(Tenant.created_at).offset((page - 1) * page_size).limit(page_size)
        items = (await self.session.execute(stmt)).scalars().all()
        return items, total

    async def create(self, **kwargs) -> Tenant:
        tenant = Tenant(**kwargs)
        self.session.add(tenant)
        await self.session.flush()
        return tenant

    async def update(self, tenant: Tenant, **kwargs) -> Tenant:
        for key, value in kwargs.items():
            if hasattr(tenant, key):
                setattr(tenant, key, value)
        tenant.updated_at = utcnow()
        await self.session.flush()
        return tenant

    async def count(self) -> int:
        return (
            await self.session.execute(select(func.count()).select_from(Tenant))
        ).scalar_one()


class UserRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, user_id: str) -> Optional[User]:
        return await self.session.get(User, user_id)

    async def get_by_email(self, tenant_id: str, email: str) -> Optional[User]:
        return (
            await self.session.execute(
                select(User).where(User.tenant_id == tenant_id, User.email == email)
            )
        ).scalar_one_or_none()

    async def list_by_tenant(
        self,
        tenant_id: str,
        search: Optional[str] = None,
        role: Optional[str] = None,
        status: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[Sequence[User], int]:
        stmt = select(User).where(User.tenant_id == tenant_id)
        count_stmt = select(func.count()).select_from(User).where(User.tenant_id == tenant_id)
        if role:
            stmt = stmt.where(User.role == role)
            count_stmt = count_stmt.where(User.role == role)
        if status:
            stmt = stmt.where(User.status == status)
            count_stmt = count_stmt.where(User.status == status)
        if search:
            pattern = f"%{search}%"
            flt = or_(User.email.ilike(pattern), User.display_name.ilike(pattern))
            stmt = stmt.where(flt)
            count_stmt = count_stmt.where(flt)
        total = (await self.session.execute(count_stmt)).scalar_one()
        stmt = stmt.order_by(User.created_at).offset((page - 1) * page_size).limit(page_size)
        items = (await self.session.execute(stmt)).scalars().all()
        return items, total

    async def create(self, **kwargs) -> User:
        user = User(**kwargs)
        self.session.add(user)
        await self.session.flush()
        return user

    async def update(self, user: User, **kwargs) -> User:
        for key, value in kwargs.items():
            if hasattr(user, key):
                setattr(user, key, value)
        await self.session.flush()
        return user

    async def count_by_tenant(self, tenant_id: str) -> int:
        return (
            await self.session.execute(
                select(func.count()).select_from(User).where(User.tenant_id == tenant_id)
            )
        ).scalar_one()


class CustomRoleRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, role_id: str) -> Optional[CustomRole]:
        return await self.session.get(CustomRole, role_id)

    async def list_by_tenant(self, tenant_id: str) -> Sequence[CustomRole]:
        return (
            await self.session.execute(
                select(CustomRole).where(CustomRole.tenant_id == tenant_id)
            )
        ).scalars().all()

    async def create(self, **kwargs) -> CustomRole:
        role = CustomRole(**kwargs)
        self.session.add(role)
        await self.session.flush()
        return role

    async def update(self, role: CustomRole, **kwargs) -> CustomRole:
        for key, value in kwargs.items():
            if hasattr(role, key):
                setattr(role, key, value)
        await self.session.flush()
        return role

    async def delete(self, role: CustomRole) -> None:
        await self.session.delete(role)
        await self.session.flush()

    async def get_user_custom_roles(self, user_id: str) -> Sequence[CustomRole]:
        return (
            await self.session.execute(
                select(CustomRole)
                .join(UserCustomRole, UserCustomRole.custom_role_id == CustomRole.id)
                .where(UserCustomRole.user_id == user_id)
            )
        ).scalars().all()


class AuditRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def append(
        self,
        actor_id: Optional[str],
        actor_email: str,
        action: str,
        subject_type: str = "",
        subject_id: str = "",
        tenant_id: Optional[str] = None,
        detail: Optional[dict] = None,
    ) -> ConsoleAuditLog:
        row = ConsoleAuditLog(
            actor_id=actor_id,
            actor_email=actor_email,
            action=action,
            subject_type=subject_type,
            subject_id=subject_id,
            tenant_id=tenant_id,
            detail=detail,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def list_filtered(
        self,
        tenant_id: Optional[str] = None,
        actor: Optional[str] = None,
        action: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        search: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[Sequence[ConsoleAuditLog], int]:
        stmt = select(ConsoleAuditLog)
        count_stmt = select(func.count()).select_from(ConsoleAuditLog)
        if tenant_id:
            stmt = stmt.where(ConsoleAuditLog.tenant_id == tenant_id)
            count_stmt = count_stmt.where(ConsoleAuditLog.tenant_id == tenant_id)
        if actor:
            pattern = f"%{actor}%"
            flt = ConsoleAuditLog.actor_email.ilike(pattern)
            stmt = stmt.where(flt)
            count_stmt = count_stmt.where(flt)
        if action:
            stmt = stmt.where(ConsoleAuditLog.action == action)
            count_stmt = count_stmt.where(ConsoleAuditLog.action == action)
        if date_from:
            stmt = stmt.where(ConsoleAuditLog.ts >= date_from)
            count_stmt = count_stmt.where(ConsoleAuditLog.ts >= date_from)
        if date_to:
            stmt = stmt.where(ConsoleAuditLog.ts <= date_to)
            count_stmt = count_stmt.where(ConsoleAuditLog.ts <= date_to)
        if search:
            pattern = f"%{search}%"
            flt = or_(
                ConsoleAuditLog.action.ilike(pattern),
                ConsoleAuditLog.subject_type.ilike(pattern),
            )
            stmt = stmt.where(flt)
            count_stmt = count_stmt.where(flt)
        total = (await self.session.execute(count_stmt)).scalar_one()
        stmt = stmt.order_by(ConsoleAuditLog.ts.desc()).offset((page - 1) * page_size).limit(page_size)
        items = (await self.session.execute(stmt)).scalars().all()
        return items, total


class SettingsRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, key: str) -> Optional[PlatformSetting]:
        return await self.session.get(PlatformSetting, key)

    async def set(self, key: str, value, updated_by: Optional[str] = None) -> PlatformSetting:
        setting = await self.get(key)
        if setting is None:
            setting = PlatformSetting(key=key, value=value, updated_by=updated_by)
            self.session.add(setting)
        else:
            setting.value = value
            setting.updated_by = updated_by
            setting.updated_at = utcnow()
        await self.session.flush()
        return setting
