"""Thin data-access helpers. Each repo wraps one AsyncSession.

Commit responsibility stays with the caller (one commit per API request)
so multi-table updates remain atomic.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
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
    CCCandidateSkill,
    CCCveEntry,
    CCFleetCache,
    CCFleetPattern,
    CCIncident,
    CCLearnedSignal,
    CCLearningCycle,
    CCOutcomeHistory,
    CCSafetyState,
    CCSite,
    CCSkillDelivery,
    CCStopSwitch,
    CCUsageSnapshot,
    CCWarranty,
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
        service_tag: str = "",
        firmware: Optional[list] = None,
        device_class: str = "",
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
        if device_class:
            row.device_class = device_class
        row.observation = observation or row.observation
        row.health = health or row.health
        if subsystems is not None:
            row.subsystems = subsystems
        row.service_tag = service_tag or row.service_tag
        if firmware is not None:
            row.firmware = firmware
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

    async def list_undecided_for_site(
        self, site_id: str
    ) -> Sequence[CCApprovalRoute]:
        """Undecided routes for one site — the reconciliation set the
        fleet poller compares against the SM's current pending list."""
        return (
            await self.session.execute(
                select(CCApprovalRoute)
                .where(CCApprovalRoute.site_id == site_id)
                .where(CCApprovalRoute.decision.is_(None))
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


#: Serializes hash-chain appends within this process (R4-2 P12); the
#: UNIQUE constraint on cc_audit_log.seq is the cross-process backstop.
_audit_chain_lock = asyncio.Lock()


class AuditRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _chain_ts(ts) -> str:
        """Timezone-stable timestamp string for hashing.

        sqlite returns naive datetimes for values written tz-aware, so
        normalize to naive UTC before formatting -- the payload string
        must be identical at write time and at verify time.
        """
        from datetime import timezone as _tz
        if ts.tzinfo is not None:
            ts = ts.astimezone(_tz.utc).replace(tzinfo=None)
        return ts.isoformat()

    @staticmethod
    def _chain_payload(row: CCAuditLog) -> dict:
        return {
            "ts": AuditRepo._chain_ts(row.ts),
            "actor": row.actor,
            "action": row.action,
            "subject": row.subject,
            "tenant_id": row.tenant_id,
            "detail": row.detail,
        }

    async def append(
        self,
        actor: str,
        action: str,
        subject: str = "",
        tenant_id: str = "",
        detail: Optional[dict] = None,
    ) -> CCAuditLog:
        from harkeniq.audit.chain import next_link, pg_advisory_chain_lock

        row = CCAuditLog(
            ts=utcnow(),
            actor=actor,
            action=action,
            subject=subject,
            tenant_id=tenant_id,
            detail=detail,
        )
        async with _audit_chain_lock:
            # R5-2: cross-replica serialization on PostgreSQL (held
            # through the caller's commit); no-op on sqlite.
            await pg_advisory_chain_lock(self.session, "cc.cc_audit_log")
            tail = (
                await self.session.execute(
                    select(CCAuditLog.seq, CCAuditLog.entry_hash)
                    .where(CCAuditLog.seq.isnot(None))
                    .order_by(CCAuditLog.seq.desc())
                    .limit(1)
                )
            ).first()
            row.seq, row.prev_hash, row.entry_hash = next_link(
                tail[0] if tail else 0,
                tail[1] if tail else None,
                self._chain_payload(row),
            )
            self.session.add(row)
            await self.session.flush()
        return row

    async def verify_chain(self):
        """Verify the audit hash chain (R4-2 P12); returns ChainVerification."""
        from harkeniq.audit.chain import verify_chain

        rows = (
            await self.session.execute(
                select(CCAuditLog)
                .where(CCAuditLog.seq.isnot(None))
                .order_by(CCAuditLog.seq)
            )
        ).scalars().all()
        return verify_chain(
            (r.seq, r.prev_hash, r.entry_hash, self._chain_payload(r))
            for r in rows
        )

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

    async def count_filtered(
        self,
        tenant_id: str,
        actor: Optional[str] = None,
        action: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> int:
        """True total for the same filter — P0 2026-08-29: the audit list
        endpoint reported the current page length as "total"."""
        stmt = select(func.count(CCAuditLog.id)).where(
            CCAuditLog.tenant_id == tenant_id
        )
        if actor is not None:
            stmt = stmt.where(CCAuditLog.actor == actor)
        if action is not None:
            stmt = stmt.where(CCAuditLog.action == action)
        if date_from is not None:
            stmt = stmt.where(CCAuditLog.ts >= date_from)
        if date_to is not None:
            stmt = stmt.where(CCAuditLog.ts <= date_to)
        return (await self.session.execute(stmt)).scalar() or 0


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

    async def get_member(self, member_id: str) -> Optional[CCApprovalGroupMember]:
        return await self.session.get(CCApprovalGroupMember, member_id)

    async def remove_member(self, member: CCApprovalGroupMember) -> None:
        await self.session.delete(member)
        await self.session.flush()


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


class StopSwitchRepo:
    """QA-022: persisted stop-switch state, one row per tenant."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, tenant_id: str) -> Optional[CCStopSwitch]:
        return (
            await self.session.execute(
                select(CCStopSwitch).where(CCStopSwitch.tenant_id == tenant_id)
            )
        ).scalar_one_or_none()

    async def is_active(self, tenant_id: str) -> bool:
        row = await self.get(tenant_id)
        return bool(row.active) if row is not None else False

    async def set(self, tenant_id: str, active: bool, changed_by: str) -> CCStopSwitch:
        row = await self.get(tenant_id)
        if row is None:
            row = CCStopSwitch(tenant_id=tenant_id)
            self.session.add(row)
        row.active = active
        row.changed_by = changed_by
        row.updated_at = utcnow()
        await self.session.flush()
        return row


class OutcomeHistoryRepo:
    """Read path for cc_outcome_history (R4-1: written by the fleet poller,
    read by the intelligence loop and the outcomes API)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_outcome_dicts(
        self,
        tenant_id: str,
        since: Optional[datetime] = None,
        limit: int = 10000,
    ) -> list[dict]:
        """Outcome rows as aggregator-ready dicts (site_id included).

        Tenant scoping goes through cc_sites: an outcome belongs to the
        tenant that owns the site it was polled from.
        """
        stmt = (
            select(CCOutcomeHistory)
            .join(CCSite, CCOutcomeHistory.site_id == CCSite.id)
            .where(CCSite.tenant_id == tenant_id)
            .order_by(CCOutcomeHistory.ingested_at)
            .limit(limit)
        )
        if since is not None:
            stmt = stmt.where(CCOutcomeHistory.ingested_at > since)
        rows = (await self.session.execute(stmt)).scalars().all()
        return [
            {
                "action_type": r.action_type,
                "vendor": r.vendor,
                "model": r.model,
                "outcome": r.outcome,
                "fault_resolved": bool(r.fault_resolved),
                "site_id": r.site_id,
                "ingested_at": r.ingested_at,
            }
            for r in rows
        ]

    async def count(self, tenant_id: str) -> int:
        result = await self.session.execute(
            select(func.count(CCOutcomeHistory.id))
            .join(CCSite, CCOutcomeHistory.site_id == CCSite.id)
            .where(CCSite.tenant_id == tenant_id)
        )
        return result.scalar() or 0

    async def list_device_outcome_dicts(
        self, tenant_id: str, limit: int = 50000
    ) -> list[dict]:
        """Per-device outcome rows for risk scoring (R4-3 P20).

        Uses the ix_outcome_history_device access path; returns dicts
        with device attribution and recorded_at for recency weighting.
        """
        stmt = (
            select(CCOutcomeHistory)
            .join(CCSite, CCOutcomeHistory.site_id == CCSite.id)
            .where(CCSite.tenant_id == tenant_id)
            .order_by(CCOutcomeHistory.recorded_at)
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return [
            {
                "device_agent_id": r.device_agent_id,
                "vendor": r.vendor,
                "model": r.model,
                "outcome": r.outcome,
                "recorded_at": r.recorded_at,
            }
            for r in rows
        ]


class FleetPatternRepo:
    """Persistence for detected fleet patterns (R4-1)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def save(self, pattern, tenant_id: str = "") -> CCFleetPattern:
        """Persist a pattern_detector.FleetPattern; idempotent on id."""
        from datetime import timezone

        row = await self.session.get(CCFleetPattern, pattern.pattern_id)
        if row is None:
            row = CCFleetPattern(id=pattern.pattern_id, tenant_id=tenant_id)
            self.session.add(row)
        row.tenant_id = tenant_id or row.tenant_id
        row.pattern_type = pattern.pattern_type
        row.description = pattern.description
        row.affected_scope = dict(pattern.affected_scope)
        row.confidence = pattern.confidence
        row.evidence = dict(pattern.evidence)
        row.detected_at = datetime.fromtimestamp(
            pattern.detected_at, tz=timezone.utc
        )
        await self.session.flush()
        return row

    async def list_patterns(
        self,
        pattern_type: Optional[str] = None,
        status: Optional[str] = "active",
        limit: int = 200,
        tenant_id: Optional[str] = None,
    ) -> Sequence[CCFleetPattern]:
        stmt = (
            select(CCFleetPattern)
            .order_by(CCFleetPattern.detected_at.desc())
            .limit(limit)
        )
        if tenant_id is not None:
            stmt = stmt.where(CCFleetPattern.tenant_id == tenant_id)
        if pattern_type:
            stmt = stmt.where(CCFleetPattern.pattern_type == pattern_type)
        if status:
            stmt = stmt.where(CCFleetPattern.status == status)
        return (await self.session.execute(stmt)).scalars().all()

    async def resolve(self, pattern_id: str) -> Optional[CCFleetPattern]:
        row = await self.session.get(CCFleetPattern, pattern_id)
        if row is None:
            return None
        row.status = "resolved"
        row.resolved_at = utcnow()
        await self.session.flush()
        return row


class CandidateSkillRepo:
    """SM-generated candidate skills for the R-C1 loop (QA-033)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert(
        self, tenant_id: str, site_id: str, cand: dict,
    ) -> CCCandidateSkill:
        """Ingest one FleetSnapshot candidate dict; idempotent on skill_id."""
        import json as _json
        from datetime import timezone

        row = await self.session.get(
            CCCandidateSkill, (cand.get("skill_id", ""), tenant_id)
        )
        if row is None:
            row = CCCandidateSkill(
                skill_id=cand.get("skill_id", ""), tenant_id=tenant_id
            )
            self.session.add(row)
        row.site_id = site_id
        row.yaml_text = cand.get("yaml_text", "")
        row.source_device = cand.get("source_device", "")
        row.source_component = cand.get("source_component", "")
        row.validation_state = cand.get("validation_state", "draft")
        try:
            row.warnings = _json.loads(cand.get("warnings_json") or "[]") or None
        except ValueError:
            row.warnings = None
        row.dry_run_matches = cand.get("dry_run_matches", 0)
        if cand.get("generated_at_unix"):
            row.generated_at = datetime.fromtimestamp(
                cand["generated_at_unix"], tz=timezone.utc
            )
        await self.session.flush()
        return row

    async def list_candidates(
        self,
        tenant_id: str,
        status: Optional[str] = None,
        limit: int = 200,
    ) -> Sequence[CCCandidateSkill]:
        stmt = (
            select(CCCandidateSkill)
            .where(CCCandidateSkill.tenant_id == tenant_id)
            .order_by(CCCandidateSkill.received_at.desc())
            .limit(limit)
        )
        if status:
            stmt = stmt.where(CCCandidateSkill.status == status)
        return (await self.session.execute(stmt)).scalars().all()

    async def link_cycle(
        self, tenant_id: str, skill_id: str, cycle_id: str,
    ) -> Optional[CCCandidateSkill]:
        row = await self.session.get(CCCandidateSkill, (skill_id, tenant_id))
        if row is None:
            return None
        row.cycle_id = cycle_id
        row.status = "cycle_linked"
        await self.session.flush()
        return row

    async def mark_promoted(
        self, tenant_id: str, skill_id: str,
    ) -> Optional[CCCandidateSkill]:
        row = await self.session.get(CCCandidateSkill, (skill_id, tenant_id))
        if row is None:
            return None
        row.status = "promoted"
        await self.session.flush()
        return row


class CveFeedRepo:
    """Local CVE feed persistence (R4-2 P14)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def import_entries(
        self, entries: list[dict], tenant_id: str = ""
    ) -> int:
        """Upsert feed entries keyed by (tenant, cve_id, vendor, component)."""
        count = 0
        for entry in entries:
            cve_id = str(entry.get("cve_id", "")).strip()
            affected = str(entry.get("affected_versions", "")).strip()
            if not cve_id or not affected:
                continue
            vendor = str(entry.get("vendor", "*")) or "*"
            component = str(entry.get("component", "*")) or "*"
            row = (
                await self.session.execute(
                    select(CCCveEntry).where(
                        CCCveEntry.tenant_id == tenant_id,
                        CCCveEntry.cve_id == cve_id,
                        CCCveEntry.vendor == vendor,
                        CCCveEntry.component == component,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                row = CCCveEntry(tenant_id=tenant_id, cve_id=cve_id,
                                 vendor=vendor, component=component,
                                 affected_versions=affected)
                self.session.add(row)
            row.affected_versions = affected
            row.fixed_version = str(entry.get("fixed_version", ""))
            row.severity = str(entry.get("severity", "medium"))
            row.description = str(entry.get("description", ""))[:512]
            row.published = str(entry.get("published", ""))
            row.imported_at = utcnow()
            count += 1
        await self.session.flush()
        return count

    async def list_all(self, tenant_id: str = "") -> Sequence[CCCveEntry]:
        return (
            await self.session.execute(
                select(CCCveEntry)
                .where(CCCveEntry.tenant_id == tenant_id)
                .order_by(CCCveEntry.cve_id)
            )
        ).scalars().all()


class WarrantyRepo:
    """Warranty cache persistence (R4-2 P15)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert_records(self, records: list, tenant_id: str = "") -> int:
        """Upsert WarrantyRecord-shaped objects; returns count stored."""
        count = 0
        for record in records:
            tag = getattr(record, "service_tag", "") or ""
            if not tag:
                continue
            row = await self.session.get(CCWarranty, (tenant_id, tag))
            if row is None:
                row = CCWarranty(tenant_id=tenant_id, service_tag=tag)
                self.session.add(row)
            row.vendor = getattr(record, "vendor", "") or row.vendor
            row.service_level = getattr(record, "service_level", "")
            row.start_date = getattr(record, "start_date", "")
            row.end_date = getattr(record, "end_date", "")
            row.source = getattr(record, "source", "")
            row.fetched_at = utcnow()
            count += 1
        await self.session.flush()
        return count

    async def get_map(
        self, service_tags: list[str], tenant_id: str = ""
    ) -> dict[str, CCWarranty]:
        tags = [t for t in service_tags if t]
        if not tags:
            return {}
        rows = (
            await self.session.execute(
                select(CCWarranty).where(
                    CCWarranty.tenant_id == tenant_id,
                    CCWarranty.service_tag.in_(tags),
                )
            )
        ).scalars().all()
        return {r.service_tag: r for r in rows}

    async def list_all(self, tenant_id: str = "") -> Sequence[CCWarranty]:
        return (
            await self.session.execute(
                select(CCWarranty)
                .where(CCWarranty.tenant_id == tenant_id)
                .order_by(CCWarranty.service_tag)
            )
        ).scalars().all()

    async def stale_or_missing_tags(
        self, service_tags: list[str], ttl_s: float, tenant_id: str = ""
    ) -> list[str]:
        """Tags with no cached record or a record older than the TTL."""
        from datetime import timedelta

        tags = sorted({t for t in service_tags if t})
        if not tags:
            return []
        cached = await self.get_map(tags, tenant_id=tenant_id)
        cutoff = utcnow() - timedelta(seconds=ttl_s)
        stale: list[str] = []
        for tag in tags:
            row = cached.get(tag)
            if row is None:
                stale.append(tag)
                continue
            fetched = row.fetched_at
            if fetched is not None and fetched.tzinfo is None:
                from datetime import timezone as _tz
                fetched = fetched.replace(tzinfo=_tz.utc)
            if fetched is None or fetched < cutoff:
                stale.append(tag)
        return stale


class SkillDeliveryRepo:
    """Marketplace delivery ledger (R5-2)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, install_id: str, site_id: str) -> Optional[CCSkillDelivery]:
        return await self.session.get(CCSkillDelivery, (install_id, site_id))

    async def record(
        self, install_id: str, site_id: str, skill_name: str,
        skill_version: str, status: str, directives_queued: int = 0,
        detail: str = "",
    ) -> CCSkillDelivery:
        row = await self.get(install_id, site_id)
        if row is None:
            row = CCSkillDelivery(install_id=install_id, site_id=site_id)
            self.session.add(row)
        row.skill_name = skill_name
        row.skill_version = skill_version
        row.status = status
        row.directives_queued = directives_queued
        row.detail = detail[:512]
        row.delivered_at = utcnow()
        await self.session.flush()
        return row

    async def list_all(self) -> Sequence[CCSkillDelivery]:
        return (
            await self.session.execute(
                select(CCSkillDelivery).order_by(CCSkillDelivery.delivered_at)
            )
        ).scalars().all()


class LearningCycleRepo:
    """Durable ledger of R-C1 learning cycles (S3)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert(self, tenant_id: str, entry) -> CCLearningCycle:
        """Persist a tracker cycle. Idempotent on cycle_id, so the loop can
        write the same cycle on every pass as it advances."""
        row = await self.session.get(CCLearningCycle, entry.cycle_id)
        if row is None:
            row = CCLearningCycle(cycle_id=entry.cycle_id, tenant_id=tenant_id)
            self.session.add(row)
            if entry.started_at:
                row.started_at = datetime.fromtimestamp(
                    entry.started_at, tz=timezone.utc
                )
        row.tenant_id = tenant_id or row.tenant_id
        row.pattern_id = entry.pattern_id
        row.pattern_type = entry.pattern_type
        row.skill_id = entry.skill_id
        row.sites_distributed = entry.sites_distributed
        row.devices_applied = entry.devices_applied
        row.outcomes_before = dict(entry.outcomes_before or {})
        row.outcomes_after = dict(entry.outcomes_after or {})
        row.improvement_pct = entry.improvement_pct
        row.promotion_recommended = bool(entry.promoted)
        row.updated_at = utcnow()
        if entry.completed_at:
            row.completed_at = datetime.fromtimestamp(
                entry.completed_at, tz=timezone.utc
            )
            row.status = "closed"
        elif entry.promoted:
            row.status = "promotion_recommended"
        elif entry.skill_id:
            row.status = "measuring"
        else:
            row.status = "open"
        await self.session.flush()
        return row

    async def list_cycles(
        self, tenant_id: str, status: Optional[str] = None, limit: int = 200,
    ) -> Sequence[CCLearningCycle]:
        stmt = select(CCLearningCycle).where(
            CCLearningCycle.tenant_id == tenant_id
        )
        if status:
            stmt = stmt.where(CCLearningCycle.status == status)
        stmt = stmt.order_by(CCLearningCycle.started_at.desc()).limit(limit)
        return (await self.session.execute(stmt)).scalars().all()

    async def get(self, cycle_id: str) -> Optional[CCLearningCycle]:
        return await self.session.get(CCLearningCycle, cycle_id)


class LearnedSignalRepo:
    """Durable knowledge derived from patterns (S3).

    Upsert on (tenant, signal_key): re-detecting the same relationship
    REFRESHES the knowledge and bumps its observation count rather than
    accumulating duplicates.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert(
        self, tenant_id: str, signal: dict, cycle_id: Optional[str] = None,
    ) -> CCLearnedSignal:
        existing = (
            await self.session.execute(
                select(CCLearnedSignal)
                .where(CCLearnedSignal.tenant_id == tenant_id)
                .where(CCLearnedSignal.signal_key == signal["signal_key"])
            )
        ).scalar_one_or_none()
        if existing is None:
            existing = CCLearnedSignal(
                tenant_id=tenant_id, signal_key=signal["signal_key"],
            )
            self.session.add(existing)
        else:
            existing.observation_count += 1
        existing.scope_type = signal["scope_type"]
        existing.scope_ref = signal["scope_ref"]
        existing.action_type = signal["action_type"]
        existing.vendor = signal.get("vendor", "")
        existing.model = signal.get("model", "")
        existing.statement = signal["statement"][:512]
        existing.evidence = dict(signal.get("evidence") or {})
        existing.confidence = signal["confidence"]
        existing.source_pattern_id = signal.get("source_pattern_id", "")
        if cycle_id:
            existing.source_cycle_id = cycle_id
        existing.status = "active"
        existing.last_confirmed_at = utcnow()
        await self.session.flush()
        return existing

    async def list_active(
        self, tenant_id: str, scope_type: Optional[str] = None,
        scope_ref: Optional[str] = None, limit: int = 500,
    ) -> Sequence[CCLearnedSignal]:
        stmt = (
            select(CCLearnedSignal)
            .where(CCLearnedSignal.tenant_id == tenant_id)
            .where(CCLearnedSignal.status == "active")
        )
        if scope_type:
            stmt = stmt.where(CCLearnedSignal.scope_type == scope_type)
        if scope_ref:
            stmt = stmt.where(CCLearnedSignal.scope_ref == scope_ref)
        stmt = stmt.order_by(CCLearnedSignal.confidence.desc()).limit(limit)
        return (await self.session.execute(stmt)).scalars().all()


class SafetyStateRepo:
    """Live autonomy safety state per site (S5).

    Replace-on-poll, like the fleet cache: safety state is a CURRENT
    reading, and a stale row read as current would be worse than none.
    A poll that carried no safety state writes `reported=False` rather
    than leaving yesterday's row to look like today's truth.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert(
        self, tenant_id: str, site_id: str, safety: dict,
    ) -> CCSafetyState:
        row = await self.session.get(CCSafetyState, site_id)
        if row is None:
            row = CCSafetyState(site_id=site_id)
            self.session.add(row)
        row.tenant_id = tenant_id
        row.reported = bool(safety.get("reported"))
        as_of = safety.get("as_of_unix") or 0
        row.as_of = (
            datetime.fromtimestamp(as_of, tz=timezone.utc) if as_of else None
        )
        row.sm_stop_switch = bool(safety.get("sm_stop_switch"))
        row.suppressions = safety.get("suppressions") or []
        row.error_budgets = safety.get("error_budgets") or []
        row.site_budgets = safety.get("site_budgets") or {}
        row.ingested_at = datetime.now(timezone.utc)
        await self.session.flush()
        return row

    async def list_for_tenant(self, tenant_id: str) -> Sequence[CCSafetyState]:
        return (
            await self.session.execute(
                select(CCSafetyState)
                .where(CCSafetyState.tenant_id == tenant_id)
                .order_by(CCSafetyState.site_id)
            )
        ).scalars().all()


class IncidentRepo:
    """Real incidents projected from Site Manager snapshots (S4)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert(self, tenant_id: str, site_id: str, inc: dict) -> CCIncident:
        """Idempotent on incident_id; refreshes diagnosis on every poll.

        The diagnosis can arrive AFTER the incident opens (the LLM enriches
        asynchronously at the site), so a later poll legitimately carries an
        explanation the first one did not.
        """
        row = await self.session.get(CCIncident, inc["incident_id"])
        if row is None:
            row = CCIncident(
                incident_id=inc["incident_id"], tenant_id=tenant_id,
            )
            self.session.add(row)
        row.tenant_id = tenant_id or row.tenant_id
        row.site_id = site_id
        row.kind = inc.get("kind", "")
        row.status = inc.get("status", "open")
        row.title = (inc.get("title") or "")[:512]
        row.device_agent_id = inc.get("device_agent_id", "")
        row.subsystem = inc.get("subsystem", "")
        row.parent_incident_id = inc.get("parent_incident_id") or None
        row.confidence = inc.get("confidence", 0.0)
        row.inferred = bool(inc.get("inferred", False))
        if inc.get("correlation_meta"):
            row.correlation_meta = dict(inc["correlation_meta"])
        if inc.get("explanation"):
            row.explanation = dict(inc["explanation"])
        opened = inc.get("opened_at_unix")
        if opened:
            row.opened_at = datetime.fromtimestamp(opened, tz=timezone.utc)
        row.last_seen_at = utcnow()
        # Reappearing after an inferred resolution means it never really
        # cleared; reopen rather than leaving a stale resolved row.
        row.resolved_at = None
        row.status = inc.get("status", "open")
        await self.session.flush()
        return row

    async def resolve_absent(
        self, tenant_id: str, site_id: str, present_ids: set[str],
    ) -> int:
        """D3 absence-inference: an open incident missing from this site's
        snapshot has cleared at the site, so mark it resolved here.

        Scoped to ONE site: another site's incidents are absent from this
        snapshot for the obvious reason and must not be resolved by it.
        """
        rows = (
            await self.session.execute(
                select(CCIncident)
                .where(CCIncident.tenant_id == tenant_id)
                .where(CCIncident.site_id == site_id)
                .where(CCIncident.status == "open")
            )
        ).scalars().all()
        resolved = 0
        for row in rows:
            if row.incident_id not in present_ids:
                row.status = "resolved"
                row.resolved_at = utcnow()
                resolved += 1
        if resolved:
            await self.session.flush()
        return resolved

    async def list_incidents(
        self, tenant_id: str, status: Optional[str] = "open",
        site_id: Optional[str] = None, device_agent_id: Optional[str] = None,
        limit: int = 200,
    ) -> Sequence[CCIncident]:
        stmt = select(CCIncident).where(CCIncident.tenant_id == tenant_id)
        if status:
            stmt = stmt.where(CCIncident.status == status)
        if site_id:
            stmt = stmt.where(CCIncident.site_id == site_id)
        if device_agent_id:
            stmt = stmt.where(CCIncident.device_agent_id == device_agent_id)
        stmt = stmt.order_by(CCIncident.opened_at.desc().nullslast()).limit(limit)
        return (await self.session.execute(stmt)).scalars().all()

    async def get(self, tenant_id: str, incident_id: str) -> Optional[CCIncident]:
        row = await self.session.get(CCIncident, incident_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return row

    async def children_of(
        self, tenant_id: str, parent_id: str,
    ) -> Sequence[CCIncident]:
        return (
            await self.session.execute(
                select(CCIncident)
                .where(CCIncident.tenant_id == tenant_id)
                .where(CCIncident.parent_incident_id == parent_id)
                .order_by(CCIncident.opened_at)
            )
        ).scalars().all()
