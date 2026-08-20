"""Thin data-access helpers. Each repo wraps one AsyncSession.

Commit responsibility stays with the caller (one commit per API request)
so multi-table updates remain atomic.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_cc.db.models import (
    CCApprovalGroup,
    CCApprovalGroupMember,
    CCApprovalPolicy,
    CCApprovalRoute,
    CCAuditLog,
    CCAutonomyBudget,
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

    async def count(self, tenant_id: str) -> int:
        result = await self.session.execute(
            select(func.count(CCSite.id)).where(CCSite.tenant_id == tenant_id)
        )
        return result.scalar() or 0


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

    async def list_filtered(
        self,
        tenant_id: str,
        site_id: Optional[str] = None,
        vendor: Optional[str] = None,
        health: Optional[str] = None,
        search: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[Sequence[CCFleetCache], int]:
        """Return paginated devices with total count, optionally filtered."""
        stmt = (
            select(CCFleetCache)
            .join(CCSite, CCSite.id == CCFleetCache.site_id)
            .where(CCSite.tenant_id == tenant_id)
        )
        count_stmt = (
            select(func.count(CCFleetCache.id))
            .join(CCSite, CCSite.id == CCFleetCache.site_id)
            .where(CCSite.tenant_id == tenant_id)
        )
        if site_id:
            stmt = stmt.where(CCFleetCache.site_id == site_id)
            count_stmt = count_stmt.where(CCFleetCache.site_id == site_id)
        if vendor:
            stmt = stmt.where(CCFleetCache.vendor == vendor)
            count_stmt = count_stmt.where(CCFleetCache.vendor == vendor)
        if health:
            stmt = stmt.where(CCFleetCache.health == health)
            count_stmt = count_stmt.where(CCFleetCache.health == health)
        if search:
            pattern = f"%{search}%"
            stmt = stmt.where(
                CCFleetCache.agent_name.ilike(pattern)
                | CCFleetCache.agent_id.ilike(pattern)
                | CCFleetCache.model.ilike(pattern)
            )
            count_stmt = count_stmt.where(
                CCFleetCache.agent_name.ilike(pattern)
                | CCFleetCache.agent_id.ilike(pattern)
                | CCFleetCache.model.ilike(pattern)
            )
        total = (await self.session.execute(count_stmt)).scalar() or 0
        stmt = stmt.order_by(CCFleetCache.agent_name)
        stmt = stmt.offset((page - 1) * page_size).limit(page_size)
        rows = (await self.session.execute(stmt)).scalars().all()
        return rows, total

    async def get_by_agent_id(self, agent_id: str) -> Optional[CCFleetCache]:
        return (
            await self.session.execute(
                select(CCFleetCache).where(CCFleetCache.agent_id == agent_id)
            )
        ).scalar_one_or_none()

    async def count_by_health(self, tenant_id: str) -> dict[str, int]:
        """Return {health_status: count} for all devices in the tenant."""
        stmt = (
            select(CCFleetCache.health, func.count(CCFleetCache.id))
            .join(CCSite, CCSite.id == CCFleetCache.site_id)
            .where(CCSite.tenant_id == tenant_id)
            .group_by(CCFleetCache.health)
        )
        rows = (await self.session.execute(stmt)).all()
        return {row[0]: row[1] for row in rows}

    async def count_total(self, tenant_id: str) -> int:
        result = await self.session.execute(
            select(func.count(CCFleetCache.id))
            .join(CCSite, CCSite.id == CCFleetCache.site_id)
            .where(CCSite.tenant_id == tenant_id)
        )
        return result.scalar() or 0

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

    async def list_pending_paginated(
        self, tenant_id: str, page: int = 1, page_size: int = 50,
    ) -> tuple[Sequence[CCApprovalRoute], int]:
        base = (
            select(CCApprovalRoute)
            .join(CCSite, CCSite.id == CCApprovalRoute.site_id)
            .where(CCSite.tenant_id == tenant_id)
            .where(CCApprovalRoute.decision.is_(None))
        )
        total = (
            await self.session.execute(
                select(func.count()).select_from(base.subquery())
            )
        ).scalar() or 0
        rows = (
            await self.session.execute(
                base.order_by(CCApprovalRoute.routed_at)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).scalars().all()
        return rows, total

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

    async def list_history_paginated(
        self, tenant_id: str, page: int = 1, page_size: int = 50,
    ) -> tuple[Sequence[CCApprovalRoute], int]:
        base = (
            select(CCApprovalRoute)
            .join(CCSite, CCSite.id == CCApprovalRoute.site_id)
            .where(CCSite.tenant_id == tenant_id)
            .where(CCApprovalRoute.decision.isnot(None))
        )
        total = (
            await self.session.execute(
                select(func.count()).select_from(base.subquery())
            )
        ).scalar() or 0
        rows = (
            await self.session.execute(
                base.order_by(CCApprovalRoute.decided_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).scalars().all()
        return rows, total

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

    async def mark_delivered(self, route: CCApprovalRoute) -> None:
        route.delivered_at = utcnow()
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


# ---------------------------------------------------------------------------
# Phase 3 repos: approval policies, groups, autonomy budgets
# ---------------------------------------------------------------------------


class ApprovalPolicyRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_all(self, tenant_id: str) -> Sequence[CCApprovalPolicy]:
        return (
            await self.session.execute(
                select(CCApprovalPolicy)
                .where(CCApprovalPolicy.tenant_id == tenant_id)
                .order_by(CCApprovalPolicy.name)
            )
        ).scalars().all()

    async def get_by_id(self, policy_id: str) -> Optional[CCApprovalPolicy]:
        return await self.session.get(CCApprovalPolicy, policy_id)

    async def create(
        self,
        tenant_id: str,
        name: str,
        created_by: str,
        device_type: str = "*",
        action_type: str = "*",
        risk_level: str = "medium",
        time_window_json: Optional[dict] = None,
        approval_mode: str = "require_approval",
        required_approvers: int = 1,
        group_id: Optional[str] = None,
    ) -> CCApprovalPolicy:
        row = CCApprovalPolicy(
            tenant_id=tenant_id,
            name=name,
            device_type=device_type,
            action_type=action_type,
            risk_level=risk_level,
            time_window_json=time_window_json,
            approval_mode=approval_mode,
            required_approvers=required_approvers,
            group_id=group_id,
            created_by=created_by,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def update(
        self,
        policy: CCApprovalPolicy,
        **fields,
    ) -> None:
        for key, value in fields.items():
            if hasattr(policy, key) and value is not None:
                setattr(policy, key, value)
        policy.updated_at = utcnow()
        await self.session.flush()

    async def delete(self, policy: CCApprovalPolicy) -> None:
        await self.session.delete(policy)
        await self.session.flush()


class ApprovalGroupRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_all(self, tenant_id: str) -> Sequence[CCApprovalGroup]:
        return (
            await self.session.execute(
                select(CCApprovalGroup)
                .where(CCApprovalGroup.tenant_id == tenant_id)
                .order_by(CCApprovalGroup.name)
            )
        ).scalars().all()

    async def get_by_id(self, group_id: str) -> Optional[CCApprovalGroup]:
        return await self.session.get(CCApprovalGroup, group_id)

    async def create(
        self,
        tenant_id: str,
        name: str,
        created_by: str,
        slack_channel: str = "",
        github_team: str = "",
        required_count: int = 1,
        escalation_chain: Optional[dict] = None,
    ) -> CCApprovalGroup:
        row = CCApprovalGroup(
            tenant_id=tenant_id,
            name=name,
            slack_channel=slack_channel,
            github_team=github_team,
            required_count=required_count,
            escalation_chain=escalation_chain,
            created_by=created_by,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def update(self, group: CCApprovalGroup, **fields) -> None:
        for key, value in fields.items():
            if hasattr(group, key) and value is not None:
                setattr(group, key, value)
        await self.session.flush()

    async def delete(self, group: CCApprovalGroup) -> None:
        # Delete members first
        members = (
            await self.session.execute(
                select(CCApprovalGroupMember).where(
                    CCApprovalGroupMember.group_id == group.id
                )
            )
        ).scalars().all()
        for m in members:
            await self.session.delete(m)
        await self.session.delete(group)
        await self.session.flush()

    async def list_members(self, group_id: str) -> Sequence[CCApprovalGroupMember]:
        return (
            await self.session.execute(
                select(CCApprovalGroupMember)
                .where(CCApprovalGroupMember.group_id == group_id)
                .order_by(CCApprovalGroupMember.user_email)
            )
        ).scalars().all()

    async def add_member(
        self, group_id: str, user_email: str, role: str = "approver"
    ) -> CCApprovalGroupMember:
        row = CCApprovalGroupMember(
            group_id=group_id, user_email=user_email, role=role,
        )
        self.session.add(row)
        await self.session.flush()
        return row


class AutonomyBudgetRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_all(self, tenant_id: str) -> Sequence[CCAutonomyBudget]:
        return (
            await self.session.execute(
                select(CCAutonomyBudget)
                .where(CCAutonomyBudget.tenant_id == tenant_id)
                .order_by(CCAutonomyBudget.device_type)
            )
        ).scalars().all()

    async def get_by_id(self, budget_id: str) -> Optional[CCAutonomyBudget]:
        return await self.session.get(CCAutonomyBudget, budget_id)

    async def upsert(
        self,
        tenant_id: str,
        device_type: str = "*",
        level: int = 0,
        budget_limit: int = 0,
        budget_period: str = "monthly",
        learning_ramp_config: Optional[dict] = None,
    ) -> CCAutonomyBudget:
        """Create or update an autonomy budget for the tenant + device_type."""
        row = (
            await self.session.execute(
                select(CCAutonomyBudget).where(
                    CCAutonomyBudget.tenant_id == tenant_id,
                    CCAutonomyBudget.device_type == device_type,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = CCAutonomyBudget(
                tenant_id=tenant_id,
                device_type=device_type,
            )
            self.session.add(row)
        row.level = level
        row.budget_limit = budget_limit
        row.budget_period = budget_period
        if learning_ramp_config is not None:
            row.learning_ramp_config = learning_ramp_config
        row.updated_at = utcnow()
        await self.session.flush()
        return row

    async def delete(self, budget: CCAutonomyBudget) -> None:
        await self.session.delete(budget)
        await self.session.flush()
