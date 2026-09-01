"""Thin data-access helpers. Each repo wraps one AsyncSession.

Commit responsibility stays with the caller (one commit per API request)
so multi-table updates remain atomic.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import delete as sa_delete, false as sa_false, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_cc.db.models import (
    CCAgentIdentity,
    CCAgentPreflight,
    CCCapabilityCatalogue,
    CCAgentSkillInstall,
    CCCampaign,
    CCCampaignDispatch,
    CCCampaignPlan,
    CCCampaignScope,
    CCCampaignSite,
    CCCampaignTarget,
    CCCampaignWave,
    CCOrgUnit,
    CCAgentCapability,
    CCAgentProposal,
    CCScopeGrant,
    CCTenantSettings,
    CCApprovalGroup,
    CCApprovalGroupMember,
    CCApprovalPolicy,
    CCApprovalRecord,
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
    CCOperationalAgent,
    CCOutcomeHistory,
    CCSafetyState,
    CCSite,
    CCSkillDelivery,
    CCStopSwitch,
    CCUsageSnapshot,
    CCWarranty,
    utcnow,
)


def scope_sites(column, scope) -> Any:
    """A SQLAlchemy condition restricting `column` to the caller's sites.

    E1.2 layer 2, applied in the repository so no handler and no browser
    can be the thing that decided. Three cases and no fourth:

    * `scope is None`   -- an internal caller (a poller, the evaluator).
      No user is asking, so there is nothing to scope to.
    * tenant-wide       -- no condition; the caller reaches every site.
    * anything else     -- ``site_id IN (...)``, and an EMPTY scope
      yields ``false()``, not "no filter". Fail closed is the whole
      point: an unscoped principal under strict mode must read nothing.
    """
    if scope is None or getattr(scope, "tenant_wide", False):
        return None
    site_ids = sorted(getattr(scope, "site_ids", ()) or ())
    if not site_ids:
        return sa_false()
    return column.in_(site_ids)


def _audit_scoped(stmt, scope):
    """Scope an audit read (E1.2).

    `cc_audit_log.site_id` is nullable and pre-E1.2 rows have no site --
    it was never recorded and cannot be invented now. A NULL site is
    therefore TENANT-LEVEL and visible only to a tenant-scope holder;
    a scoped principal sees the entries for their own sites and nothing
    else. That is fail-closed, and it means a scoped principal now sees
    LESS audit than before E1.2, which is the correction rather than a
    regression. An auditor holds tenant scope and loses nothing.
    """
    if scope is None or getattr(scope, "tenant_wide", False):
        return stmt
    site_ids = sorted(getattr(scope, "site_ids", ()) or ())
    if not site_ids:
        return stmt.where(sa_false())
    return stmt.where(CCAuditLog.site_id.in_(site_ids))


def apply_scope(stmt, column, scope):
    """Conjoin `scope_sites` onto a statement when it applies."""
    condition = scope_sites(column, scope)
    return stmt if condition is None else stmt.where(condition)


class OrgUnitRepo:
    """Reads and writes on the organizational tree (E1.1).

    Every query is conjoined with `tenant_id`. The prefix match alone
    would already be safe -- ids are uuid4 -- but tenant isolation is
    the invariant that must never depend on id entropy.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, tenant_id: str, unit_id: str) -> Optional[CCOrgUnit]:
        return (
            await self.session.execute(
                select(CCOrgUnit).where(
                    CCOrgUnit.tenant_id == tenant_id, CCOrgUnit.id == unit_id
                )
            )
        ).scalar_one_or_none()

    async def list_all(self, tenant_id: str) -> Sequence[CCOrgUnit]:
        return (
            await self.session.execute(
                select(CCOrgUnit)
                .where(CCOrgUnit.tenant_id == tenant_id)
                .order_by(CCOrgUnit.depth, CCOrgUnit.sort_order, CCOrgUnit.name)
            )
        ).scalars().all()

    async def list_roots(self, tenant_id: str) -> Sequence[CCOrgUnit]:
        return (
            await self.session.execute(
                select(CCOrgUnit)
                .where(
                    CCOrgUnit.tenant_id == tenant_id,
                    CCOrgUnit.parent_id.is_(None),
                )
                .order_by(CCOrgUnit.sort_order, CCOrgUnit.name)
            )
        ).scalars().all()

    async def list_subtree(self, tenant_id: str, path: str) -> Sequence[CCOrgUnit]:
        """The unit at `path` and everything beneath it.

        One prefix match. `autoescape` neutralizes LIKE's `%` and `_`;
        hex ids mean neither can appear anyway, so this is belt and
        braces on a structural guarantee rather than the guarantee
        itself.
        """
        return (
            await self.session.execute(
                select(CCOrgUnit)
                .where(
                    CCOrgUnit.tenant_id == tenant_id,
                    CCOrgUnit.path.startswith(path, autoescape=True),
                )
                .order_by(CCOrgUnit.depth, CCOrgUnit.sort_order, CCOrgUnit.name)
            )
        ).scalars().all()

    async def list_by_ids(
        self, tenant_id: str, unit_ids: Sequence[str]
    ) -> Sequence[CCOrgUnit]:
        if not unit_ids:
            return []
        return (
            await self.session.execute(
                select(CCOrgUnit).where(
                    CCOrgUnit.tenant_id == tenant_id,
                    CCOrgUnit.id.in_(list(unit_ids)),
                )
            )
        ).scalars().all()

    async def sibling_named(
        self, tenant_id: str, parent_id: Optional[str], name: str,
        exclude_id: Optional[str] = None,
    ) -> Optional[CCOrgUnit]:
        stmt = select(CCOrgUnit).where(
            CCOrgUnit.tenant_id == tenant_id,
            CCOrgUnit.name == name,
        )
        stmt = stmt.where(
            CCOrgUnit.parent_id.is_(None)
            if parent_id is None
            else CCOrgUnit.parent_id == parent_id
        )
        if exclude_id:
            stmt = stmt.where(CCOrgUnit.id != exclude_id)
        return (await self.session.execute(stmt)).scalars().first()

    async def create(
        self,
        tenant_id: str,
        *,
        name: str,
        unit_type: str,
        parent: Optional[CCOrgUnit],
        sort_order: int = 0,
        created_by: str = "",
    ) -> CCOrgUnit:
        from harkeniq_cc.org_tree import compose_path

        unit = CCOrgUnit(
            tenant_id=tenant_id,
            parent_id=parent.id if parent else None,
            unit_type=unit_type,
            name=name,
            sort_order=sort_order,
            created_by=created_by,
            updated_by=created_by,
            # Placeholder: the path needs the generated id, so it is set
            # immediately below once the default has fired.
            path="",
            depth=(parent.depth + 1) if parent else 1,
        )
        self.session.add(unit)
        await self.session.flush()
        unit.path = compose_path(parent.path if parent else None, unit.id)
        await self.session.flush()
        return unit

    async def child_count(self, tenant_id: str, unit_id: str) -> int:
        return int(
            (
                await self.session.execute(
                    select(func.count())
                    .select_from(CCOrgUnit)
                    .where(
                        CCOrgUnit.tenant_id == tenant_id,
                        CCOrgUnit.parent_id == unit_id,
                    )
                )
            ).scalar_one()
        )

    async def subtree_height(self, tenant_id: str, path: str) -> int:
        """Levels carried by this subtree; 1 when the unit is a leaf."""
        rows = await self.list_subtree(tenant_id, path)
        base = len([seg for seg in path.split("/") if seg])
        return max((u.depth for u in rows), default=base) - base + 1

    async def move(
        self,
        tenant_id: str,
        unit: CCOrgUnit,
        new_parent: Optional[CCOrgUnit],
        *,
        actor: str = "",
    ) -> tuple[str, str]:
        """Re-parent `unit` and rewrite every descendant path.

        Returns (old_path, new_path). The descendants are rewritten in
        the same transaction as the unit itself: a half-moved subtree
        would leave paths that resolve to nothing.
        """
        from harkeniq_cc.org_tree import compose_path, rewrite_descendant_path

        old_path = unit.path
        descendants = [
            row for row in await self.list_subtree(tenant_id, old_path)
            if row.id != unit.id
        ]
        new_path = compose_path(new_parent.path if new_parent else None, unit.id)
        new_depth = (new_parent.depth + 1) if new_parent else 1

        unit.parent_id = new_parent.id if new_parent else None
        unit.path = new_path
        unit.depth = new_depth
        unit.updated_by = actor

        shift = new_depth - len([s for s in old_path.split("/") if s])
        for row in descendants:
            row.path = rewrite_descendant_path(row.path, old_path, new_path)
            row.depth = row.depth + shift
            row.updated_by = actor
        await self.session.flush()
        return old_path, new_path

    async def delete(self, unit: CCOrgUnit) -> None:
        await self.session.execute(
            sa_delete(CCOrgUnit).where(CCOrgUnit.id == unit.id)
        )

    async def ensure_root(self, tenant_id: str, created_by: str = "") -> CCOrgUnit:
        """The tenant's root organizational unit, creating it if absent.

        E1.1 promised every site a canonical organizational path, and
        migration 0010's backfill delivered that for every tenant that
        existed WHEN IT RAN. A tenant created afterwards -- or one whose
        first site arrives later -- had no root at all, so its tree read
        was empty and its sites had no path. Found by the compose gate on
        a fresh stack, where the migration runs before any tenant exists.
        """
        from harkeniq_cc.org_tree import compose_path

        roots = await self.list_roots(tenant_id)
        if roots:
            return roots[0]
        unit = CCOrgUnit(
            tenant_id=tenant_id,
            parent_id=None,
            unit_type="organization",
            name=tenant_id,
            path="",
            depth=1,
            created_by=created_by or "system",
            updated_by=created_by or "system",
        )
        self.session.add(unit)
        await self.session.flush()
        unit.path = compose_path(None, unit.id)
        await self.session.flush()
        return unit

    async def site_counts(self, tenant_id: str) -> dict[str, int]:
        rows = (
            await self.session.execute(
                select(CCSite.org_unit_id, func.count())
                .where(
                    CCSite.tenant_id == tenant_id,
                    CCSite.org_unit_id.isnot(None),
                )
                .group_by(CCSite.org_unit_id)
            )
        ).all()
        return {unit_id: int(count) for unit_id, count in rows}

    async def sites_in(self, tenant_id: str, unit_id: str) -> Sequence[CCSite]:
        return (
            await self.session.execute(
                select(CCSite)
                .where(
                    CCSite.tenant_id == tenant_id,
                    CCSite.org_unit_id == unit_id,
                )
                .order_by(CCSite.site_name)
            )
        ).scalars().all()

    async def site_count_in_subtree(self, tenant_id: str, path: str) -> int:
        """Sites attached anywhere at or below `path`."""
        unit_ids = [u.id for u in await self.list_subtree(tenant_id, path)]
        if not unit_ids:
            return 0
        return int(
            (
                await self.session.execute(
                    select(func.count())
                    .select_from(CCSite)
                    .where(
                        CCSite.tenant_id == tenant_id,
                        CCSite.org_unit_id.in_(unit_ids),
                    )
                )
            ).scalar_one()
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

    async def list_all(self, tenant_id: str, scope=None) -> Sequence[CCSite]:
        stmt = apply_scope(
            select(CCSite).where(CCSite.tenant_id == tenant_id), CCSite.id, scope
        )
        return (
            await self.session.execute(stmt.order_by(CCSite.site_name))
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
        last_seen_at: Optional[datetime] = None,
        capabilities: Optional[dict] = None,
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
        # Only written when the site actually reported a reading. A poll that
        # carries none must leave the last honest value alone rather than
        # clear it — and must never fall back to snapshot_at, which is this
        # row's refresh time and says nothing about the agent.
        if last_seen_at is not None:
            row.last_seen_at = last_seen_at
        # Same rule as last_seen_at: a poll from an SM that carries no
        # declaration must leave the last one alone. An SM downgraded
        # below the Registry would otherwise turn a whole site's proven
        # capability into unknown on its next poll.
        if capabilities is not None:
            row.capabilities = capabilities
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

    async def list_all(
        self, tenant_id: str, scope=None
    ) -> Sequence[CCFleetCache]:
        stmt = apply_scope(
            select(CCFleetCache)
            .join(CCSite, CCSite.id == CCFleetCache.site_id)
            .where(CCSite.tenant_id == tenant_id),
            CCFleetCache.site_id,
            scope,
        )
        return (
            await self.session.execute(stmt.order_by(CCFleetCache.agent_name))
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
        scope=None,
    ) -> tuple[Sequence[CCFleetCache], int]:
        """Return paginated devices with total count, optionally filtered."""
        stmt = apply_scope(
            select(CCFleetCache)
            .join(CCSite, CCSite.id == CCFleetCache.site_id)
            .where(CCSite.tenant_id == tenant_id),
            CCFleetCache.site_id,
            scope,
        )
        # The COUNT is scoped too: a total that counted rows the caller
        # cannot see would leak the size of the rest of the fleet.
        count_stmt = apply_scope(
            select(func.count(CCFleetCache.id))
            .join(CCSite, CCSite.id == CCFleetCache.site_id)
            .where(CCSite.tenant_id == tenant_id),
            CCFleetCache.site_id,
            scope,
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

    async def count_by_health(self, tenant_id: str, scope=None) -> dict[str, int]:
        """Return {health_status: count} for the devices the caller may see."""
        stmt = apply_scope(
            select(CCFleetCache.health, func.count(CCFleetCache.id))
            .join(CCSite, CCSite.id == CCFleetCache.site_id)
            .where(CCSite.tenant_id == tenant_id),
            CCFleetCache.site_id,
            scope,
        ).group_by(CCFleetCache.health)
        rows = (await self.session.execute(stmt)).all()
        return {row[0]: row[1] for row in rows}

    async def count_total(self, tenant_id: str, scope=None) -> int:
        result = await self.session.execute(
            apply_scope(
                select(func.count(CCFleetCache.id))
                .join(CCSite, CCSite.id == CCFleetCache.site_id)
                .where(CCSite.tenant_id == tenant_id),
                CCFleetCache.site_id,
                scope,
            )
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

    async def list_pending(
        self, tenant_id: str, scope=None
    ) -> Sequence[CCApprovalRoute]:
        stmt = apply_scope(
            select(CCApprovalRoute)
            .join(CCSite, CCSite.id == CCApprovalRoute.site_id)
            .where(CCSite.tenant_id == tenant_id)
            .where(CCApprovalRoute.decision.is_(None)),
            CCApprovalRoute.site_id,
            scope,
        )
        return (
            await self.session.execute(stmt.order_by(CCApprovalRoute.routed_at))
        ).scalars().all()

    async def list_pending_paginated(
        self, tenant_id: str, page: int = 1, page_size: int = 50, scope=None,
    ) -> tuple[Sequence[CCApprovalRoute], int]:
        base = apply_scope(
            select(CCApprovalRoute)
            .join(CCSite, CCSite.id == CCApprovalRoute.site_id)
            .where(CCSite.tenant_id == tenant_id)
            .where(CCApprovalRoute.decision.is_(None)),
            CCApprovalRoute.site_id,
            scope,
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

    async def list_history(
        self, tenant_id: str, scope=None
    ) -> Sequence[CCApprovalRoute]:
        stmt = apply_scope(
            select(CCApprovalRoute)
            .join(CCSite, CCSite.id == CCApprovalRoute.site_id)
            .where(CCSite.tenant_id == tenant_id)
            .where(CCApprovalRoute.decision.isnot(None)),
            CCApprovalRoute.site_id,
            scope,
        )
        return (
            await self.session.execute(
                stmt.order_by(CCApprovalRoute.decided_at.desc())
            )
        ).scalars().all()

    async def list_history_paginated(
        self, tenant_id: str, page: int = 1, page_size: int = 50, scope=None,
    ) -> tuple[Sequence[CCApprovalRoute], int]:
        base = apply_scope(
            select(CCApprovalRoute)
            .join(CCSite, CCSite.id == CCApprovalRoute.site_id)
            .where(CCSite.tenant_id == tenant_id)
            .where(CCApprovalRoute.decision.isnot(None)),
            CCApprovalRoute.site_id,
            scope,
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


class ScopeGrantRepo:
    """Reads and writes on `cc_scope_grants` (E1.2).

    One table, two principal kinds. Revocation is a timestamp, never a
    delete: an approval recorded under a grant keeps a `scope_snapshot`
    that has to stay addressable afterwards.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_for_principal(
        self, tenant_id: str, principal_ref: str,
        principal_type: str = "user", include_revoked: bool = False,
        realm: str = "",
    ) -> Sequence[CCScopeGrant]:
        """Grants for one principal, in one realm (E1.4).

        `realm` narrows to grants made under the realm this Central
        Command serves. A Keycloak subject is realm-scoped, so a grant
        from another realm authorizing here would be a cross-realm
        authorization bug. An empty stored realm is a pre-E1.4 grant and
        still counts, so an upgrade changes nothing.
        """
        stmt = select(CCScopeGrant).where(
            CCScopeGrant.tenant_id == tenant_id,
            CCScopeGrant.principal_type == principal_type,
            CCScopeGrant.principal_ref == principal_ref,
        )
        if realm:
            stmt = stmt.where(
                CCScopeGrant.realm.in_([realm, ""])
                | CCScopeGrant.realm.is_(None)
            )
        if not include_revoked:
            stmt = stmt.where(CCScopeGrant.revoked_at.is_(None))
        return (
            await self.session.execute(
                stmt.order_by(CCScopeGrant.scope_type, CCScopeGrant.scope_ref)
            )
        ).scalars().all()

    async def list_all(
        self, tenant_id: str, *, principal_type: str = "",
        include_revoked: bool = False,
    ) -> Sequence[CCScopeGrant]:
        stmt = select(CCScopeGrant).where(CCScopeGrant.tenant_id == tenant_id)
        if principal_type:
            stmt = stmt.where(CCScopeGrant.principal_type == principal_type)
        if not include_revoked:
            stmt = stmt.where(CCScopeGrant.revoked_at.is_(None))
        return (
            await self.session.execute(
                stmt.order_by(
                    CCScopeGrant.principal_ref,
                    CCScopeGrant.scope_type,
                    CCScopeGrant.scope_ref,
                )
            )
        ).scalars().all()

    async def get(self, tenant_id: str, grant_id: str) -> Optional[CCScopeGrant]:
        return (
            await self.session.execute(
                select(CCScopeGrant).where(
                    CCScopeGrant.tenant_id == tenant_id,
                    CCScopeGrant.id == grant_id,
                )
            )
        ).scalar_one_or_none()

    async def grant(
        self,
        *,
        tenant_id: str,
        principal_type: str,
        principal_ref: str,
        scope_type: str,
        scope_ref: str = "",
        permission_subset: Optional[list] = None,
        role: str = "",
        realm: str = "",
        granted_by: str = "",
        expires_at: Optional[datetime] = None,
        note: str = "",
    ) -> CCScopeGrant:
        """Create, or revive a previously revoked identical grant.

        The unique constraint is on the identity of the grant, so
        re-granting after a revocation reuses the row rather than
        colliding with its own history.
        """
        existing = (
            await self.session.execute(
                select(CCScopeGrant).where(
                    CCScopeGrant.tenant_id == tenant_id,
                    CCScopeGrant.principal_type == principal_type,
                    CCScopeGrant.principal_ref == principal_ref,
                    CCScopeGrant.scope_type == scope_type,
                    CCScopeGrant.scope_ref == scope_ref,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.revoked_at = None
            existing.revoked_by = ""
            existing.permission_subset = permission_subset
            existing.role = role
            existing.realm = realm
            existing.granted_by = granted_by
            existing.granted_at = utcnow()
            existing.expires_at = expires_at
            existing.note = note
            await self.session.flush()
            return existing

        row = CCScopeGrant(
            tenant_id=tenant_id,
            principal_type=principal_type,
            principal_ref=principal_ref,
            scope_type=scope_type,
            scope_ref=scope_ref,
            permission_subset=permission_subset,
            role=role,
            realm=realm,
            granted_by=granted_by,
            expires_at=expires_at,
            note=note,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def revoke(self, grant: CCScopeGrant, revoked_by: str) -> CCScopeGrant:
        grant.revoked_at = utcnow()
        grant.revoked_by = revoked_by
        await self.session.flush()
        return grant

    async def realm_census(self, tenant_id: str) -> dict[str, int]:
        """How many active grants exist, and under which realm. E1.4.

        A tenant moved to a new realm keeps grants naming subjects from
        the old one. They authorize nothing, and without this the
        condition is invisible: every principal simply sees nothing and
        nobody can explain why.
        """
        rows = (
            await self.session.execute(
                select(CCScopeGrant.realm, func.count())
                .where(
                    CCScopeGrant.tenant_id == tenant_id,
                    CCScopeGrant.revoked_at.is_(None),
                )
                .group_by(CCScopeGrant.realm)
            )
        ).all()
        return {(realm or ""): int(count) for realm, count in rows}

    async def distinct_principals(
        self, tenant_id: str, principal_type: str = "user"
    ) -> Sequence[str]:
        rows = (
            await self.session.execute(
                select(CCScopeGrant.principal_ref)
                .where(
                    CCScopeGrant.tenant_id == tenant_id,
                    CCScopeGrant.principal_type == principal_type,
                    CCScopeGrant.revoked_at.is_(None),
                )
                .distinct()
            )
        ).all()
        return [r[0] for r in rows]


class TenantSettingsRepo:
    """Per-tenant enforcement posture (E1.2)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, tenant_id: str) -> Optional[CCTenantSettings]:
        return await self.session.get(CCTenantSettings, tenant_id)

    async def enforcement(self, tenant_id: str) -> str:
        """The posture, defaulting to legacy_open for an unseen tenant.

        An unseen tenant is one that predates E1.2 or has never been
        configured; treating that as strict would lock out a working
        deployment on upgrade.
        """
        row = await self.get(tenant_id)
        return row.scope_enforcement if row else "legacy_open"

    async def set_enforcement(
        self, tenant_id: str, mode: str, updated_by: str
    ) -> CCTenantSettings:
        row = await self.get(tenant_id)
        if row is None:
            row = CCTenantSettings(tenant_id=tenant_id)
            self.session.add(row)
        row.scope_enforcement = mode
        row.updated_by = updated_by
        await self.session.flush()
        return row


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
        site_id: Optional[str] = None,
    ) -> CCAuditLog:
        """Append one entry to the tenant's hash chain.

        E1.2: `site_id` is authorization/indexing metadata and is
        DELIBERATELY absent from `_chain_payload`. The chain hashes ts,
        actor, action, subject, tenant_id and detail -- and only those --
        so recording a site neither changes an entry's hash nor
        invalidates any chain written before this column existed. A test
        asserts that rather than trusting it.
        """
        from harkeniq.audit.chain import next_link, pg_advisory_chain_lock

        row = CCAuditLog(
            ts=utcnow(),
            actor=actor,
            action=action,
            subject=subject,
            tenant_id=tenant_id,
            detail=detail,
            site_id=site_id,
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
        scope=None,
    ) -> Sequence[CCAuditLog]:
        stmt = _audit_scoped(
            select(CCAuditLog).where(CCAuditLog.tenant_id == tenant_id), scope
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
        scope=None,
        actor: Optional[str] = None,
        action: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
    ) -> int:
        """True total for the same filter — P0 2026-08-29: the audit list
        endpoint reported the current page length as "total"."""
        stmt = _audit_scoped(
            select(func.count(CCAuditLog.id)).where(
                CCAuditLog.tenant_id == tenant_id
            ),
            scope,
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
        risk_level: str = "*",
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
        self, group_id: str, user_email: str, role: str = "approver",
        principal_ref: str = "",
    ) -> CCApprovalGroupMember:
        """Add an approver. `principal_ref` is the Keycloak subject.

        E0.1: membership matches on the subject first and falls back to
        the email, so an address change cannot silently lapse someone's
        approval authority. The email stays as the display name and as
        the match for memberships added before subjects were recorded.
        """
        row = CCApprovalGroupMember(
            group_id=group_id, user_email=user_email, role=role,
            principal_ref=principal_ref,
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
        scope=None,
    ) -> list[dict]:
        """Outcome rows as aggregator-ready dicts (site_id included).

        Tenant scoping goes through cc_sites: an outcome belongs to the
        tenant that owns the site it was polled from. E1.2 narrows that
        further to the caller's own sites; `scope=None` is the internal
        caller (the IntelligenceEngine), which is fleet-wide by design.
        """
        stmt = apply_scope(
            select(CCOutcomeHistory)
            .join(CCSite, CCOutcomeHistory.site_id == CCSite.id)
            .where(CCSite.tenant_id == tenant_id),
            CCOutcomeHistory.site_id,
            scope,
        ).order_by(CCOutcomeHistory.ingested_at).limit(limit)
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
                # A1: evidence that cannot name its actor cannot answer
                # "what did MY agent do", which is half of trusting one.
                "actor": r.actor or "",
                "device_agent_id": r.device_agent_id,
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
        # A22.4: None means the Site Manager did not report, which must stay
        # distinguishable from "reported, and nothing is affected". Only an
        # actual report overwrites -- a poll from an older SM never erases a
        # newer SM's answer.
        if inc.get("components") is not None:
            row.components = list(inc["components"])
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
        scope=None,
    ) -> Sequence[CCIncident]:
        stmt = apply_scope(
            select(CCIncident).where(CCIncident.tenant_id == tenant_id),
            CCIncident.site_id,
            scope,
        )
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


class OperationalAgentRepo:
    """A0: the Operational Agent bundle, its scope, and its bindings.

    Every read is tenant-scoped on the row itself (CC is single-tenant
    structurally, but an agent is tenant-owned data and must never be
    reachable by id alone from another tenant's request).
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # -- agent -----------------------------------------------------------
    async def create(
        self,
        *,
        tenant_id: str,
        name: str,
        description: str,
        autonomy_ceiling: int,
        require_approval_always: bool,
        max_proposals_per_day: int,
        created_by: str,
        execution_budget: int = 0,
        budget_period: str = "daily",
    ) -> CCOperationalAgent:
        agent = CCOperationalAgent(
            tenant_id=tenant_id,
            name=name,
            description=description,
            autonomy_ceiling=autonomy_ceiling,
            require_approval_always=require_approval_always,
            max_proposals_per_day=max_proposals_per_day,
            # A2/D2: settable at creation. 0 means unset -- the tenant and
            # site budgets still apply, and this can only narrow them.
            execution_budget=execution_budget,
            budget_period=budget_period,
            created_by=created_by,
            updated_by=created_by,
        )
        self.session.add(agent)
        await self.session.flush()
        return agent

    async def get(self, tenant_id: str, agent_id: str) -> Optional[CCOperationalAgent]:
        return (
            await self.session.execute(
                select(CCOperationalAgent).where(
                    CCOperationalAgent.id == agent_id,
                    CCOperationalAgent.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()

    async def get_by_name(
        self, tenant_id: str, name: str
    ) -> Optional[CCOperationalAgent]:
        return (
            await self.session.execute(
                select(CCOperationalAgent).where(
                    CCOperationalAgent.tenant_id == tenant_id,
                    CCOperationalAgent.name == name,
                )
            )
        ).scalar_one_or_none()

    async def list_all(
        self, tenant_id: str, status: Optional[str] = None
    ) -> Sequence[CCOperationalAgent]:
        stmt = select(CCOperationalAgent).where(
            CCOperationalAgent.tenant_id == tenant_id
        )
        if status:
            stmt = stmt.where(CCOperationalAgent.status == status)
        return (
            await self.session.execute(stmt.order_by(CCOperationalAgent.created_at))
        ).scalars().all()

    async def list_pending_activation(
        self, tenant_id: str
    ) -> Sequence[CCOperationalAgent]:
        """Agents whose ACTIVATION is waiting on a human (A2 D1).

        A preflight that found unattended grants raises a subject; until
        somebody decides it, the activation is waiting exactly as a node
        action or an agent proposal waits. It belongs in the one queue,
        or the operator has to know to go looking on the agent page --
        which is a second approval surface in everything but name.

        An agent that is already active or retired is excluded: the
        subject ref stays on the row as the record of what was approved,
        but there is nothing left to decide.
        """
        return (
            await self.session.execute(
                select(CCOperationalAgent)
                .where(
                    CCOperationalAgent.tenant_id == tenant_id,
                    CCOperationalAgent.activation_subject_ref != "",
                    CCOperationalAgent.status.notin_(("active", "retired")),
                )
                .order_by(CCOperationalAgent.updated_at)
            )
        ).scalars().all()

    async def bump_version(self, agent: CCOperationalAgent, actor: str) -> None:
        """Any configuration change is a new version.

        The attribution key embeds the version, so a bundle that changes
        after a proposal was made cannot silently rewrite what that
        proposal was made under.
        """
        agent.version += 1
        agent.updated_by = actor
        agent.updated_at = utcnow()

    async def set_status(
        self, agent: CCOperationalAgent, status: str, actor: str
    ) -> None:
        """Move an agent's lifecycle state, recording what was switched on.

        A2/A19.9: `activated_version` is written HERE, in the same unit of
        work as the status, because the pair is one fact. Splitting them
        would let a crash between the two leave an agent that is active at
        a version nothing recorded.

        The invariant this establishes is stated positively:

            active AND activated_version == version  ->  no drift

        Before this existed the column had no writer at all, so it stayed
        0 while `version` starts at 1, and every freshly activated agent
        reported configuration drift immediately.
        """
        agent.status = status
        agent.updated_by = actor
        agent.updated_at = utcnow()
        if status == "active":
            agent.activated_by = actor
            agent.activated_at = utcnow()
            agent.activated_version = int(agent.version)

    async def mark_evaluated(self, agent: CCOperationalAgent) -> None:
        agent.last_evaluated_at = utcnow()

    # -- scope -----------------------------------------------------------
    async def list_scopes(self, agent_id: str) -> Sequence[CCScopeGrant]:
        """An agent's scope rows -- from the ONE grant table (E1.2).

        `cc_agent_scopes` migrated into `cc_scope_grants` as
        `principal_type="agent"`. Callers are unchanged because a grant
        row carries the same `scope_type` / `scope_ref` the agent rows
        did; what changed is that humans and agents now resolve through
        the same resolver over the same table.
        """
        return (
            await self.session.execute(
                select(CCScopeGrant)
                .where(
                    CCScopeGrant.principal_type == "agent",
                    CCScopeGrant.principal_ref == agent_id,
                    CCScopeGrant.revoked_at.is_(None),
                )
                .order_by(CCScopeGrant.scope_type, CCScopeGrant.scope_ref)
            )
        ).scalars().all()

    async def add_scope(
        self, *, agent_id: str, tenant_id: str, scope_type: str, scope_ref: str,
        granted_by: str = "",
    ) -> CCScopeGrant:
        row = CCScopeGrant(
            tenant_id=tenant_id,
            principal_type="agent",
            principal_ref=agent_id,
            scope_type=scope_type,
            scope_ref=scope_ref,
            # An agent's authority is its A0 capability bindings, not a
            # permission set; the grant carries WHERE, the bindings
            # carry WHAT. NULL keeps the resolver from narrowing it away.
            permission_subset=None,
            granted_by=granted_by,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def clear_scopes(self, agent_id: str) -> None:
        for row in await self.list_scopes(agent_id):
            await self.session.delete(row)

    # -- capabilities ----------------------------------------------------
    async def list_capabilities(self, agent_id: str) -> Sequence[CCAgentCapability]:
        return (
            await self.session.execute(
                select(CCAgentCapability)
                .where(CCAgentCapability.agent_id == agent_id)
                .order_by(CCAgentCapability.kind, CCAgentCapability.capability_ref)
            )
        ).scalars().all()

    async def add_capability(
        self,
        *,
        agent_id: str,
        tenant_id: str,
        kind: str,
        capability_ref: str,
        config: Optional[dict] = None,
    ) -> CCAgentCapability:
        row = CCAgentCapability(
            agent_id=agent_id,
            tenant_id=tenant_id,
            kind=kind,
            capability_ref=capability_ref,
            config=config,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def clear_capabilities(self, agent_id: str) -> None:
        for row in await self.list_capabilities(agent_id):
            await self.session.delete(row)


class AgentProposalRepo:
    """A1: proposals produced by Operational Agents."""

    #: Statuses that still block a duplicate proposal for the same
    #: (agent, device, action). Mirrors the node action queue's rule:
    #: a persisting condition must not re-propose work already open, and
    #: `denied` blocks too because denial is final (D16).
    OPEN_STATUSES = (
        "proposed",
        "awaiting_approval",
        "approved",
        "dispatched",
        "denied",
    )

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, **kwargs) -> CCAgentProposal:
        row = CCAgentProposal(**kwargs)
        self.session.add(row)
        await self.session.flush()
        return row

    async def get(self, tenant_id: str, proposal_id: str) -> Optional[CCAgentProposal]:
        return (
            await self.session.execute(
                select(CCAgentProposal).where(
                    CCAgentProposal.id == proposal_id,
                    CCAgentProposal.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()

    async def all_dedupe_keys(self, tenant_id: str) -> set[str]:
        """Every condition this tenant's agents have already proposed for.

        Deliberately ALL keys, not just the open ones. A proposal that
        already failed for a given fault will fail again for the same
        fault: the live stack re-proposed a permanently-refused SEL_CLEAR
        on every pass once its first attempt settled. The key carries the
        condition, so a NEW incident is a new proposal and only a repeat
        of the same one is suppressed.
        """
        rows = (
            await self.session.execute(
                select(CCAgentProposal.dedupe_key).where(
                    CCAgentProposal.tenant_id == tenant_id
                )
            )
        ).scalars().all()
        return {k for k in rows if k}

    async def find_open(
        self, tenant_id: str, dedupe_key: str
    ) -> Optional[CCAgentProposal]:
        return (
            await self.session.execute(
                select(CCAgentProposal)
                .where(
                    CCAgentProposal.tenant_id == tenant_id,
                    CCAgentProposal.dedupe_key == dedupe_key,
                    CCAgentProposal.status.in_(self.OPEN_STATUSES),
                )
                .order_by(CCAgentProposal.created_at.desc())
            )
        ).scalars().first()

    async def list_for_agent(
        self, tenant_id: str, agent_id: str, limit: int = 100
    ) -> Sequence[CCAgentProposal]:
        return (
            await self.session.execute(
                select(CCAgentProposal)
                .where(
                    CCAgentProposal.tenant_id == tenant_id,
                    CCAgentProposal.agent_id == agent_id,
                )
                .order_by(CCAgentProposal.created_at.desc())
                .limit(limit)
            )
        ).scalars().all()

    async def list_awaiting_approval(
        self, tenant_id: str, scope=None
    ) -> Sequence[CCAgentProposal]:
        stmt = apply_scope(
            select(CCAgentProposal).where(
                CCAgentProposal.tenant_id == tenant_id,
                CCAgentProposal.status == "awaiting_approval",
            ),
            CCAgentProposal.site_id,
            scope,
        )
        return (
            await self.session.execute(stmt.order_by(CCAgentProposal.created_at))
        ).scalars().all()

    async def list_by_status(
        self, tenant_id: str, statuses: Sequence[str], limit: int = 500
    ) -> Sequence[CCAgentProposal]:
        return (
            await self.session.execute(
                select(CCAgentProposal)
                .where(
                    CCAgentProposal.tenant_id == tenant_id,
                    CCAgentProposal.status.in_(list(statuses)),
                )
                .order_by(CCAgentProposal.created_at)
                .limit(limit)
            )
        ).scalars().all()

    async def count_since(
        self, tenant_id: str, agent_id: str, since: datetime
    ) -> int:
        result = await self.session.execute(
            select(func.count(CCAgentProposal.id)).where(
                CCAgentProposal.tenant_id == tenant_id,
                CCAgentProposal.agent_id == agent_id,
                CCAgentProposal.created_at >= since,
            )
        )
        return result.scalar() or 0

    async def find_by_directive(
        self, directive_id: str
    ) -> Optional[CCAgentProposal]:
        if not directive_id:
            return None
        return (
            await self.session.execute(
                select(CCAgentProposal).where(
                    CCAgentProposal.directive_id == directive_id
                )
            )
        ).scalars().first()

    async def find_open_for_execution(
        self, tenant_id: str, device_agent_id: str, action_type: str
    ) -> Optional[CCAgentProposal]:
        """The dispatched proposal an incoming outcome most likely settles.

        Outcomes arrive keyed by (device, action type), not by proposal
        id, so this is the join. Oldest dispatched first: an outcome
        settles the proposal that has been waiting longest, never a
        newer one that has not executed yet.
        """
        return (
            await self.session.execute(
                select(CCAgentProposal)
                .where(
                    CCAgentProposal.tenant_id == tenant_id,
                    CCAgentProposal.device_agent_id == device_agent_id,
                    CCAgentProposal.action_type == action_type,
                    CCAgentProposal.status == "dispatched",
                )
                .order_by(CCAgentProposal.dispatched_at)
            )
        ).scalars().first()

    async def decide(
        self, proposal: CCAgentProposal, decision: str, decided_by: str
    ) -> None:
        proposal.status = "approved" if decision == "approved" else "denied"
        proposal.decided_by = decided_by
        proposal.decided_at = utcnow()

    async def mark_dispatched(
        self, proposal: CCAgentProposal, directive_id: str, reason: str = ""
    ) -> None:
        proposal.status = "dispatched"
        proposal.directive_id = directive_id
        proposal.dispatch_reason = reason
        proposal.dispatched_at = utcnow()

    async def mark_failed(self, proposal: CCAgentProposal, reason: str) -> None:
        proposal.status = "failed"
        proposal.dispatch_reason = reason[:512]
        proposal.outcome_at = utcnow()

    async def withhold_unattended(
        self, proposal: CCAgentProposal, reason: str
    ) -> None:
        """A2/D2: this may no longer run WITHOUT a human -- but it may run.

        Budget exhaustion (or an agent pause) withdraws the unattended
        grant, it does not destroy the proposal. So the proposal returns
        to the human queue with its basis rewritten, rather than being
        failed: the agent keeps observing and proposing, and a person can
        still approve this exact piece of work.

        This is the shape R3a ratified for the error-budget drop-back --
        `propose`, never `deny` -- applied to the per-agent budget.
        """
        proposal.status = "awaiting_approval"
        proposal.authorization_basis = "human_approval"
        proposal.decided_by = ""
        proposal.decided_at = None
        proposal.dispatch_reason = reason[:512]

    async def count_in_flight(
        self, tenant_id: str, agent_id: str, since: datetime
    ) -> int:
        """Unattended work this agent has already launched but not settled.

        Outcome history is the source of truth for what RAN, but it lags
        dispatch: an outcome only exists once the node has reported. Left
        at that, an agent with a budget of one could dispatch a hundred
        proposals in a single pass, because none of them had come back
        yet -- the budget would be real in the report and meaningless in
        the runtime.

        A dispatched action is an execution in flight, so it counts
        against the allowance. Settled proposals leave `dispatched`, so
        this never double-counts what outcome history already holds.

        Matched on the agent across versions, for the same reason
        `count_executions` is: work launched under v1 is still this
        agent's work after an edit makes it v2.
        """
        return int(
            (
                await self.session.execute(
                    select(func.count(CCAgentProposal.id)).where(
                        CCAgentProposal.tenant_id == tenant_id,
                        CCAgentProposal.agent_id == agent_id,
                        CCAgentProposal.status == "dispatched",
                        CCAgentProposal.dispatched_at >= since,
                    )
                )
            ).scalar()
            or 0
        )

    async def settle(self, proposal: CCAgentProposal, outcome: str) -> None:
        proposal.status = "completed" if outcome == "SUCCESS" else "failed"
        proposal.outcome = outcome
        proposal.outcome_at = utcnow()


class ApprovalRecordRepo:
    """E0.1: the per-approver approval ledger.

    The route's `decision` column is a projection of these rows; these
    rows are the truth. Both origins (node actions and Operational Agent
    proposals) write here, keyed by `subject_type`.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_for_subject(
        self, subject_type: str, subject_ref: str
    ) -> Sequence[CCApprovalRecord]:
        return (
            await self.session.execute(
                select(CCApprovalRecord)
                .where(
                    CCApprovalRecord.subject_type == subject_type,
                    CCApprovalRecord.subject_ref == subject_ref,
                )
                .order_by(CCApprovalRecord.decided_at)
            )
        ).scalars().all()

    async def get_by_approver(
        self, subject_type: str, subject_ref: str, approver_ref: str
    ) -> Optional[CCApprovalRecord]:
        return (
            await self.session.execute(
                select(CCApprovalRecord).where(
                    CCApprovalRecord.subject_type == subject_type,
                    CCApprovalRecord.subject_ref == subject_ref,
                    CCApprovalRecord.approver_ref == approver_ref,
                )
            )
        ).scalar_one_or_none()

    async def record(
        self,
        *,
        tenant_id: str,
        subject_type: str,
        subject_ref: str,
        approver_ref: str,
        approver_email: str,
        decision: str,
        policy_id: str = "",
        scope_ok: bool = True,
        reason: str = "",
        scope_snapshot: Optional[dict] = None,
        authority_snapshot: Optional[dict] = None,
    ) -> CCApprovalRecord:
        row = CCApprovalRecord(
            scope_snapshot=scope_snapshot,
            authority_snapshot=authority_snapshot,
            tenant_id=tenant_id,
            subject_type=subject_type,
            subject_ref=subject_ref,
            approver_ref=approver_ref,
            approver_email=approver_email,
            decision=decision,
            policy_id=policy_id,
            scope_ok=scope_ok,
            reason=reason[:512],
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def map_for_subjects(
        self, subject_type: str, subject_refs: Sequence[str]
    ) -> dict[str, list[CCApprovalRecord]]:
        """Records for many subjects at once, so a queue page is one query."""
        if not subject_refs:
            return {}
        rows = (
            await self.session.execute(
                select(CCApprovalRecord)
                .where(
                    CCApprovalRecord.subject_type == subject_type,
                    CCApprovalRecord.subject_ref.in_(list(subject_refs)),
                )
                .order_by(CCApprovalRecord.decided_at)
            )
        ).scalars().all()
        out: dict[str, list[CCApprovalRecord]] = {}
        for row in rows:
            out.setdefault(row.subject_ref, []).append(row)
        return out


class CampaignRepo:
    """S6 campaigns, their targets, their per-site branches and the
    dispatch ledger.

    Every read is tenant-scoped and takes the E1.2 `scope`, exactly as
    the fleet and incident reads do: a campaign is a fleet-shaped object
    and a scoped principal must not see one that reaches sites they
    cannot.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # -- campaigns ----------------------------------------------------------

    async def create(self, **kw) -> CCCampaign:
        campaign = CCCampaign(**kw)
        self.session.add(campaign)
        await self.session.flush()
        return campaign

    async def get(self, tenant_id: str, campaign_id: str) -> Optional[CCCampaign]:
        row = await self.session.get(CCCampaign, campaign_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return row

    async def list_all(
        self, tenant_id: str, status: Optional[str] = None, scope=None
    ) -> Sequence[CCCampaign]:
        stmt = select(CCCampaign).where(CCCampaign.tenant_id == tenant_id)
        if status:
            stmt = stmt.where(CCCampaign.status == status)
        rows = (
            await self.session.execute(stmt.order_by(CCCampaign.created_at.desc()))
        ).scalars().all()
        if scope is None or getattr(scope, "tenant_wide", False):
            return rows
        # A campaign is visible when the caller can see at least one site
        # it reaches. Filtering on the campaign's own sites rather than a
        # column keeps this consistent with how every other site-anchored
        # read is scoped.
        visible = set(getattr(scope, "site_ids", ()) or ())
        if not visible:
            return []
        site_rows = (
            await self.session.execute(
                select(CCCampaignSite.campaign_id, CCCampaignSite.site_id).where(
                    CCCampaignSite.campaign_id.in_([r.id for r in rows] or [""])
                )
            )
        ).all()
        reach: dict[str, set[str]] = {}
        for campaign_id, site_id in site_rows:
            reach.setdefault(campaign_id, set()).add(site_id)
        return [r for r in rows if reach.get(r.id, set()) & visible]

    async def bump_version(self, campaign: CCCampaign) -> None:
        """An edit is a new configuration context.

        The version bump is what invalidates the acknowledgement and any
        approval taken against the previous one -- otherwise a person
        acknowledges v1 and the estate runs v2.
        """
        campaign.version = int(campaign.version) + 1
        campaign.acknowledged_by = ""
        campaign.acknowledged_at = None
        campaign.acknowledged_version = 0
        campaign.status = "draft"
        campaign.preflight_at = None
        campaign.updated_at = utcnow()
        await self.session.flush()

    # -- targets ------------------------------------------------------------

    async def replace_targets(self, campaign_id: str, rows: list[dict]) -> None:
        await self.session.execute(
            sa_delete(CCCampaignTarget).where(
                CCCampaignTarget.campaign_id == campaign_id
            )
        )
        for row in rows:
            self.session.add(CCCampaignTarget(campaign_id=campaign_id, **row))
        await self.session.flush()

    async def targets(
        self, campaign_id: str, site_id: Optional[str] = None
    ) -> Sequence[CCCampaignTarget]:
        stmt = select(CCCampaignTarget).where(
            CCCampaignTarget.campaign_id == campaign_id
        )
        if site_id:
            stmt = stmt.where(CCCampaignTarget.site_id == site_id)
        return (
            await self.session.execute(
                stmt.order_by(CCCampaignTarget.site_id, CCCampaignTarget.device_agent_id)
            )
        ).scalars().all()

    async def get_target(
        self, campaign_id: str, device_agent_id: str
    ) -> Optional[CCCampaignTarget]:
        return (
            await self.session.execute(
                select(CCCampaignTarget).where(
                    CCCampaignTarget.campaign_id == campaign_id,
                    CCCampaignTarget.device_agent_id == device_agent_id,
                )
            )
        ).scalar_one_or_none()

    # -- per-site branches --------------------------------------------------

    async def replace_sites(self, campaign_id: str, rows: list[dict]) -> None:
        await self.session.execute(
            sa_delete(CCCampaignSite).where(
                CCCampaignSite.campaign_id == campaign_id
            )
        )
        for row in rows:
            self.session.add(CCCampaignSite(campaign_id=campaign_id, **row))
        await self.session.flush()

    async def sites(self, campaign_id: str) -> Sequence[CCCampaignSite]:
        return (
            await self.session.execute(
                select(CCCampaignSite)
                .where(CCCampaignSite.campaign_id == campaign_id)
                .order_by(CCCampaignSite.order_index)
            )
        ).scalars().all()

    async def get_site(
        self, campaign_id: str, site_id: str
    ) -> Optional[CCCampaignSite]:
        return (
            await self.session.execute(
                select(CCCampaignSite).where(
                    CCCampaignSite.campaign_id == campaign_id,
                    CCCampaignSite.site_id == site_id,
                )
            )
        ).scalar_one_or_none()

    # -- dispatch ledger ----------------------------------------------------

    async def already_dispatched(
        self, campaign_id: str, version: int, site_id: str,
        device_agent_id: str, wave_index: int, plan_hash: str,
    ) -> bool:
        """Has this exact device already been dispatched in this wave?

        The composite primary key is the real guarantee; this read exists
        so the caller can skip cleanly rather than provoke an integrity
        error on a replay.
        """
        row = await self.session.get(
            CCCampaignDispatch,
            (campaign_id, version, site_id, device_agent_id, wave_index,
             plan_hash),
        )
        return row is not None

    async def record_dispatch(self, **kw) -> CCCampaignDispatch:
        row = CCCampaignDispatch(**kw)
        self.session.add(row)
        await self.session.flush()
        return row

    async def dispatches(
        self, campaign_id: str
    ) -> Sequence[CCCampaignDispatch]:
        return (
            await self.session.execute(
                select(CCCampaignDispatch).where(
                    CCCampaignDispatch.campaign_id == campaign_id
                )
            )
        ).scalars().all()

    # -- scopes (SELECTION, not an authorization grant) ---------------------

    async def replace_scopes(self, campaign_id: str, rules: list[tuple]) -> None:
        await self.session.execute(
            sa_delete(CCCampaignScope).where(
                CCCampaignScope.campaign_id == campaign_id
            )
        )
        for scope_type, scope_ref in rules:
            self.session.add(CCCampaignScope(
                campaign_id=campaign_id,
                scope_type=scope_type,
                scope_ref=scope_ref,
            ))
        await self.session.flush()

    async def scopes(self, campaign_id: str) -> Sequence[CCCampaignScope]:
        return (
            await self.session.execute(
                select(CCCampaignScope).where(
                    CCCampaignScope.campaign_id == campaign_id
                )
            )
        ).scalars().all()

    # -- plans (IMMUTABLE) --------------------------------------------------

    async def store_plan(self, **kw) -> CCCampaignPlan:
        """Persist a plan exactly as the site computed it.

        Never updates. If this plan hash already exists for this
        (campaign, version, site) it is the same plan and the existing
        row is returned -- which is what makes re-planning idempotent.
        """
        existing = (
            await self.session.execute(
                select(CCCampaignPlan).where(
                    CCCampaignPlan.campaign_id == kw["campaign_id"],
                    CCCampaignPlan.campaign_version == kw["campaign_version"],
                    CCCampaignPlan.site_id == kw["site_id"],
                    CCCampaignPlan.plan_hash == kw["plan_hash"],
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        row = CCCampaignPlan(**kw)
        self.session.add(row)
        await self.session.flush()
        return row

    async def supersede_plans(
        self, campaign_id: str, site_id: str, keep_hash: str
    ) -> int:
        """Stamp every other plan for this site superseded.

        The rows stay: an approval that referenced one must remain
        explicable after the fact, and deleting the plan would erase what
        somebody actually authorized.
        """
        rows = (
            await self.session.execute(
                select(CCCampaignPlan).where(
                    CCCampaignPlan.campaign_id == campaign_id,
                    CCCampaignPlan.site_id == site_id,
                    CCCampaignPlan.plan_hash != keep_hash,
                    CCCampaignPlan.superseded_at.is_(None),
                )
            )
        ).scalars().all()
        for row in rows:
            row.superseded_at = utcnow()
        await self.session.flush()
        return len(rows)

    async def current_plan(
        self, campaign_id: str, site_id: str
    ) -> Optional[CCCampaignPlan]:
        return (
            await self.session.execute(
                select(CCCampaignPlan)
                .where(
                    CCCampaignPlan.campaign_id == campaign_id,
                    CCCampaignPlan.site_id == site_id,
                    CCCampaignPlan.superseded_at.is_(None),
                )
                .order_by(CCCampaignPlan.received_at.desc())
            )
        ).scalars().first()

    # -- waves (approval + execution unit) ----------------------------------

    async def add_wave(self, **kw) -> CCCampaignWave:
        row = CCCampaignWave(**kw)
        self.session.add(row)
        await self.session.flush()
        return row

    async def waves(
        self, campaign_id: str, site_id: Optional[str] = None
    ) -> Sequence[CCCampaignWave]:
        stmt = select(CCCampaignWave).where(
            CCCampaignWave.campaign_id == campaign_id
        )
        if site_id:
            stmt = stmt.where(CCCampaignWave.site_id == site_id)
        return (
            await self.session.execute(
                stmt.order_by(CCCampaignWave.site_id, CCCampaignWave.wave_index)
            )
        ).scalars().all()

    async def wave_by_subject(
        self, subject_ref: str
    ) -> Optional[CCCampaignWave]:
        """Resolve an approval subject back to the wave it authorizes."""
        return (
            await self.session.execute(
                select(CCCampaignWave).where(
                    CCCampaignWave.subject_ref == subject_ref
                )
            )
        ).scalar_one_or_none()

    async def clear_waves(
        self, campaign_id: str, site_id: Optional[str] = None
    ) -> None:
        stmt = sa_delete(CCCampaignWave).where(
            CCCampaignWave.campaign_id == campaign_id
        )
        if site_id:
            stmt = stmt.where(CCCampaignWave.site_id == site_id)
        await self.session.execute(stmt)
        await self.session.flush()


class AgentPreflightRepo:
    """A2: stored activation readiness results, and the skill ledger.

    Preflights are IMMUTABLE. A re-run is a new row and the previous one
    is stamped superseded rather than updated, so the result a person
    actually approved stays explicable after the configuration moves on.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def store(self, **kw) -> CCAgentPreflight:
        row = CCAgentPreflight(**kw)
        self.session.add(row)
        await self.session.flush()
        return row

    async def supersede_all(self, agent_id: str) -> int:
        rows = (
            await self.session.execute(
                select(CCAgentPreflight).where(
                    CCAgentPreflight.agent_id == agent_id,
                    CCAgentPreflight.superseded_at.is_(None),
                )
            )
        ).scalars().all()
        for row in rows:
            row.superseded_at = utcnow()
        await self.session.flush()
        return len(rows)

    async def current(self, agent_id: str) -> Optional[CCAgentPreflight]:
        return (
            await self.session.execute(
                select(CCAgentPreflight)
                .where(
                    CCAgentPreflight.agent_id == agent_id,
                    CCAgentPreflight.superseded_at.is_(None),
                )
                .order_by(CCAgentPreflight.produced_at.desc())
            )
        ).scalars().first()

    async def history(self, agent_id: str) -> Sequence[CCAgentPreflight]:
        return (
            await self.session.execute(
                select(CCAgentPreflight)
                .where(CCAgentPreflight.agent_id == agent_id)
                .order_by(CCAgentPreflight.produced_at.desc())
            )
        ).scalars().all()

    # -- per-device skill installs ------------------------------------------

    async def record_install(self, **kw) -> CCAgentSkillInstall:
        """One row per (agent, version, skill, device). The composite key
        is the guarantee: a re-activation cannot install twice."""
        existing = await self.session.get(
            CCAgentSkillInstall,
            (kw["agent_id"], kw["agent_version"], kw["skill_id"],
             kw["device_agent_id"]),
        )
        if existing is not None:
            return existing
        row = CCAgentSkillInstall(**kw)
        self.session.add(row)
        await self.session.flush()
        return row

    async def installs(
        self, agent_id: str, agent_version: Optional[int] = None
    ) -> Sequence[CCAgentSkillInstall]:
        stmt = select(CCAgentSkillInstall).where(
            CCAgentSkillInstall.agent_id == agent_id
        )
        if agent_version is not None:
            stmt = stmt.where(CCAgentSkillInstall.agent_version == agent_version)
        return (await self.session.execute(stmt)).scalars().all()

    async def already_installed(
        self, agent_id: str, agent_version: int, skill_id: str, device: str
    ) -> bool:
        row = await self.session.get(
            CCAgentSkillInstall, (agent_id, agent_version, skill_id, device)
        )
        return row is not None

    async def count_executions(
        self, tenant_id: str, agent_id: str, since: datetime
    ) -> int:
        """Executions attributed to this AGENT in the budget window (D2).

        Counts what actually RAN, from the existing outcome history --
        not proposals. A proposal that is never executed consumes
        nothing, because intent is not consumption.

        Matched on the agent, ACROSS its configuration versions, and
        that distinction is the whole correctness of the budget. Each
        outcome still records the exact version that decided it (D3
        requires attribution to name the configuration), but the
        allowance belongs to the agent: keyed to `op-agent:<id>@v<n>`
        exactly, editing a description bumped the version and silently
        refilled a spent budget -- so the one control a customer sets to
        bound unattended work was reset by the most routine edit there
        is.

        The prefix is a generated hex id, so it carries no LIKE
        wildcards; the value is parameterised regardless.
        """
        from harkeniq_cc.db.models import CCOutcomeHistory
        from harkeniq_cc.operational_agent import ATTRIBUTION_PREFIX

        return int(
            (
                await self.session.execute(
                    select(func.count(CCOutcomeHistory.id)).where(
                        CCOutcomeHistory.actor.like(
                            f"{ATTRIBUTION_PREFIX}{agent_id}@v%"
                        ),
                        CCOutcomeHistory.recorded_at >= since,
                    )
                )
            ).scalar()
            or 0
        )


class AgentIdentityRepo:
    """A3: the machine-identity ledger (A20.1/A20.5).

    Reads are keyed on (realm, keycloak_sub) because that is what a token
    carries and because an identity is a (realm, subject) fact -- E1.4's
    lesson, applied to a machine principal.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_for_agent(
        self, tenant_id: str, agent_id: str
    ) -> Optional[CCAgentIdentity]:
        return (
            await self.session.execute(
                select(CCAgentIdentity).where(
                    CCAgentIdentity.tenant_id == tenant_id,
                    CCAgentIdentity.agent_id == agent_id,
                )
            )
        ).scalar_one_or_none()

    async def get_by_subject(
        self, realm: str, subject: str
    ) -> Optional[CCAgentIdentity]:
        """The authentication hot path. Deliberately NOT tenant-filtered.

        The tenant is checked by `machine_identity.authenticate`, which
        refuses with a reason. Filtering it away here would turn a
        cross-tenant credential into "no identity" and lose the fact that
        a real identity was presented against the wrong tenant -- which
        is exactly what an operator needs to see in the audit log.
        """
        if not subject:
            return None
        return (
            await self.session.execute(
                select(CCAgentIdentity).where(
                    CCAgentIdentity.realm == (realm or ""),
                    CCAgentIdentity.keycloak_sub == subject,
                )
            )
        ).scalar_one_or_none()

    async def list_for_tenant(
        self, tenant_id: str
    ) -> Sequence[CCAgentIdentity]:
        return (
            await self.session.execute(
                select(CCAgentIdentity)
                .where(CCAgentIdentity.tenant_id == tenant_id)
                .order_by(CCAgentIdentity.issued_at)
            )
        ).scalars().all()

    async def create(
        self, *, tenant_id: str, agent_id: str, realm: str,
        keycloak_client_id: str, keycloak_sub: str, issued_by: str,
    ) -> CCAgentIdentity:
        row = CCAgentIdentity(
            tenant_id=tenant_id, agent_id=agent_id, realm=realm,
            keycloak_client_id=keycloak_client_id, keycloak_sub=keycloak_sub,
            status="active", issued_by=issued_by, issued_at=utcnow(),
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def mark_rotated(self, row: CCAgentIdentity, actor: str) -> None:
        """Rotation changes the SECRET, never the identity.

        One client, one subject, one row -- so there is never a moment
        with two identities for one agent. Tokens already issued stay
        valid to their natural expiry, which is what makes rotation
        gapless.
        """
        row.rotated_at = utcnow()
        row.rotated_by = actor

    async def mark_revoked(
        self, row: CCAgentIdentity, actor: str, reason: str,
        status: str = "revoked",
    ) -> None:
        row.status = status
        row.revoked_at = utcnow()
        row.revoked_by = actor
        row.revoke_reason = reason[:512]

    async def touch(self, row: CCAgentIdentity, source: str) -> None:
        """Record that this identity was used. OBSERVATION ONLY.

        `last_seen_source` is caller-supplied and therefore never an
        authorization input -- a principal that could influence its own
        authorization by choosing a string would be a second
        authorization model.
        """
        row.last_seen_at = utcnow()
        if source:
            row.last_seen_source = source[:255]


class CapabilityCatalogueRepo:
    """A4: the condition -> capability catalogue (A21.1).

    Seeds lazily on first read for a tenant the migration did not cover
    (one created after the upgrade). The seed is read from the SEED
    constant, never duplicated here -- a second copy would drift from the
    one the runtime reads.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_for_tenant(
        self, tenant_id: str, *, seed_if_empty: bool = True
    ) -> Sequence[CCCapabilityCatalogue]:
        rows = (
            await self.session.execute(
                select(CCCapabilityCatalogue)
                .where(CCCapabilityCatalogue.tenant_id == tenant_id)
                .order_by(
                    CCCapabilityCatalogue.subsystem,
                    CCCapabilityCatalogue.action_type,
                )
            )
        ).scalars().all()
        if rows or not seed_if_empty:
            return rows
        await self.seed(tenant_id, actor="platform-default")
        return (
            await self.session.execute(
                select(CCCapabilityCatalogue)
                .where(CCCapabilityCatalogue.tenant_id == tenant_id)
                .order_by(
                    CCCapabilityCatalogue.subsystem,
                    CCCapabilityCatalogue.action_type,
                )
            )
        ).scalars().all()

    async def seed(self, tenant_id: str, actor: str) -> int:
        """Write the platform default catalogue for a tenant. Idempotent."""
        from harkeniq_cc.capability_catalogue import SEED

        existing = {
            (r.subsystem, r.action_type)
            for r in await self.list_for_tenant(tenant_id, seed_if_empty=False)
        }
        written = 0
        for entry in SEED:
            if (entry["subsystem"], entry["action_type"]) in existing:
                continue
            self.session.add(CCCapabilityCatalogue(
                tenant_id=tenant_id, subsystem=entry["subsystem"],
                action_type=entry["action_type"], because=entry["because"],
                provenance=entry["provenance"], enabled=True,
                created_by=actor, updated_by=actor,
            ))
            written += 1
        if written:
            await self.session.flush()
        return written

    async def replace(
        self, tenant_id: str, entries: list[dict], actor: str
    ) -> int:
        """Full replacement of a tenant's catalogue.

        Replacement rather than patch, for the reason A0's bindings are:
        an operator reasoning about what their agents may propose should
        see the complete set in one request, not reconstruct it from a
        history of deltas.
        """
        for row in await self.list_for_tenant(tenant_id, seed_if_empty=False):
            await self.session.delete(row)
        await self.session.flush()
        for entry in entries:
            self.session.add(CCCapabilityCatalogue(
                tenant_id=tenant_id,
                subsystem=str(entry["subsystem"]).lower(),
                action_type=str(entry["action_type"]).upper(),
                because=str(entry.get("because", ""))[:512],
                provenance=str(entry.get("provenance", ""))[:255],
                enabled=bool(entry.get("enabled", True)),
                created_by=actor, updated_by=actor,
            ))
        await self.session.flush()
        return len(entries)
