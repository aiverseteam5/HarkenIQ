"""Thin data-access helpers. Each repo wraps one AsyncSession.

Commit responsibility stays with the caller (one commit per API request)
so multi-table updates remain atomic.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_cc.db.models import (
    CCApprovalRoute,
    CCAuditLog,
    CCFleetCache,
    CCSite,
    CCUsageSnapshot,
    utcnow,
)


class SiteRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, site_id: str) -> Optional[CCSite]:
        return await self.session.get(CCSite, site_id)

    async def get_by_name(self, tenant_id: str, name: str) -> Optional[CCSite]:
        return (
            await self.session.execute(
                select(CCSite).where(
                    CCSite.tenant_id == tenant_id, CCSite.site_name == name
                )
            )
        ).scalar_one_or_none()

    async def list_all(self, tenant_id: str) -> Sequence[CCSite]:
        return (
            await self.session.execute(
                select(CCSite)
                .where(CCSite.tenant_id == tenant_id)
                .order_by(CCSite.site_name)
            )
        ).scalars().all()

    async def upsert(
        self,
        tenant_id: str,
        site_name: str,
        sm_endpoint: str,
        sm_token: Optional[str] = None,
        license_fingerprint: str = "",
    ) -> CCSite:
        site = await self.get_by_name(tenant_id, site_name)
        if site is None:
            site = CCSite(
                tenant_id=tenant_id,
                site_name=site_name,
                sm_endpoint=sm_endpoint,
            )
            self.session.add(site)
        site.sm_endpoint = sm_endpoint
        if sm_token is not None:
            site.sm_token = sm_token
        site.license_fingerprint = license_fingerprint or site.license_fingerprint
        site.last_seen_at = utcnow()
        await self.session.flush()
        return site

    async def update_last_seen(self, site: CCSite) -> None:
        site.last_seen_at = utcnow()
        await self.session.flush()


class FleetCacheRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert_device(
        self,
        site_id: str,
        agent_id: str,
        agent_name: str = "",
        vendor: str = "",
        model: str = "",
        observation: str = "",
        health: str = "",
        subsystems: Optional[dict] = None,
    ) -> CCFleetCache:
        row = (
            await self.session.execute(
                select(CCFleetCache).where(
                    CCFleetCache.site_id == site_id,
                    CCFleetCache.agent_id == agent_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = CCFleetCache(site_id=site_id, agent_id=agent_id)
            self.session.add(row)
        row.agent_name = agent_name or row.agent_name
        row.vendor = vendor or row.vendor
        row.model = model or row.model
        row.observation = observation or row.observation
        row.health = health or row.health
        if subsystems is not None:
            row.subsystems = subsystems
        row.snapshot_at = utcnow()
        await self.session.flush()
        return row

    async def list_by_site(self, site_id: str) -> Sequence[CCFleetCache]:
        return (
            await self.session.execute(
                select(CCFleetCache)
                .where(CCFleetCache.site_id == site_id)
                .order_by(CCFleetCache.agent_name)
            )
        ).scalars().all()

    async def list_all(self, tenant_id: str) -> Sequence[CCFleetCache]:
        return (
            await self.session.execute(
                select(CCFleetCache)
                .join(CCSite, CCSite.id == CCFleetCache.site_id)
                .where(CCSite.tenant_id == tenant_id)
                .order_by(CCFleetCache.agent_name)
            )
        ).scalars().all()

    async def clear_site(self, site_id: str) -> None:
        rows = (
            await self.session.execute(
                select(CCFleetCache).where(CCFleetCache.site_id == site_id)
            )
        ).scalars().all()
        for row in rows:
            await self.session.delete(row)
        await self.session.flush()


class ApprovalRouteRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        site_id: str,
        action_id: str,
        action_type: str = "",
        device_agent_id: str = "",
    ) -> CCApprovalRoute:
        row = CCApprovalRoute(
            site_id=site_id,
            action_id=action_id,
            action_type=action_type,
            device_agent_id=device_agent_id,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def get_by_action_id(self, action_id: str) -> Optional[CCApprovalRoute]:
        return (
            await self.session.execute(
                select(CCApprovalRoute).where(
                    CCApprovalRoute.action_id == action_id
                )
            )
        ).scalar_one_or_none()

    async def list_pending(self, tenant_id: str) -> Sequence[CCApprovalRoute]:
        return (
            await self.session.execute(
                select(CCApprovalRoute)
                .join(CCSite, CCSite.id == CCApprovalRoute.site_id)
                .where(CCSite.tenant_id == tenant_id)
                .where(CCApprovalRoute.decision.is_(None))
                .order_by(CCApprovalRoute.routed_at)
            )
        ).scalars().all()

    async def list_history(self, tenant_id: str) -> Sequence[CCApprovalRoute]:
        return (
            await self.session.execute(
                select(CCApprovalRoute)
                .join(CCSite, CCSite.id == CCApprovalRoute.site_id)
                .where(CCSite.tenant_id == tenant_id)
                .where(CCApprovalRoute.decision.isnot(None))
                .order_by(CCApprovalRoute.decided_at.desc())
            )
        ).scalars().all()

    async def update_decision(
        self,
        route: CCApprovalRoute,
        decision: str,
        decided_by: str,
    ) -> None:
        route.decision = decision
        route.decided_by = decided_by
        route.decided_at = utcnow()
        await self.session.flush()


class UsageSnapshotRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert(
        self,
        date: str,
        site_id: str,
        tenant_id: str,
        node_count: int,
        agent_versions: Optional[dict] = None,
    ) -> CCUsageSnapshot:
        row = await self.session.get(CCUsageSnapshot, (date, site_id))
        if row is None:
            row = CCUsageSnapshot(date=date, site_id=site_id, tenant_id=tenant_id)
            self.session.add(row)
        row.node_count = node_count
        if agent_versions is not None:
            row.agent_versions = agent_versions
        row.reported_at = utcnow()
        await self.session.flush()
        return row

    async def get_by_month(
        self, tenant_id: str, year: int, month: int
    ) -> Sequence[CCUsageSnapshot]:
        prefix = f"{year:04d}-{month:02d}"
        return (
            await self.session.execute(
                select(CCUsageSnapshot)
                .where(CCUsageSnapshot.tenant_id == tenant_id)
                .where(CCUsageSnapshot.date.startswith(prefix))
                .order_by(CCUsageSnapshot.date)
            )
        ).scalars().all()

    async def get_by_date(
        self, tenant_id: str, date: str
    ) -> Sequence[CCUsageSnapshot]:
        return (
            await self.session.execute(
                select(CCUsageSnapshot)
                .where(CCUsageSnapshot.tenant_id == tenant_id)
                .where(CCUsageSnapshot.date == date)
            )
        ).scalars().all()


class AuditRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def append(
        self,
        actor: str,
        action: str,
        subject: str = "",
        tenant_id: str = "",
        detail: Optional[dict] = None,
    ) -> CCAuditLog:
        row = CCAuditLog(
            actor=actor,
            action=action,
            subject=subject,
            tenant_id=tenant_id,
            detail=detail,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def list_filtered(
        self,
        tenant_id: str,
        actor: Optional[str] = None,
        action: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> Sequence[CCAuditLog]:
        stmt = (
            select(CCAuditLog)
            .where(CCAuditLog.tenant_id == tenant_id)
        )
        if actor is not None:
            stmt = stmt.where(CCAuditLog.actor == actor)
        if action is not None:
            stmt = stmt.where(CCAuditLog.action == action)
        if date_from is not None:
            stmt = stmt.where(CCAuditLog.ts >= date_from)
        if date_to is not None:
            stmt = stmt.where(CCAuditLog.ts <= date_to)
        stmt = stmt.order_by(CCAuditLog.ts.desc())
        stmt = stmt.offset((page - 1) * page_size).limit(page_size)
        return (await self.session.execute(stmt)).scalars().all()
