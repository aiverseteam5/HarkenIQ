"""Thin data-access helpers. Each repo wraps one AsyncSession.

Commit responsibility stays with the caller (one commit per API request)
so multi-table updates remain atomic.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Iterable, Any, Optional, Sequence

from sqlalchemy import (
    DateTime,
    Float,
    and_,
    bindparam,
    case,
    delete as sa_delete,
    exists,
    false as sa_false,
    func,
    literal,
    or_,
    select,
    tuple_,
    update as sa_update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from harkeniq_cc.scope import ReadReach
from harkeniq_cc.resource_catalogue import (
    LEGACY_DEVICE,
    LEGACY_KINDS,
    LEGACY_RACK,
    LEGACY_SITE,
    LIFECYCLE_ACTIVE,
    LIFECYCLE_RETIRED,
    MAX_CONTAINMENT_DEPTH,
    MAX_CONTAINMENT_WALK,
    MAX_TOPOLOGY_DEPTH,
    MAX_TOPOLOGY_NODES,
    REL_CONTAINS,
    SOURCE_LEGACY_MIRROR,
    CatalogueError,
    IdentityConflict,
    validate_primary_containment,
    validate_relationship_type,
    validate_type,
)
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
    CCAgentIngressAttempt,
    CCAgentReadWindow,
    CCAgentThrottleWindow,
    CCAgentProposal,
    CCAgentSubmission,
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
    CCResource,
    CCResourceExternalId,
    CCResourceRelationship,
    CCSafetyState,
    CCSite,
    CCSiteTopology,
    CCSkillDelivery,
    CCStopSwitch,
    CCUsageSnapshot,
    CCWarranty,
    utcnow,
)


def require_read_reach(scope) -> Optional[ReadReach]:
    """The repository boundary of A30.24: `None` or a `ReadReach`.

    A repository filter used to read `tenant_wide` and `site_ids` by
    `getattr`, so it accepted a bare `ResolvedScope` -- whose projections
    are permission-NEUTRAL. A read then took its permission from the role
    and its reach from any grant at all, and a grant narrowed to
    `incident.view` at one site made that site's fleet readable (P1).

    `None` is an internal caller (a poller, the evaluator): no user is
    asking. Anything else must be `scope.read_reach(resolved, <the
    permission this read requires>)`. A bare scope RAISES rather than
    filtering, because a filter that quietly works is exactly how the
    permission-neutral path would come back.
    """
    if scope is None or isinstance(scope, ReadReach):
        return scope
    raise TypeError(
        "a repository read filter needs a permission-aware ReadReach "
        "(harkeniq_cc.scope.read_reach(scope, <permission>)), not "
        f"{type(scope).__name__}: ResolvedScope.site_ids is permission-"
        "neutral and is never a read filter (spec A30.24)"
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

    A30.24: `scope` is a `ReadReach` -- the reach of the grants that carry
    the permission this read requires -- never a bare `ResolvedScope`.
    """
    scope = require_read_reach(scope)
    if scope is None or scope.tenant_wide:
        return None
    site_ids = sorted(scope.site_ids)
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
    scope = require_read_reach(scope)
    if scope is None or scope.tenant_wide:
        return stmt
    site_ids = sorted(scope.site_ids)
    if not site_ids:
        return stmt.where(sa_false())
    return stmt.where(CCAuditLog.site_id.in_(site_ids))


def _actor_matches(actor: str):
    """Dual-form actor filter (A23-2).

    A caller asking for one actor may hold the stable reference (matches
    `actor_ref` on new rows) or a legacy display string (matches `actor`
    on historical rows, which were never rewritten). Both forms answer,
    so the history of a principal does not split at the migration.
    """
    return (CCAuditLog.actor_ref == actor) | (CCAuditLog.actor == actor)


def apply_scope(stmt, column, scope):
    """Conjoin `scope_sites` onto a statement when it applies.

    For a SITE-owned table only. A table whose rows name a device is
    filtered by `scope_fleet_devices` or `scope_device_owned` (A30.25): a
    site filter on such a table is what made a `device` or `device_class`
    grant read nothing (F1).
    """
    condition = scope_sites(column, scope)
    return stmt if condition is None else stmt.where(condition)


# ---------------------------------------------------------------------------
# A30.25 (A6-4B0b): device-bearing tables. Two predicates, by name.
#
# The owner rule: a row that names a device is DEVICE-owned when that
# device currently resolves in the fleet cache AT THE ROW'S OWN SITE, and
# SITE-owned otherwise. A device-owned row is readable when one grant that
# carries the read's permission covers the device -- by site, by id, or by
# the device's CURRENT class. A site-owned row is readable when such a
# grant covers the site. It is `Grant.covers_device` asked of the target as
# it currently resolves; `ReadReach.covers_owned` is the same rule in
# Python, and a generated matrix holds the two equal on both engines.
#
# Not one generic filter, on purpose. The fleet cache is its own
# resolution; every other table has to ASK it, because only
# `cc_fleet_cache` carries `device_class`. A reader should be able to see
# which of the two it is looking at.
#
# For a reach with no device and no class -- every tenant, org-unit and
# site principal -- each predicate is the single clause
# ``site_col IN (...)`` and compiles to exactly what `scope_sites` does.
# ---------------------------------------------------------------------------


def scope_fleet_devices(scope) -> Any:
    """The `cc_fleet_cache` rows a reach covers: site, device id, or class.

    Every fleet row resolves itself, so this is `covers_device` verbatim.
    `lower()` because `Grant.covers_device` compares class
    case-insensitively; a blank class is in no reach (`read_reach` drops
    it) and so never matches (R1).
    """
    reach = require_read_reach(scope)
    if reach is None or reach.tenant_wide:
        return None
    clauses = []
    if reach.site_ids:
        clauses.append(CCFleetCache.site_id.in_(sorted(reach.site_ids)))
    if reach.device_ids:
        clauses.append(CCFleetCache.agent_id.in_(sorted(reach.device_ids)))
    if reach.device_classes:
        clauses.append(
            func.lower(CCFleetCache.device_class).in_(sorted(reach.device_classes))
        )
    if not clauses:
        return sa_false()
    return or_(*clauses)


def scope_device_owned(site_column, device_column, scope) -> Any:
    """The rows of a device-bearing table a reach covers, by the owner rule.

    ``site IN sites`` covers a row whoever owns it -- a site administrator
    reads an incident on a device Central Command has never heard of. The
    device half applies only to a row that NAMES a device (a blank device
    column is a site-owned row: a correlated parent incident, say) whose
    device RESOLVES in the fleet cache at that row's own site, and is then
    covered by id or by its current class. A device that does not resolve
    is natural zero (R9): nothing is revoked, and nothing is read.

    The fleet table is aliased so the `EXISTS` correlates to the OUTER
    row's columns and never to a fleet join the caller may already have.
    """
    reach = require_read_reach(scope)
    if reach is None or reach.tenant_wide:
        return None
    clauses = []
    if reach.site_ids:
        clauses.append(site_column.in_(sorted(reach.site_ids)))
    if reach.device_ids or reach.device_classes:
        fleet = aliased(CCFleetCache)
        covered = []
        if reach.device_ids:
            covered.append(fleet.agent_id.in_(sorted(reach.device_ids)))
        if reach.device_classes:
            covered.append(
                func.lower(fleet.device_class).in_(sorted(reach.device_classes))
            )
        clauses.append(
            and_(
                device_column != "",
                # Correlate everything but the aliased fleet table to the
                # enclosing query, explicitly: the outer row's own site
                # and device decide, whatever else the caller has joined.
                exists().where(
                    fleet.agent_id == device_column,
                    fleet.site_id == site_column,
                    or_(*covered),
                ).correlate_except(fleet),
            )
        )
    if not clauses:
        return sa_false()
    return or_(*clauses)


def scope_resources(scope, resource=None) -> Any:
    """The `cc_resources` rows a reach may read (TX-1, spec A31.4).

    DERIVED, never stored: a resource is visible exactly when its
    CANONICAL legacy object is, asked through the predicates the canonical
    reads already use -- so the catalogue cannot see further than Sites
    and Fleet, and cannot see less. Nothing in the catalogue is an input:
    no relationship, no type, no name, no external identifier. By legacy
    kind:

    * ``site`` -- `scope_sites` over its own id: exactly `SiteRepo.list_all`,
      the AUTHORITATIVE site read. The contextual projection of A30.25 D2
      is not extended here.
    * ``device`` -- LIVE (a fleet row in this tenant carries its
      `agent_id`): visible when one of those rows passes
      `scope_fleet_devices`, i.e. exactly the fleet list -- a moved device
      and one transiently reported at two sites included. RETIRED (no
      fleet row): site-owned at its last site, the A30.25 owner rule under
      which a device that no longer resolves owns nothing (R9).
    * ``rack`` -- SITE-OWNED (TX-2, A32.5), live or retired: `scope_sites`
      over the site it was reported at -- the same answer its site's own
      resource gets. A device- or class-scoped reader sees no rack, exactly
      as it sees no site.
    * no legacy link -- tenant-owned: no scoped reader sees one. No writer
      produces one yet (A32.6).
    * any other legacy kind -- no scoped reader sees it. Fail closed.

    `resource` is the entity being filtered -- an alias, for the far end of
    a relationship. The fleet and site tables are the subqueries' own and
    correlation is explicit, so a caller that has joined either table
    cannot change what the subquery means.
    """
    if resource is None:
        resource = CCResource
    reach = require_read_reach(scope)
    if reach is None or reach.tenant_wide:
        return None
    site_rule = scope_sites(resource.legacy_site_id, reach)

    def _fleet_row(*conditions):
        # The site join is an explicit JOIN ... ON, not a WHERE term: an
        # empty reach makes `scope_fleet_devices` false(), which collapses
        # the whole WHERE -- and a join held there would collapse with it.
        return (
            select(CCFleetCache.id)
            .join(CCSite, CCSite.id == CCFleetCache.site_id)
            .where(
                CCFleetCache.agent_id == resource.legacy_ref,
                CCSite.tenant_id == resource.tenant_id,
                *conditions,
            )
            .correlate(resource)
            .exists()
        )

    live = _fleet_row()
    admitted = _fleet_row(scope_fleet_devices(reach))
    return or_(
        and_(resource.legacy_kind == LEGACY_SITE, site_rule),
        and_(resource.legacy_kind == LEGACY_RACK, site_rule),
        and_(
            resource.legacy_kind == LEGACY_DEVICE,
            or_(admitted, and_(~live, site_rule)),
        ),
    )


def _where(stmt, condition):
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
        """The sites the caller holds AUTHORITY over. Authoritative, always.

        `sites_count`, site reach, delegation ceilings and administration
        are computed from this and from nothing else. A site that merely
        contains a device the caller reads is NOT here: see `list_context`.
        """
        stmt = apply_scope(
            select(CCSite).where(CCSite.tenant_id == tenant_id), CCSite.id, scope
        )
        return (
            await self.session.execute(stmt.order_by(CCSite.site_name))
        ).scalars().all()

    async def list_context(self, tenant_id: str, scope=None) -> Sequence[CCSite]:
        """The sites that CONTAIN a device the caller reads, and nothing more.

        A30.25 (D2, R10): context is not authority. A device- or
        device_class-scoped principal looking at its own device needs to
        know which site that device is at; it does not thereby hold the
        site. These rows exist to be PROJECTED as ``{id, site_name,
        contextual: true}`` and to name a site in a payload about the
        caller's own devices. They are deliberately a separate read from
        `list_all`: nothing here is written into a `ResolvedScope` or a
        `ReadReach`, `covers_site()` stays false for every one of them,
        and every site mutation -- already gated on `permits(site_id=...)`
        -- refuses them.

        Permission-aware, because it starts from the reach: a device grant
        that does not carry the read's permission yields no context.
        Sites the caller already holds are excluded; a tenant-wide or
        internal caller has no contextual sites at all.
        """
        reach = require_read_reach(scope)
        if reach is None or reach.tenant_wide:
            return []
        if not (reach.device_ids or reach.device_classes):
            return []
        fleet = aliased(CCFleetCache)
        covered = []
        if reach.device_ids:
            covered.append(fleet.agent_id.in_(sorted(reach.device_ids)))
        if reach.device_classes:
            covered.append(
                func.lower(fleet.device_class).in_(sorted(reach.device_classes))
            )
        stmt = select(CCSite).where(
            CCSite.tenant_id == tenant_id,
            exists()
            .where(fleet.site_id == CCSite.id, or_(*covered))
            .correlate_except(fleet),
        )
        if reach.site_ids:
            stmt = stmt.where(CCSite.id.notin_(sorted(reach.site_ids)))
        return (
            await self.session.execute(stmt.order_by(CCSite.site_name))
        ).scalars().all()

    async def names_in_view(self, tenant_id: str, scope=None) -> dict[str, str]:
        """site id -> name, for naming a site in a payload about the
        caller's own rows: authoritative sites plus contextual ones. A name
        map and nothing else -- it is never a reach answer."""
        rows = list(await self.list_all(tenant_id, scope=scope))
        rows += list(await self.list_context(tenant_id, scope=scope))
        return {s.id: s.site_name for s in rows}

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
        # A30.25: a fleet row is covered by site, by device id, or by its
        # own class -- not by site alone, which read nothing for a
        # `device` or `device_class` grant (F1).
        stmt = _where(
            select(CCFleetCache)
            .join(CCSite, CCSite.id == CCFleetCache.site_id)
            .where(CCSite.tenant_id == tenant_id),
            scope_fleet_devices(scope),
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
        covered = scope_fleet_devices(scope)
        stmt = _where(
            select(CCFleetCache)
            .join(CCSite, CCSite.id == CCFleetCache.site_id)
            .where(CCSite.tenant_id == tenant_id),
            covered,
        )
        # The COUNT is scoped too, by the SAME condition: a total that
        # counted rows the caller cannot see would leak the size of the
        # rest of the fleet.
        count_stmt = _where(
            select(func.count(CCFleetCache.id))
            .join(CCSite, CCSite.id == CCFleetCache.site_id)
            .where(CCSite.tenant_id == tenant_id),
            covered,
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

    async def get_at_site(
        self, site_id: str, agent_id: str
    ) -> Optional[CCFleetCache]:
        """The device as it resolves AT THIS SITE, or None (A30.25).

        An identity lookup for the owner rule, never a visibility answer:
        a device that has moved sites is not the device a row at the old
        site is about.
        """
        return (
            await self.session.execute(
                select(CCFleetCache)
                .where(CCFleetCache.site_id == site_id)
                .where(CCFleetCache.agent_id == agent_id)
                .limit(1)
            )
        ).scalars().first()

    async def count_by_health(self, tenant_id: str, scope=None) -> dict[str, int]:
        """Return {health_status: count} for the devices the caller may see."""
        stmt = _where(
            select(CCFleetCache.health, func.count(CCFleetCache.id))
            .join(CCSite, CCSite.id == CCFleetCache.site_id)
            .where(CCSite.tenant_id == tenant_id),
            scope_fleet_devices(scope),
        ).group_by(CCFleetCache.health)
        rows = (await self.session.execute(stmt)).all()
        return {row[0]: row[1] for row in rows}

    async def count_total(self, tenant_id: str, scope=None) -> int:
        result = await self.session.execute(
            _where(
                select(func.count(CCFleetCache.id))
                .join(CCSite, CCSite.id == CCFleetCache.site_id)
                .where(CCSite.tenant_id == tenant_id),
                scope_fleet_devices(scope),
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


class ResourceRepo:
    """The Resource Catalogue (TX-1 spec A31, TX-2 spec A32). Describes;
    never authorizes.

    Two kinds of read, deliberately apart. The `*_visible` reads take a
    permission-aware `ReadReach` (or None for an internal caller) and every
    one of them filters EVERY resource it returns through
    `scope_resources`, so a hidden resource is indistinguishable from one
    that does not exist -- a traversal included: it stops at, or skips, a
    resource its reader cannot read, and never shows what lies beyond one.
    The unscoped reads (`get`, `get_by_legacy`, `list_mirrored`, ...) exist
    for the mirror and must never back a principal's request.

    The write primitives enforce A31.2 and A32.3 on every caller: closed
    kinds, the built-in type registry, physical-only primary containment,
    no containment cycle over any active CONTAINS edge, edges only between
    live resources of one tenant, one active primary parent, and no silent
    identity merge. An edge is retired and revived, never rewritten or
    deleted (A32.2). Every write runs inside the mirror's per-tenant
    advisory lock -- the mirror is the one writer -- so a check and the
    write it guards cannot interleave with another.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # -- scoped reads ---------------------------------------------------

    async def list_visible(
        self,
        tenant_id: str,
        *,
        scope,
        resource_kind: Optional[str] = None,
        resource_type: Optional[str] = None,
        lifecycle_state: Optional[str] = LIFECYCLE_ACTIVE,
        external: Optional[tuple[str, str, str]] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[Sequence[CCResource], int]:
        """A page of the resources `scope` may read, and their count.

        `lifecycle_state=None` means any. `external` is an exact
        `(source_system, namespace, external_id)` lookup -- conjoined with
        visibility, so a hidden resource's identifier resolves to nothing.
        The total is filtered by the SAME condition as the page.
        """
        conditions = [CCResource.tenant_id == tenant_id]
        if resource_kind:
            conditions.append(CCResource.resource_kind == resource_kind)
        if resource_type:
            conditions.append(CCResource.resource_type == resource_type)
        if lifecycle_state:
            conditions.append(CCResource.lifecycle_state == lifecycle_state)
        if external is not None:
            source_system, namespace, external_id = external
            conditions.append(
                exists()
                .where(
                    CCResourceExternalId.resource_id == CCResource.id,
                    CCResourceExternalId.tenant_id == tenant_id,
                    CCResourceExternalId.source_system == source_system,
                    CCResourceExternalId.namespace == namespace,
                    CCResourceExternalId.external_id == external_id,
                )
                .correlate(CCResource)
            )
        visible = scope_resources(scope)
        stmt = _where(select(CCResource).where(*conditions), visible)
        count_stmt = _where(
            select(func.count(CCResource.id)).where(*conditions), visible
        )
        total = (await self.session.execute(count_stmt)).scalar() or 0
        stmt = (
            stmt.order_by(
                CCResource.resource_kind, CCResource.display_name, CCResource.id
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return (await self.session.execute(stmt)).scalars().all(), total

    async def get_visible(
        self, tenant_id: str, resource_id: str, *, scope
    ) -> Optional[CCResource]:
        stmt = _where(
            select(CCResource).where(
                CCResource.tenant_id == tenant_id, CCResource.id == resource_id
            ),
            scope_resources(scope),
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def primary_parent_visible(
        self, tenant_id: str, resource_id: str, *, scope
    ) -> Optional[CCResource]:
        """The ACTIVE primary containment parent, or None -- None also when
        the parent exists and the reader cannot see it: the two answers are
        deliberately the same."""
        found = await self._primary_parent_with_edge(
            tenant_id, resource_id, scope=scope
        )
        return found[1] if found is not None else None

    async def _primary_parent_with_edge(
        self, tenant_id: str, resource_id: str, *, scope
    ) -> Optional[tuple[CCResourceRelationship, CCResource]]:
        parent = aliased(CCResource)
        rel = CCResourceRelationship
        stmt = _where(
            select(rel, parent)
            .join(parent, rel.from_resource_id == parent.id)
            .where(
                rel.tenant_id == tenant_id,
                parent.tenant_id == tenant_id,
                rel.to_resource_id == resource_id,
                rel.relationship_type == REL_CONTAINS,
                rel.is_primary.is_(True),
                rel.lifecycle_state == LIFECYCLE_ACTIVE,
            ),
            scope_resources(scope, parent),
        )
        row = (await self.session.execute(stmt)).first()
        return (row[0], row[1]) if row is not None else None

    async def ancestors_visible(
        self, tenant_id: str, resource_id: str, *, scope
    ) -> list[tuple[CCResourceRelationship, CCResource]]:
        """The ACTIVE primary-containment ancestors, nearest first (A32.8).

        The chain stops at the first ancestor the reader cannot see, and
        nothing above it is returned -- so a chain cut by a hidden ancestor
        reads exactly as a chain that ends at a root, and reveals nothing
        about what lies beyond. The caller must already have established
        that `resource_id` itself is visible. Bounded by
        MAX_CONTAINMENT_DEPTH, and by a cycle guard that would only ever
        fire on corrupted data: the write side refuses a cycle.
        """
        out: list[tuple[CCResourceRelationship, CCResource]] = []
        seen = {resource_id}
        cursor = resource_id
        while len(out) < MAX_CONTAINMENT_DEPTH:
            found = await self._primary_parent_with_edge(
                tenant_id, cursor, scope=scope
            )
            if found is None or found[1].id in seen:
                break
            out.append(found)
            seen.add(found[1].id)
            cursor = found[1].id
        return out

    def _children_stmt(
        self,
        tenant_id: str,
        parent_ids: Sequence[str],
        *,
        scope,
        lifecycle_state: Optional[str],
    ):
        """Active primary-containment edges out of `parent_ids` whose child
        the reader may see, with the child. The ONE definition every
        downward read shares."""
        rel = CCResourceRelationship
        child = aliased(CCResource)
        conditions = [
            rel.tenant_id == tenant_id,
            child.tenant_id == tenant_id,
            rel.from_resource_id.in_(list(parent_ids)),
            rel.relationship_type == REL_CONTAINS,
            rel.is_primary.is_(True),
            rel.lifecycle_state == LIFECYCLE_ACTIVE,
        ]
        if lifecycle_state:
            conditions.append(child.lifecycle_state == lifecycle_state)
        visible = scope_resources(scope, child)
        stmt = _where(
            select(rel, child)
            .select_from(rel)
            .join(child, child.id == rel.to_resource_id)
            .where(*conditions),
            visible,
        )
        count_stmt = _where(
            select(func.count(rel.id))
            .select_from(rel)
            .join(child, child.id == rel.to_resource_id)
            .where(*conditions),
            visible,
        )
        order = (child.resource_kind, child.display_name, child.id)
        return stmt, count_stmt, order

    async def children_visible(
        self,
        tenant_id: str,
        resource_id: str,
        *,
        scope,
        lifecycle_state: Optional[str] = LIFECYCLE_ACTIVE,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[tuple[CCResourceRelationship, CCResource]], int]:
        """The resources `resource_id` primarily contains that the reader
        may see, paginated, and their count (A32.8).

        A hidden child is not returned and not counted. `lifecycle_state`
        filters the CHILD (None = any); the edge is always the active one.
        The caller must already have established that `resource_id` is
        visible."""
        stmt, count_stmt, order = self._children_stmt(
            tenant_id, [resource_id], scope=scope, lifecycle_state=lifecycle_state,
        )
        total = (await self.session.execute(count_stmt)).scalar() or 0
        rows = (
            await self.session.execute(
                stmt.order_by(*order)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        return [(edge, child) for edge, child in rows], total

    async def subtree_visible(
        self,
        tenant_id: str,
        resource_id: str,
        *,
        scope,
        depth: int,
        lifecycle_state: Optional[str] = LIFECYCLE_ACTIVE,
        max_nodes: int = MAX_TOPOLOGY_NODES,
    ) -> tuple[list[tuple[CCResourceRelationship, CCResource, int]], bool]:
        """The visible primary-containment subtree under `resource_id`,
        breadth first, to `depth` levels and `max_nodes` nodes (A32.8).

        A hidden resource is not returned, and the walk never descends
        through one -- what lies under it is not shown either. Returns
        ``(edge, node, level)`` and whether the walk was cut at
        `max_nodes`; the cut counts VISIBLE nodes only, so it tells the
        reader nothing they could not find by listing children. The caller
        must already have established that `resource_id` is visible.
        """
        depth = max(1, min(depth, MAX_TOPOLOGY_DEPTH))
        out: list[tuple[CCResourceRelationship, CCResource, int]] = []
        seen = {resource_id}
        frontier = [resource_id]
        for level in range(1, depth + 1):
            if not frontier:
                break
            room = max_nodes - len(out)
            stmt, _count, order = self._children_stmt(
                tenant_id, frontier, scope=scope, lifecycle_state=lifecycle_state,
            )
            rel = CCResourceRelationship
            rows = (
                await self.session.execute(
                    stmt.order_by(rel.from_resource_id, *order).limit(room + 1)
                )
            ).all()
            frontier = []
            for edge, node in rows:
                if node.id in seen:          # only corrupted data cycles
                    continue
                if len(out) >= max_nodes:
                    return out, True
                seen.add(node.id)
                out.append((edge, node, level))
                frontier.append(node.id)
        return out, False

    async def relationships_visible(
        self,
        tenant_id: str,
        resource_id: str,
        *,
        scope,
        relationship_type: Optional[str] = None,
        direction: Optional[str] = None,
        lifecycle_state: Optional[str] = LIFECYCLE_ACTIVE,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[tuple[CCResourceRelationship, CCResource, str]], int]:
        """The edges touching `resource_id` whose OTHER end `scope` may read.

        Returns ``(edge, other, direction)``, direction ``outgoing`` when
        `resource_id` is the edge's subject ("<this> TYPE <other>") and
        ``incoming`` otherwise. The caller must already have established
        that `resource_id` itself is visible. An edge to a resource the
        reader cannot see is not returned and not counted.
        `lifecycle_state` filters the EDGE (A32.2): the current edges by
        default, former ones with ``retired``, both with None.
        """
        rel = CCResourceRelationship
        other = aliased(CCResource)
        outgoing = and_(
            rel.from_resource_id == resource_id, other.id == rel.to_resource_id
        )
        incoming = and_(
            rel.to_resource_id == resource_id, other.id == rel.from_resource_id
        )
        if direction == "outgoing":
            on = outgoing
        elif direction == "incoming":
            on = incoming
        else:
            on = or_(outgoing, incoming)
        conditions = [rel.tenant_id == tenant_id, other.tenant_id == tenant_id]
        if relationship_type:
            conditions.append(rel.relationship_type == relationship_type)
        if lifecycle_state:
            conditions.append(rel.lifecycle_state == lifecycle_state)
        visible = scope_resources(scope, other)
        heading = case(
            (rel.from_resource_id == resource_id, literal("outgoing")),
            else_=literal("incoming"),
        )
        stmt = _where(
            select(rel, other, heading)
            .select_from(rel)
            .join(other, on)
            .where(*conditions),
            visible,
        )
        count_stmt = _where(
            select(func.count(rel.id))
            .select_from(rel)
            .join(other, on)
            .where(*conditions),
            visible,
        )
        total = (await self.session.execute(count_stmt)).scalar() or 0
        stmt = (
            stmt.order_by(
                rel.relationship_type, heading, other.display_name, rel.id
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = (await self.session.execute(stmt)).all()
        return [(edge, far, way) for edge, far, way in rows], total

    async def external_ids(
        self, tenant_id: str, resource_id: str
    ) -> Sequence[CCResourceExternalId]:
        """The identifiers bound to ONE resource. The caller must already
        have established that the resource is visible."""
        return (
            await self.session.execute(
                select(CCResourceExternalId)
                .where(
                    CCResourceExternalId.tenant_id == tenant_id,
                    CCResourceExternalId.resource_id == resource_id,
                )
                .order_by(
                    CCResourceExternalId.source_system,
                    CCResourceExternalId.namespace,
                    CCResourceExternalId.external_id,
                )
            )
        ).scalars().all()

    # -- unscoped reads, for the mirror only -----------------------------

    async def get(self, tenant_id: str, resource_id: str) -> Optional[CCResource]:
        return (
            await self.session.execute(
                select(CCResource).where(
                    CCResource.tenant_id == tenant_id, CCResource.id == resource_id
                )
            )
        ).scalar_one_or_none()

    async def get_by_legacy(
        self, tenant_id: str, legacy_kind: str, legacy_ref: str
    ) -> Optional[CCResource]:
        return (
            await self.session.execute(
                select(CCResource).where(
                    CCResource.tenant_id == tenant_id,
                    CCResource.legacy_kind == legacy_kind,
                    CCResource.legacy_ref == legacy_ref,
                )
            )
        ).scalar_one_or_none()

    async def list_mirrored(
        self, tenant_id: str, legacy_kind: str
    ) -> Sequence[CCResource]:
        return (
            await self.session.execute(
                select(CCResource)
                .where(
                    CCResource.tenant_id == tenant_id,
                    CCResource.legacy_kind == legacy_kind,
                )
                .order_by(CCResource.id)
            )
        ).scalars().all()

    async def primary_parent_edge(
        self, child_id: str
    ) -> Optional[CCResourceRelationship]:
        """The ACTIVE primary containment edge into `child_id`, if any."""
        rel = CCResourceRelationship
        return (
            await self.session.execute(
                select(rel).where(
                    rel.to_resource_id == child_id,
                    rel.relationship_type == REL_CONTAINS,
                    rel.is_primary.is_(True),
                    rel.lifecycle_state == LIFECYCLE_ACTIVE,
                )
            )
        ).scalars().first()

    async def primary_parent_edges(
        self, tenant_id: str, child_ids: Iterable[str]
    ) -> dict[str, CCResourceRelationship]:
        """child id -> its ACTIVE primary containment edge, for many."""
        ids = sorted(set(child_ids))
        if not ids:
            return {}
        rel = CCResourceRelationship
        rows = (
            await self.session.execute(
                select(rel).where(
                    rel.tenant_id == tenant_id,
                    rel.to_resource_id.in_(ids),
                    rel.relationship_type == REL_CONTAINS,
                    rel.is_primary.is_(True),
                    rel.lifecycle_state == LIFECYCLE_ACTIVE,
                )
            )
        ).scalars().all()
        return {edge.to_resource_id: edge for edge in rows}

    async def bound_external_ids(
        self, tenant_id: str, source_system: str, namespace: str
    ) -> dict[str, str]:
        """external id -> resource id, for one namespace."""
        rows = (
            await self.session.execute(
                select(
                    CCResourceExternalId.external_id,
                    CCResourceExternalId.resource_id,
                ).where(
                    CCResourceExternalId.tenant_id == tenant_id,
                    CCResourceExternalId.source_system == source_system,
                    CCResourceExternalId.namespace == namespace,
                )
            )
        ).all()
        return {external_id: resource_id for external_id, resource_id in rows}

    # -- write primitives -------------------------------------------------

    async def create_mirrored(
        self,
        tenant_id: str,
        *,
        legacy_kind: str,
        legacy_ref: str,
        legacy_site_id: Optional[str],
        resource_kind: str,
        resource_type: str,
        display_name: str,
    ) -> CCResource:
        """A resource standing for one canonical object (A31.3).

        The legacy link is what visibility is derived from, so it is only
        ever written here, and only the mirror calls this (a structural
        test pins the call site).
        """
        validate_type(resource_type, resource_kind)
        if legacy_kind not in LEGACY_KINDS:
            raise CatalogueError(f"unknown legacy kind {legacy_kind!r}")
        if not legacy_ref:
            raise CatalogueError("a legacy link names the object it mirrors")
        row = CCResource(
            tenant_id=tenant_id,
            resource_kind=resource_kind,
            resource_type=resource_type,
            display_name=display_name[:255],
            lifecycle_state=LIFECYCLE_ACTIVE,
            source=SOURCE_LEGACY_MIRROR,
            legacy_kind=legacy_kind,
            legacy_ref=legacy_ref,
            legacy_site_id=legacy_site_id,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def refresh_mirrored(
        self,
        resource: CCResource,
        *,
        display_name: str,
        resource_type: str,
        legacy_site_id: Optional[str],
    ) -> bool:
        """Bring a mirrored resource's description up to date. True when
        anything changed -- an unchanged resource is not written at all."""
        validate_type(resource_type, resource.resource_kind)
        changed = False
        for name, value in (
            ("display_name", display_name[:255]),
            ("resource_type", resource_type),
            ("legacy_site_id", legacy_site_id),
        ):
            if getattr(resource, name) != value:
                setattr(resource, name, value)
                changed = True
        if changed:
            await self.session.flush()
        return changed

    async def retire(self, resource: CCResource) -> None:
        """A tombstone: the id, last placement and legacy link all stay."""
        resource.lifecycle_state = LIFECYCLE_RETIRED
        resource.retired_at = utcnow()
        await self.session.flush()

    async def revive(self, resource: CCResource) -> None:
        resource.lifecycle_state = LIFECYCLE_ACTIVE
        resource.retired_at = None
        await self.session.flush()

    async def _edge(
        self, from_id: str, to_id: str, relationship_type: str
    ) -> Optional[CCResourceRelationship]:
        rel = CCResourceRelationship
        return (
            await self.session.execute(
                select(rel).where(
                    rel.from_resource_id == from_id,
                    rel.to_resource_id == to_id,
                    rel.relationship_type == relationship_type,
                )
            )
        ).scalars().first()

    async def _ends(
        self, tenant_id: str, from_id: str, to_id: str
    ) -> tuple[CCResource, CCResource]:
        if from_id == to_id:
            raise CatalogueError("a resource cannot relate to itself")
        first = await self.get(tenant_id, from_id)
        second = await self.get(tenant_id, to_id)
        if first is None or second is None:
            # Also how a cross-tenant edge is refused: the other tenant's
            # resource is not a resource OF THIS tenant. The composite
            # (resource, tenant) foreign keys refuse it again at the
            # database (A32.3).
            raise CatalogueError("both ends must be resources of this tenant")
        for end in (first, second):
            if end.lifecycle_state != LIFECYCLE_ACTIVE:
                # A tombstone keeps the edges it had; nothing new is said
                # about a resource that is gone (A32.2).
                raise CatalogueError(
                    f"resource {end.id} is {end.lifecycle_state}: an edge is "
                    "asserted only between live resources"
                )
        return first, second

    async def _refuse_containment_cycle(
        self, tenant_id: str, parent_id: str, child_id: str
    ) -> None:
        """Refuse `parent CONTAINS child` if the child already contains the
        parent, through ANY active CONTAINS edge, primary or secondary
        (A32.3).

        Walks UP from the proposed parent, breadth first: a resource's
        containers are few, its contents may be thousands. A walk deeper
        than MAX_CONTAINMENT_DEPTH or wider than MAX_CONTAINMENT_WALK is
        refused -- fail closed -- because the answer to "is there a cycle"
        is then unknown, and unknown is not "no".
        """
        rel = CCResourceRelationship
        seen = {parent_id}
        frontier = [parent_id]
        depth = 0
        while frontier:
            if child_id in frontier:
                raise CatalogueError(
                    "refusing a containment cycle: the child already contains "
                    "the proposed parent"
                )
            depth += 1
            if depth > MAX_CONTAINMENT_DEPTH:
                raise CatalogueError("refusing a containment chain this deep")
            above = (
                await self.session.execute(
                    select(rel.from_resource_id).where(
                        rel.tenant_id == tenant_id,
                        rel.to_resource_id.in_(frontier),
                        rel.relationship_type == REL_CONTAINS,
                        rel.lifecycle_state == LIFECYCLE_ACTIVE,
                    )
                )
            ).scalars().all()
            frontier = sorted(set(above) - seen)
            seen.update(frontier)
            if len(seen) > MAX_CONTAINMENT_WALK:
                raise CatalogueError(
                    "refusing a containment walk this wide: a cycle cannot be "
                    "ruled out"
                )

    @staticmethod
    def _retire_edge(edge: CCResourceRelationship) -> None:
        edge.lifecycle_state = LIFECYCLE_RETIRED
        edge.retired_at = utcnow()

    @staticmethod
    def _revive_edge(edge: CCResourceRelationship, *, is_primary: bool) -> None:
        edge.lifecycle_state = LIFECYCLE_ACTIVE
        edge.retired_at = None
        edge.is_primary = is_primary

    async def set_primary_parent(
        self, tenant_id: str, child_id: str, parent_id: str, *, source: str
    ) -> CCResourceRelationship:
        """Make `parent_id` the ONE active primary containment parent of
        `child_id`.

        Physical containment only, no cycle, both ends live, and a parent
        set by another source is never replaced: a later writer that
        disagrees has a precedence question to answer first (open decision
        11), not a row to overwrite. A re-placement RETIRES the old primary
        edge and creates -- or revives -- the new one (A32.2), so the former
        placement survives as history; the old edge is retired and flushed
        before the new one is activated, and the partial unique index backs
        "one active primary parent" against any race.
        """
        parent, child = await self._ends(tenant_id, parent_id, child_id)
        validate_primary_containment(parent.resource_kind, child.resource_kind)
        existing = await self.primary_parent_edge(child.id)
        if existing is not None and existing.from_resource_id == parent.id:
            return existing
        if existing is not None and existing.source != source:
            raise CatalogueError(
                f"the primary parent was set by {existing.source!r}; "
                f"{source!r} may not replace it"
            )
        prior = await self._edge(parent.id, child.id, REL_CONTAINS)
        if prior is not None and prior.lifecycle_state == LIFECYCLE_ACTIVE:
            raise CatalogueError(
                "a secondary CONTAINS edge already joins these two resources"
            )
        if prior is not None and prior.source != source:
            raise CatalogueError(
                f"a former CONTAINS edge between these resources belongs to "
                f"{prior.source!r}; {source!r} may not take it over"
            )
        await self._refuse_containment_cycle(tenant_id, parent.id, child.id)
        if existing is not None:
            self._retire_edge(existing)
            await self.session.flush()
        if prior is not None:
            self._revive_edge(prior, is_primary=True)
            await self.session.flush()
            return prior
        edge = CCResourceRelationship(
            tenant_id=tenant_id,
            from_resource_id=parent.id,
            to_resource_id=child.id,
            relationship_type=REL_CONTAINS,
            is_primary=True,
            source=source,
            lifecycle_state=LIFECYCLE_ACTIVE,
        )
        self.session.add(edge)
        await self.session.flush()
        return edge

    async def add_relationship(
        self,
        tenant_id: str,
        from_id: str,
        to_id: str,
        relationship_type: str,
        *,
        source: str,
    ) -> CCResourceRelationship:
        """A SECONDARY typed edge. Idempotent; never primary; confers nothing.

        No production writer yet (the mirror writes primary containment
        only); this is the primitive the first topology or import slice will
        call, and the rules it enforces are tested now. A `CONTAINS` edge is
        containment whether or not it is primary, so it is held to the same
        physical-parent rule -- a Site Manager or an Operational Agent
        cannot contain anything by a secondary edge either -- and checked
        for a cycle; a former edge of the same source is revived rather
        than duplicated.
        """
        validate_relationship_type(relationship_type)
        container, contained = await self._ends(tenant_id, from_id, to_id)
        if relationship_type == REL_CONTAINS:
            validate_primary_containment(
                container.resource_kind, contained.resource_kind
            )
        existing = await self._edge(from_id, to_id, relationship_type)
        if existing is not None and existing.lifecycle_state == LIFECYCLE_ACTIVE:
            return existing
        if existing is not None and existing.source != source:
            raise CatalogueError(
                f"a former {relationship_type} edge between these resources "
                f"belongs to {existing.source!r}; {source!r} may not take it over"
            )
        if relationship_type == REL_CONTAINS:
            await self._refuse_containment_cycle(tenant_id, from_id, to_id)
        if existing is not None:
            self._revive_edge(existing, is_primary=False)
            await self.session.flush()
            return existing
        edge = CCResourceRelationship(
            tenant_id=tenant_id,
            from_resource_id=from_id,
            to_resource_id=to_id,
            relationship_type=relationship_type,
            is_primary=False,
            source=source,
            lifecycle_state=LIFECYCLE_ACTIVE,
        )
        self.session.add(edge)
        await self.session.flush()
        return edge

    async def _external(
        self, tenant_id: str, source_system: str, namespace: str, external_id: str
    ) -> Optional[CCResourceExternalId]:
        return (
            await self.session.execute(
                select(CCResourceExternalId).where(
                    CCResourceExternalId.tenant_id == tenant_id,
                    CCResourceExternalId.source_system == source_system,
                    CCResourceExternalId.namespace == namespace,
                    CCResourceExternalId.external_id == external_id,
                )
            )
        ).scalar_one_or_none()

    async def bind_external_id(
        self,
        tenant_id: str,
        resource_id: str,
        *,
        source_system: str,
        namespace: str,
        external_id: str,
        source: str,
    ) -> CCResourceExternalId:
        """Bind one identifier to one resource -- or refuse (A31.2).

        Idempotent for the resource it already names. An identifier that
        names a DIFFERENT resource raises `IdentityConflict` and the
        existing mapping is left exactly as it was: no silent merge, and no
        reassignment either.
        """
        if not (source_system and namespace and external_id):
            raise CatalogueError(
                "an external identifier names its system, namespace and value"
            )
        if await self.get(tenant_id, resource_id) is None:
            raise CatalogueError("no such resource in this tenant")

        def _conflict(owner: str) -> IdentityConflict:
            return IdentityConflict(
                f"{source_system}/{namespace}/{external_id} already names "
                f"resource {owner}; refusing to merge or reassign it"
            )

        existing = await self._external(
            tenant_id, source_system, namespace, external_id
        )
        if existing is not None:
            if existing.resource_id == resource_id:
                return existing
            raise _conflict(existing.resource_id)
        row = CCResourceExternalId(
            tenant_id=tenant_id,
            resource_id=resource_id,
            source_system=source_system,
            namespace=namespace,
            external_id=external_id,
            source=source,
        )
        try:
            async with self.session.begin_nested():
                self.session.add(row)
                await self.session.flush()
        except IntegrityError:
            # Another writer bound it first. The savepoint rolled back; the
            # outer transaction is untouched, and the same rule applies.
            existing = await self._external(
                tenant_id, source_system, namespace, external_id
            )
            if existing is not None and existing.resource_id == resource_id:
                return existing
            raise _conflict(existing.resource_id if existing else "unknown")
        return row


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
        stmt = _where(
            select(CCApprovalRoute)
            .join(CCSite, CCSite.id == CCApprovalRoute.site_id)
            .where(CCSite.tenant_id == tenant_id)
            .where(CCApprovalRoute.decision.is_(None)),
            scope_device_owned(
                CCApprovalRoute.site_id, CCApprovalRoute.device_agent_id, scope,
            ),
        )
        return (
            await self.session.execute(stmt.order_by(CCApprovalRoute.routed_at))
        ).scalars().all()

    async def list_pending_paginated(
        self, tenant_id: str, page: int = 1, page_size: int = 50, scope=None,
    ) -> tuple[Sequence[CCApprovalRoute], int]:
        base = _where(
            select(CCApprovalRoute)
            .join(CCSite, CCSite.id == CCApprovalRoute.site_id)
            .where(CCSite.tenant_id == tenant_id)
            .where(CCApprovalRoute.decision.is_(None)),
            scope_device_owned(
                CCApprovalRoute.site_id, CCApprovalRoute.device_agent_id, scope,
            ),
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
        stmt = _where(
            select(CCApprovalRoute)
            .join(CCSite, CCSite.id == CCApprovalRoute.site_id)
            .where(CCSite.tenant_id == tenant_id)
            .where(CCApprovalRoute.decision.isnot(None)),
            scope_device_owned(
                CCApprovalRoute.site_id, CCApprovalRoute.device_agent_id, scope,
            ),
        )
        return (
            await self.session.execute(
                stmt.order_by(CCApprovalRoute.decided_at.desc())
            )
        ).scalars().all()

    async def list_history_paginated(
        self, tenant_id: str, page: int = 1, page_size: int = 50, scope=None,
    ) -> tuple[Sequence[CCApprovalRoute], int]:
        base = _where(
            select(CCApprovalRoute)
            .join(CCSite, CCSite.id == CCApprovalRoute.site_id)
            .where(CCSite.tenant_id == tenant_id)
            .where(CCApprovalRoute.decision.isnot(None)),
            scope_device_owned(
                CCApprovalRoute.site_id, CCApprovalRoute.device_agent_id, scope,
            ),
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

    async def find(
        self, tenant_id: str, principal_type: str, principal_ref: str,
        scope_type: str, scope_ref: str = "",
    ) -> Optional[CCScopeGrant]:
        """The row for one grant identity, revoked or not.

        The identity is the unique constraint; `grant()` revives this
        row on re-grant, and A23-3's admission judges an overwrite by
        it -- the stored row leaves the administrator count and the
        posted shape is what remains.
        """
        return (
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
        existing = await self.find(
            tenant_id, principal_type, principal_ref, scope_type, scope_ref,
        )
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

    async def any_grant_exists(self, tenant_id: str) -> bool:
        """Has this tenant EVER carried a grant row, in any state?

        Unfiltered on purpose -- revoked and expired rows count. A23.10
        says a principal who was administered and then removed never
        regains authority through an absence, so "has never been
        administered" is the only precondition under which the platform
        may author a grant itself (A23.14 D4).
        """
        return (
            await self.session.execute(
                select(func.count())
                .select_from(CCScopeGrant)
                .where(CCScopeGrant.tenant_id == tenant_id)
            )
        ).scalar_one() > 0

    async def seed_first_grant(
        self,
        *,
        tenant_id: str,
        principal_ref: str,
        role: str,
        realm: str,
        granted_by: str,
        note: str = "",
    ) -> Optional[CCScopeGrant]:
        """The tenant's FIRST administrator, authored by provisioning (A23.14 D4).

        Deliberately NOT `grant()`. That method revives a matching
        revoked row, which is correct for a human re-granting somebody
        but catastrophic here: it would let a deliberately revoked
        administrator return through a provisioning path, outside the
        grant lifecycle and against A23.10.

        This writes only into a tenant that has NEVER held a grant row,
        and returns None otherwise. That precondition -- not the
        identity of the caller -- is what stops it becoming a general
        authorization path: there is no tenant state in which calling it
        twice produces a second grant, and no state in which it can
        overwrite, revive or widen anything.

        The row it writes is an ORDINARY grant. `count_tenant_admins`
        counts it, A23.8 refuses its removal as the last administrator,
        and its holder still cannot self-grant. It confers nothing a
        human-made first grant would not have conferred.

        Callers hold the tenant authorization lock, so the precondition
        they evaluated is still true at commit.
        """
        if await self.any_grant_exists(tenant_id):
            return None
        row = CCScopeGrant(
            tenant_id=tenant_id,
            principal_type="user",
            principal_ref=principal_ref,
            scope_type="tenant",
            scope_ref="",
            permission_subset=None,
            role=role,
            realm=realm,
            granted_by=granted_by,
            expires_at=None,
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

    async def grant_evidence(
        self, tenant_id: str, principal_type: str, refs: Iterable[str],
    ) -> Sequence[CCScopeGrant]:
        """Every row that has EVER named this principal in this tenant.

        A23-4 (spec A23.10). Revoked, expired, other-realm and
        alias-keyed rows included; NOT an authorization read. The
        resolver uses it for exactly one decision -- a principal with any
        such row is previously granted and is never handed the
        synthesized `legacy_open` tenant grant -- and for nothing else.

        `refs` is the stable principal reference plus any authenticated
        aliases of it (the token's own email claim; an email the platform
        itself paired with the subject on an authenticated request). An
        alias containing ``@`` is matched case-insensitively, because an
        address is a mutable display string; a subject is matched
        exactly. A row keyed by an alias authorizes nothing here and is
        evidence only.
        """
        exact = sorted({r for r in refs if r and "@" not in r})
        emails = sorted({r.strip().lower() for r in refs if r and "@" in r})
        if not exact and not emails:
            return []
        clauses = []
        if exact:
            clauses.append(CCScopeGrant.principal_ref.in_(exact))
        if emails:
            clauses.append(func.lower(CCScopeGrant.principal_ref).in_(emails))
        cond = clauses[0]
        for c in clauses[1:]:
            cond = cond | c
        return (
            await self.session.execute(
                select(CCScopeGrant)
                .where(
                    CCScopeGrant.tenant_id == tenant_id,
                    CCScopeGrant.principal_type == principal_type,
                    cond,
                )
                .order_by(CCScopeGrant.granted_at)
            )
        ).scalars().all()

    async def list_referencing(
        self, tenant_id: str, scope_type: str, scope_ref: str,
    ) -> Sequence[CCScopeGrant]:
        """Unrevoked grants -- user and agent -- naming one target (A23.9).

        What an org-unit deletion asks before it proceeds. Expiry is
        judged by the caller with `is_active`, so the answer is the
        same one the resolver would give.
        """
        return (
            await self.session.execute(
                select(CCScopeGrant)
                .where(
                    CCScopeGrant.tenant_id == tenant_id,
                    CCScopeGrant.scope_type == scope_type,
                    CCScopeGrant.scope_ref == scope_ref,
                    CCScopeGrant.revoked_at.is_(None),
                )
                .order_by(CCScopeGrant.principal_type, CCScopeGrant.principal_ref)
            )
        ).scalars().all()

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
        """The posture, defaulting to STRICT for an unseen tenant (A23.11).

        This answered `legacy_open` from E1.2 until A23-5. The reasoning
        was sound at the time -- CC cannot enumerate a realm's principals
        to backfill grants, so treating an absence as a decision would
        have locked out every working deployment on upgrade -- but it
        left the platform's most permissive posture reachable by a
        MISSING ROW, which is not a decision anybody made.

        Migration 0021 removes the reason: it pins the deployment's
        tenant explicitly to `legacy_open` before this default changes,
        so an existing tenant keeps exactly the posture it had and a
        tenant born afterwards is strict. A missing row can no longer
        synthesize the permissive posture.

        This is the ONE seam every caller goes through, so the flip
        happens once, here.
        """
        row = await self.get(tenant_id)
        return row.scope_enforcement if row else "strict"

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
        actor_ref: Optional[str] = None,
    ) -> CCAuditLog:
        """Append one entry to the tenant's hash chain.

        E1.2: `site_id` is authorization/indexing metadata and is
        DELIBERATELY absent from `_chain_payload`. The chain hashes ts,
        actor, action, subject, tenant_id and detail -- and only those --
        so recording a site neither changes an entry's hash nor
        invalidates any chain written before this column existed. A test
        asserts that rather than trusting it.

        A23-2: `actor_ref` is the STABLE identity, also outside the
        payload. A writer holding a `UserContext` passes
        ``actor_ref=actor_of(user)``; a writer holding only an
        attribution string leaves it unset and the one helper derives
        what it can (an agent id from an attribution key, a campaign
        ref, a bare subject). An email derives to NULL -- unresolvable
        is recorded as unresolvable, never guessed.
        """
        from harkeniq.audit.chain import next_link, pg_advisory_chain_lock
        from harkeniq_cc.actor import actor_of

        if actor_ref is None:
            actor_ref = actor_of(actor)

        row = CCAuditLog(
            ts=utcnow(),
            actor=actor,
            action=action,
            subject=subject,
            tenant_id=tenant_id,
            detail=detail,
            site_id=site_id,
            actor_ref=actor_ref or None,
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
            stmt = stmt.where(_actor_matches(actor))
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
            stmt = stmt.where(_actor_matches(actor))
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


def outcome_dict(r: CCOutcomeHistory) -> dict:
    """One outcome row as the dict settlement and its legacy join read."""
    return {
        "action_type": r.action_type,
        "vendor": r.vendor,
        "model": r.model,
        "outcome": r.outcome,
        "fault_resolved": bool(r.fault_resolved),
        "site_id": r.site_id,
        "ingested_at": r.ingested_at,
        "recorded_at": r.recorded_at,
        # A1: evidence that cannot name its actor cannot answer "what did
        # MY agent do", which is half of trusting one.
        "actor": r.actor or "",
        "device_agent_id": r.device_agent_id,
        # A25.1: the EXACT execution key. The Site Manager writes
        # `directive:<directive_id>` for every directed execution.
        "action_id": r.action_id or "",
    }


def _chunks(items: list, size: int):
    for start in range(0, len(items), size):
        yield items[start:start + size]


#: Keys or pairs per IN list: a bound on a statement's size, never on what
#: is read -- every chunk is read.
_IN_CHUNK = 500


class OutcomeHistoryRepo:
    """Read path for cc_outcome_history (R4-1: written by the fleet poller,
    read by the intelligence loop, the outcome-facing APIs and settlement).

    A30.41: every read is exact over the rows its caller may read. They all
    select through ONE statement builder, `_authorized`; no principal read
    carries a row limit, and every ordered read ends in the immutable `id`.
    PB-2 (A30.42 D-PB7) deleted the last window, `list_device_outcome_dicts`
    -- the internal decision paths' oldest-50,000 ranking input; they read
    `device_stats` over the agent's own reach now.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _authorized(tenant_id: str, scope, *columns):
        """SELECT `columns` over the outcome rows `scope` may read.

        The ONLY way an outcome read selects rows. Tenant scoping goes
        through cc_sites: an outcome belongs to the tenant that owns the
        site it was polled from. Then B0b's owner rule (A30.25): a row whose
        device resolves at its own site is that device's, otherwise the
        site's, so no device or class grant reads a site-owned row and the
        read is never widened to fit an identifier. `scope` is a principal's
        `ReadReach`, or `None` for an internal caller (the whole tenant); a
        bare `ResolvedScope` raises (S2).
        """
        return _where(
            select(*columns)
            .select_from(CCOutcomeHistory)
            .join(CCSite, CCOutcomeHistory.site_id == CCSite.id)
            .where(CCSite.tenant_id == tenant_id),
            scope_device_owned(
                CCOutcomeHistory.site_id, CCOutcomeHistory.device_agent_id, scope,
            ),
        )

    async def tally(self, tenant_id: str, *, scope=None) -> list[dict]:
        """Exact counts over EVERY row `scope` may read (A30.41).

        One row per (site, action class, vendor, model, outcome,
        fault_resolved), carrying `count`. The autonomy contract, a new
        proposal's frozen evidence, S3-E2's viewer, outcome metrics and the
        learning engine read this where they read the oldest 10,000 rows:
        their arithmetic weighs a row by `count`, so it is the arithmetic
        over the expanded rows. Groups are ordered by first appearance
        (`min(ingested_at)`), then the group key, so a consumer that inserts
        keys as it meets them -- the aggregator -- meets them in the order the
        old ingested_at scan did, and ties are total.
        """
        resolved = func.coalesce(CCOutcomeHistory.fault_resolved, False)
        key = (
            CCOutcomeHistory.site_id, CCOutcomeHistory.action_type,
            CCOutcomeHistory.vendor, CCOutcomeHistory.model,
            CCOutcomeHistory.outcome, resolved,
        )
        first_seen = func.min(CCOutcomeHistory.ingested_at)
        stmt = (
            self._authorized(tenant_id, scope, *key, func.count(), first_seen)
            .group_by(*key)
            .order_by(first_seen, *key)
        )
        return [
            {
                "site_id": site_id,
                "action_type": action_type,
                "vendor": vendor,
                "model": model,
                "outcome": outcome,
                "fault_resolved": bool(fault_resolved),
                "count": int(n),
            }
            for site_id, action_type, vendor, model, outcome, fault_resolved, n, _first
            in (await self.session.execute(stmt)).all()
        ]

    async def device_stats(
        self, tenant_id: str, *, scope, now: datetime, cohorts: bool = True,
    ):
        """A principal's predictive inputs, over EVERY row `scope` may read.

        Per device: the sample count and the two recency-weighted sums of
        `predictive.weighted_failure_rate`, summed by the database in the
        order Python sums them (`recorded_at`, then `id`). Per cohort: exact
        (vendor, model, outcome) counts for the SAME `cohort_failure_rates`
        -- skipped for a machine, which is never scored on a cohort. The
        rows these replace were the OLDEST 50,000, so above that the newest
        rows -- the heaviest -- were the ones dropped.
        """
        from harkeniq_cc.db.outcome_sql import decay_weight, ordered_sum
        from harkeniq_cc.predictive import (
            FAILURE_OUTCOMES,
            DeviceHistory,
            DeviceOutcomeStats,
        )

        weight = decay_weight(
            bindparam("decay_now", now, type_=DateTime(timezone=True)),
            CCOutcomeHistory.recorded_at,
        )
        failed = CCOutcomeHistory.outcome.in_(FAILURE_OUTCOMES)
        order = (CCOutcomeHistory.recorded_at, CCOutcomeHistory.id)
        per_device = (
            self._authorized(
                tenant_id, scope,
                CCOutcomeHistory.device_agent_id,
                func.count(),
                ordered_sum(weight, *order),
                ordered_sum(case((failed, weight), else_=literal(0.0, Float)), *order),
            )
            .group_by(CCOutcomeHistory.device_agent_id)
            .order_by(CCOutcomeHistory.device_agent_id)
        )
        devices = {
            device: DeviceHistory(
                count=int(n),
                weighted_total=float(total or 0.0),
                weighted_failed=float(failed_sum or 0.0),
            )
            for device, n, total, failed_sum in (await self.session.execute(per_device)).all()
        }
        cohort_tallies: tuple = ()
        if cohorts:
            cohort = (
                CCOutcomeHistory.vendor, CCOutcomeHistory.model, CCOutcomeHistory.outcome,
            )
            stmt = (
                self._authorized(tenant_id, scope, *cohort, func.count())
                .group_by(*cohort)
                .order_by(*cohort)
            )
            cohort_tallies = tuple(
                {"vendor": vendor, "model": model, "outcome": outcome, "count": int(n)}
                for vendor, model, outcome, n in (await self.session.execute(stmt)).all()
            )
        return DeviceOutcomeStats(
            devices=devices,
            cohort_tallies=cohort_tallies,
            total=sum(h.count for h in devices.values()),
        )

    async def first_by_action_ids(
        self, tenant_id: str, action_ids: Iterable[str],
    ) -> dict[str, CCOutcomeHistory]:
        """The outcome each execution key names: its OLDEST row (A30.41).

        Settlement's exact join (A25.1) and the machine receipt's -- one
        read, so a receipt names the outcome settlement used. Oldest by
        `(ingested_at, id)`: the choice settlement always made, made total.
        Every key is read, in bounded IN lists, with no window.
        """
        keys = sorted({k for k in action_ids if k})
        found: dict[str, CCOutcomeHistory] = {}
        for chunk in _chunks(keys, _IN_CHUNK):
            stmt = (
                self._authorized(tenant_id, None, CCOutcomeHistory)
                .where(CCOutcomeHistory.action_id.in_(chunk))
                .order_by(
                    CCOutcomeHistory.action_id,
                    CCOutcomeHistory.ingested_at,
                    CCOutcomeHistory.id,
                )
            )
            for row in (await self.session.execute(stmt)).scalars():
                found.setdefault(row.action_id, row)
        return found

    async def legacy_candidates(
        self, tenant_id: str, pairs: Iterable[tuple[str, str]], *,
        since: Optional[datetime] = None,
    ) -> dict[tuple[str, str], list[dict]]:
        """Candidate outcomes for KEYLESS proposals, by (device, action class).

        The pre-A25 heuristic's inputs, read only for the pairs a keyless
        proposal names and only from `since` (the earliest dispatch among
        them -- an outcome ingested before a dispatch cannot belong to it),
        in `(ingested_at, id)` order. The rules that pick among them are
        settlement's, unchanged.
        """
        wanted = sorted(set(pairs))
        out: dict[tuple[str, str], list[dict]] = {}
        for chunk in _chunks(wanted, _IN_CHUNK):
            stmt = (
                self._authorized(tenant_id, None, CCOutcomeHistory)
                .where(
                    tuple_(
                        CCOutcomeHistory.device_agent_id, CCOutcomeHistory.action_type,
                    ).in_(chunk)
                )
                .order_by(CCOutcomeHistory.ingested_at, CCOutcomeHistory.id)
            )
            if since is not None:
                stmt = stmt.where(CCOutcomeHistory.ingested_at >= since)
            for row in (await self.session.execute(stmt)).scalars():
                out.setdefault(
                    (row.device_agent_id, row.action_type), [],
                ).append(outcome_dict(row))
        return out

    async def count(self, tenant_id: str) -> int:
        result = await self.session.execute(
            select(func.count(CCOutcomeHistory.id))
            .join(CCSite, CCOutcomeHistory.site_id == CCSite.id)
            .where(CCSite.tenant_id == tenant_id)
        )
        return result.scalar() or 0

    async def find_by_action_id(
        self, tenant_id: str, action_id: str
    ) -> Optional[CCOutcomeHistory]:
        """The outcome an execution key names (A25.1).

        The machine receipt's read. It took the NEWEST row for a key while
        settlement took the OLDEST; it is now settlement's own read
        (A30.41), so the receipt and the proposal it describes cannot name
        two different outcomes.
        """
        if not action_id:
            return None
        return (await self.first_by_action_ids(tenant_id, [action_id])).get(action_id)


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
        limit: Optional[int] = 200,
        tenant_id: Optional[str] = None,
    ) -> Sequence[CCFleetPattern]:
        """`limit=None` reads with no window (A30.35): a window cut BEFORE
        S4 projects the rows would let a pattern the reader may not see
        take the place of one it may, so machine Attention asks for none."""
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
        """Ingest one FleetSnapshot candidate dict; idempotent on skill_id.

        A30.29: protected generated content and its generation provenance
        are one atomic security unit. Same-id writers are serialized on
        PostgreSQL before the row is read, so every update is one complete
        content/marker pair. A changed unmarked or malformed artifact clears
        the marker to UNKNOWN; an earlier marker never authorizes replacement
        content it did not describe. A byte-identical legacy replay may keep
        the marker already attached to those exact bytes.
        """
        import json as _json
        from datetime import timezone

        from harkeniq.audit.chain import pg_advisory_chain_lock
        from harkeniq.generation_provenance import parse as parse_visibility

        skill_id = cand.get("skill_id", "")
        # Transaction-scoped on PostgreSQL and acquired BEFORE the row read.
        # This also serializes the first insert, where SELECT FOR UPDATE has
        # no row to lock. SQLite remains single-writer in production/tests.
        await pg_advisory_chain_lock(
            self.session, f"cc.candidate_skill.{tenant_id}.{skill_id}"
        )

        row = await self.session.get(
            CCCandidateSkill, (skill_id, tenant_id)
        )
        if row is None:
            row = CCCandidateSkill(
                skill_id=skill_id, tenant_id=tenant_id
            )
            self.session.add(row)

        try:
            incoming_warnings = _json.loads(cand.get("warnings_json") or "[]") or None
        except (TypeError, ValueError):
            incoming_warnings = None
        incoming_yaml = cand.get("yaml_text", "")
        protected_changed = (
            (row.yaml_text or "") != incoming_yaml
            or list(row.warnings or []) != list(incoming_warnings or [])
        )

        row.site_id = site_id
        row.yaml_text = incoming_yaml
        row.source_device = cand.get("source_device", "")
        row.source_component = cand.get("source_component", "")
        row.validation_state = cand.get("validation_state", "draft")
        row.warnings = incoming_warnings
        row.dry_run_matches = cand.get("dry_run_matches", 0)
        if cand.get("generated_at_unix"):
            row.generated_at = datetime.fromtimestamp(
                cand["generated_at_unix"], tz=timezone.utc
            )
        # Store a canonical valid marker from THIS artifact. When protected
        # content changed, missing/malformed provenance becomes NULL even if
        # the old row had a valid marker. For an exact legacy replay only,
        # preserving the marker is safe because it still describes the same
        # protected bytes.
        visibility = parse_visibility(cand.get("generation_visibility"))
        if visibility is not None:
            row.generation_visibility = visibility.to_dict()
        elif protected_changed:
            row.generation_visibility = None
        await self.session.flush()
        return row

    async def list_candidates(
        self,
        tenant_id: str,
        status: Optional[str] = None,
        limit: int = 200,
        scope=None,
    ) -> Sequence[CCCandidateSkill]:
        # A23: a candidate carries the site it was generated at, so the
        # E1.2 read filter applies here like every other site-anchored
        # row. `scope=None` is the internal (learning loop) caller.
        stmt = apply_scope(
            select(CCCandidateSkill)
            .where(CCCandidateSkill.tenant_id == tenant_id),
            CCCandidateSkill.site_id,
            scope,
        ).order_by(CCCandidateSkill.received_at.desc()).limit(limit)
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
        scope_ref: Optional[str] = None, limit: Optional[int] = 500,
    ) -> Sequence[CCLearnedSignal]:
        """`limit=None` reads with no window (A30.35), for the reason
        `FleetPatternRepo.list_patterns` gives."""
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


#: Bounds on what a Site Manager's rack report may carry into Central
#: Command (TX-2, A32.4). Its ids are 32 hex characters; these only stop a
#: malformed or hostile report from writing an unbounded row.
_TOPOLOGY_MAX_RACKS = 10_000
_TOPOLOGY_MAX_MEMBERS = 10_000
_TOPOLOGY_MAX_REF = 255


def clean_topology_racks(racks) -> list[dict]:
    """A rack report reduced to what the catalogue can use, or nothing.

    Keeps a rack only when it names itself (a non-empty string id); keeps
    its name as a string, its members as non-empty strings, de-duplicated
    and sorted; sorts racks by (name, id). Everything else is dropped --
    never repaired, never guessed. Two entries naming one rack id are both
    KEPT: that is an ambiguity the mirror must see to hold, not one this
    function may resolve by choosing.
    """
    out: list[dict] = []
    if not isinstance(racks, list):
        return out
    for entry in racks[:_TOPOLOGY_MAX_RACKS]:
        if not isinstance(entry, dict):
            continue
        rack_id = entry.get("rack_id")
        if not isinstance(rack_id, str) or not rack_id or len(rack_id) > _TOPOLOGY_MAX_REF:
            continue
        name = entry.get("name")
        members = entry.get("device_agent_ids")
        out.append({
            "rack_id": rack_id,
            "name": name[:_TOPOLOGY_MAX_REF] if isinstance(name, str) else "",
            "device_agent_ids": sorted({
                m for m in (members if isinstance(members, list) else [])
                [:_TOPOLOGY_MAX_MEMBERS]
                if isinstance(m, str) and m and len(m) <= _TOPOLOGY_MAX_REF
            }),
        })
    return sorted(out, key=lambda rack: (rack["name"], rack["rack_id"]))


class SiteTopologyRepo:
    """A site's racks, as its Site Manager reports them (TX-2, A32.4).

    Replace-on-poll, like `SafetyStateRepo`, and for the same reason: a
    report is a CURRENT reading. A poll that carried no report writes
    `reported=False` -- UNKNOWN -- rather than leaving yesterday's racks to
    look like today's. Written by the fleet poller; read by the Resource
    Catalogue's mirror and by nothing else.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert(
        self, tenant_id: str, site_id: str, topology: dict,
    ) -> CCSiteTopology:
        reported = bool((topology or {}).get("reported"))
        racks = clean_topology_racks((topology or {}).get("racks")) if reported else []
        row = await self.session.get(CCSiteTopology, site_id)
        if row is None:
            row = CCSiteTopology(site_id=site_id)
            self.session.add(row)
        # Written only when the report moved, so an unchanged estate is not
        # rewritten every poll.
        for name, value in (
            ("tenant_id", tenant_id), ("reported", reported), ("racks", racks),
        ):
            if getattr(row, name) != value:
                setattr(row, name, value)
        await self.session.flush()
        return row

    async def list_for_tenant(self, tenant_id: str) -> Sequence[CCSiteTopology]:
        return (
            await self.session.execute(
                select(CCSiteTopology)
                .where(CCSiteTopology.tenant_id == tenant_id)
                .order_by(CCSiteTopology.site_id)
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
        device_agent_ids: Optional[Iterable[str]] = None,
    ) -> Sequence[CCIncident]:
        """`device_agent_ids` (A30.35, D3) narrows to incidents about those
        devices, in the WHERE and so before the limit: machine Attention
        asks only for the devices whose incidents it may show, so an
        incident about any other device can never take a place in the
        window. ``None`` is every earlier caller, unchanged."""
        stmt = _where(
            select(CCIncident).where(CCIncident.tenant_id == tenant_id),
            self._owned(scope),
        )
        if status:
            stmt = stmt.where(CCIncident.status == status)
        if site_id:
            stmt = stmt.where(CCIncident.site_id == site_id)
        if device_agent_id:
            stmt = stmt.where(CCIncident.device_agent_id == device_agent_id)
        if device_agent_ids is not None:
            wanted = sorted({d for d in device_agent_ids if d})
            stmt = stmt.where(
                CCIncident.device_agent_id.in_(wanted) if wanted else sa_false()
            )
        stmt = stmt.order_by(CCIncident.opened_at.desc().nullslast()).limit(limit)
        return (await self.session.execute(stmt)).scalars().all()

    async def get(self, tenant_id: str, incident_id: str) -> Optional[CCIncident]:
        row = await self.session.get(CCIncident, incident_id)
        if row is None or row.tenant_id != tenant_id:
            return None
        return row

    @staticmethod
    def _owned(scope):
        """A30.25: an incident that names a device is that device's; one
        that names none -- a correlated parent -- is its site's."""
        return scope_device_owned(
            CCIncident.site_id, CCIncident.device_agent_id, scope,
        )

    async def visible_ids(
        self, tenant_id: str, incident_ids: Iterable[str], scope=None,
    ) -> set[str]:
        """Which of these incidents the caller may INDEPENDENTLY read.

        R2/D7: correlation never widens reach. A child's parent, and a
        parent's children, are shown only when they would be shown on
        their own -- whatever their status or page -- and a parent that
        would not is never named.
        """
        wanted = sorted({i for i in incident_ids if i})
        if not wanted:
            return set()
        stmt = _where(
            select(CCIncident.incident_id)
            .where(CCIncident.tenant_id == tenant_id)
            .where(CCIncident.incident_id.in_(wanted)),
            self._owned(scope),
        )
        return set((await self.session.execute(stmt)).scalars().all())

    async def children_of(
        self, tenant_id: str, parent_id: str, scope=None,
    ) -> Sequence[CCIncident]:
        # R2: the SAME predicate as the list. A visible parent does not
        # carry a caller to children it could not read on their own.
        stmt = _where(
            select(CCIncident)
            .where(CCIncident.tenant_id == tenant_id)
            .where(CCIncident.parent_incident_id == parent_id),
            self._owned(scope),
        )
        return (
            await self.session.execute(stmt.order_by(CCIncident.opened_at))
        ).scalars().all()

    async def children_index(
        self, tenant_id: str, parent_ids: Iterable[str], scope=None,
    ) -> dict[str, list[CCIncident]]:
        """parent id -> the children the caller may read ON THEIR OWN.

        A30.34: the machine LIST names each incident's children, and must
        name exactly the ones its DETAIL names -- so this asks the SAME
        predicate as `children_of`, whatever the child's status or page,
        in one query for a whole page of parents.
        """
        wanted = sorted({i for i in parent_ids if i})
        if not wanted:
            return {}
        stmt = _where(
            select(CCIncident)
            .where(CCIncident.tenant_id == tenant_id)
            .where(CCIncident.parent_incident_id.in_(wanted)),
            self._owned(scope),
        )
        out: dict[str, list[CCIncident]] = {}
        for row in (await self.session.execute(stmt)).scalars().all():
            out.setdefault(row.parent_incident_id, []).append(row)
        return out


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
    async def list_administrative_scope_rows(
        self, agent_id: str
    ) -> Sequence[CCScopeGrant]:
        """The agent's CONFIGURED scope rows. **NOT an authorization source.**

        A30.4 (AD-1). This used to be `list_scopes()` and it was both
        things at once: the administrative row read AND the input every
        operational reach path resolved from. It filters `revoked_at` and
        it does NOT filter `expires_at`, so an EXPIRED grant of type
        `site`, `device` or `device_class` still yielded devices to
        `resolve_scope()` -- the evaluator, dry-run, runtime, agent-view
        and preflight all reached through a grant that had lapsed, while
        the canonical resolver said it was dead.

        The name is long on purpose. A method that returns rows a
        lifecycle filter has not been applied to cannot be called in a
        reach path by accident, and a reviewer reading a call site can
        see which of the two questions is being asked:

        * ADMINISTRATIVE GRANT ROW ACCESS -- what is configured, expired
          rows included. `clear_scopes()` is the case that must keep it:
          a row nobody can see is a row nobody can retire. Display of the
          configured scope and the A23-3 delegation ceiling also belong
          here -- the ceiling asks what a caller must have authority over
          to ADMINISTER the agent, and an expired row is still something
          the agent is configured with, so counting it can only refuse
          more.
        * EFFECTIVE AUTHORIZATION REACH -- what the agent may operate on
          now. That question has exactly one answer,
          `governance.load_agent_reach`, and it resolves through
          `scope.resolve()` like every other principal.

        `cc_agent_scopes` migrated into `cc_scope_grants` as
        `principal_type="agent"` (E1.2); a grant row carries the same
        `scope_type` / `scope_ref` the agent rows did.
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
        """Grant an agent a scope row, reviving a revoked identical one.

        A23-3: ONE lifecycle for the one table. Agent rows used to be
        inserted bare and deleted outright while human rows were revoked
        by timestamp and revived on re-grant; the same repository method
        now serves both, so an agent's scope history reads like a
        person's and a rebind cannot collide with its own past.

        An agent's authority is its A0 capability bindings, not a
        permission set; the grant carries WHERE, the bindings carry
        WHAT. NULL keeps the resolver from narrowing it away.
        """
        return await ScopeGrantRepo(self.session).grant(
            tenant_id=tenant_id,
            principal_type="agent",
            principal_ref=agent_id,
            scope_type=scope_type,
            scope_ref=scope_ref,
            permission_subset=None,
            granted_by=granted_by,
        )

    async def clear_scopes(self, agent_id: str, *, revoked_by: str = "") -> int:
        """Revoke every active scope row this agent holds. Returns the count.

        A timestamp, never a delete (A23-3): a retired agent's rows are
        history, and a deleted row is a row the impact census and an
        approval's scope snapshot can no longer explain.

        A30.4: this is the administrative read's reason to exist. An
        expired-but-unrevoked row must still be retired, and a
        lifecycle-filtered read cannot see it to revoke it.
        """
        rows = await self.list_administrative_scope_rows(agent_id)
        for row in rows:
            row.revoked_at = utcnow()
            row.revoked_by = revoked_by
        await self.session.flush()
        return len(rows)

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

    #: PB-0 (A30.43): how long a dispatch claim excludes every other
    #: dispatcher. Longer than proposal dispatch's RPC deadline
    #: (`agent_runtime.DISPATCH_RPC_TIMEOUT_S`), so a live claimer always
    #: finishes or gives up inside it; shorter than the 120 s evaluation
    #: pass, so a crashed or lost-ack claim is re-sent on the next pass.
    DISPATCH_CLAIM_LEASE = timedelta(seconds=60)

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

    async def get_for_decision(
        self, tenant_id: str, proposal_id: str
    ) -> Optional[CCAgentProposal]:
        """The proposal, row-locked for a human's decision (PB-0, A30.43).

        Two approvers finishing at once used to read `awaiting_approval`
        without a lock, both decide, and both call the Site Manager. The
        lock serializes them: the second waits for the first to commit,
        then reads the status the first committed -- exactly what a
        sequential second decision always read. It is released at the
        decision's commit, which precedes the Site Manager call, so it
        never spans the network.

        `populate_existing`: the approvals route has usually loaded this
        row already in the same session, and without it the identity map
        would hand back the UNLOCKED read's stale status.
        """
        return (
            await self.session.execute(
                select(CCAgentProposal)
                .where(
                    CCAgentProposal.id == proposal_id,
                    CCAgentProposal.tenant_id == tenant_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

    async def claim_dispatch(
        self, proposal: CCAgentProposal, *, now: Optional[datetime] = None
    ) -> bool:
        """Take the dispatch claim on an approved proposal. True = owner.

        PB-0 (A30.42 D-PB5, A30.43): ONE conditional UPDATE, so exactly one
        caller -- one replica's pass, one approver's request -- wins, and
        the winner commits the claim BEFORE it calls the Site Manager. The
        condition is the proposal as the caller's gates judged it: still
        `approved`, still on the basis the gates were asked of, and either
        never claimed or claimed longer ago than the lease. A lapsed claim
        (a crash, a lost acknowledgement, a timed-out call) is taken over
        and re-sent under the same Site Manager idempotency key, which
        answers with the directive it already holds.

        The caller commits; this only flushes pending changes first so the
        UPDATE sees this transaction's own decision. The ORM object is
        brought in line without marking it dirty.
        """
        from sqlalchemy.orm.attributes import set_committed_value

        now = now or utcnow()
        await self.session.flush()
        result = await self.session.execute(
            sa_update(CCAgentProposal)
            .where(
                CCAgentProposal.id == proposal.id,
                CCAgentProposal.tenant_id == proposal.tenant_id,
                CCAgentProposal.status == "approved",
                CCAgentProposal.authorization_basis
                == (proposal.authorization_basis or ""),
                or_(
                    CCAgentProposal.dispatch_claimed_at.is_(None),
                    CCAgentProposal.dispatch_claimed_at
                    < now - self.DISPATCH_CLAIM_LEASE,
                ),
            )
            .values(dispatch_claimed_at=now)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            return False
        set_committed_value(proposal, "dispatch_claimed_at", now)
        return True

    async def release_claim(self, proposal: CCAgentProposal) -> bool:
        """Give back THIS caller's claim after a failed call. True = released.

        A transport failure means the call has ended, so nobody needs to be
        excluded any longer: the next pass may re-send at once, and the Site
        Manager's key answers with the directive it already holds if the
        lost call had in fact been accepted. Conditional on the claim still
        being exactly the one this caller took, so a claim another
        dispatcher has since taken over is never cleared. The lease is for
        the claimer that never comes back -- a crash -- not for this.
        """
        from sqlalchemy.orm.attributes import set_committed_value

        mine = proposal.dispatch_claimed_at
        if mine is None:
            return False
        result = await self.session.execute(
            sa_update(CCAgentProposal)
            .where(
                CCAgentProposal.id == proposal.id,
                CCAgentProposal.tenant_id == proposal.tenant_id,
                CCAgentProposal.status == "approved",
                CCAgentProposal.dispatch_claimed_at == mine,
            )
            .values(dispatch_claimed_at=None)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            return False
        set_committed_value(proposal, "dispatch_claimed_at", None)
        return True

    async def note_dispatch_withheld(
        self, proposal: CCAgentProposal, reason: str
    ) -> bool:
        """Record why an approved proposal was not dispatched. True = new cause.

        Reason only -- no claim is taken for work that may not run, and the
        decision is left exactly as it stands (A22.12). Conditional so a
        dispatcher holding a stale copy can never write a refusal onto a
        proposal another dispatcher has since moved on (PB-0), and so the
        caller audits each distinct cause once however many replicas see it.
        """
        from sqlalchemy.orm.attributes import set_committed_value

        reason = reason[:512]
        result = await self.session.execute(
            sa_update(CCAgentProposal)
            .where(
                CCAgentProposal.id == proposal.id,
                CCAgentProposal.tenant_id == proposal.tenant_id,
                CCAgentProposal.status == "approved",
                CCAgentProposal.dispatch_reason != reason,
            )
            .values(dispatch_reason=reason)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            return False
        set_committed_value(proposal, "dispatch_reason", reason)
        return True

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

    #: A24.12: statuses at which work may still REACH a device, and so
    #: the only ones that may fence an equivalent physical operation.
    #: Deliberately NOT `OPEN_STATUSES`, which answers a different
    #: question and rightly includes `denied`.
    #:
    #: Walked against the documented lifecycle rather than assumed:
    #:
    #:   proposed          in flight -- governance has not finished
    #:   awaiting_approval in flight -- a human may still approve it
    #:   approved          in flight -- dispatch is pending
    #:   dispatched        in flight -- the node may be running it now
    #:   denied            NOT: final (D16). It will never reach a device,
    #:                     and fencing on it would let one refusal block
    #:                     that operation for every agent forever.
    #:   blocked           NOT: "never dispatch and exist to be read".
    #:   completed         NOT: settled. Re-running an action that already
    #:                     succeeded is legitimate work.
    #:   failed            NOT: settled, and re-trying is the normal
    #:                     response to a failure.
    #:
    #: `denied` still blocks the PER-AGENT dedupe rule, which is a
    #: different guarantee with a different reason: that agent must not
    #: relitigate a refusal. Another agent proposing the same operation
    #: later is not relitigation.
    IN_FLIGHT_STATUSES = (
        "proposed",
        "awaiting_approval",
        "approved",
        "dispatched",
    )

    async def find_open_operation(
        self, tenant_id: str, operation_key: str
    ) -> Optional[CCAgentProposal]:
        """An IN-FLIGHT proposal for this physical operation, from ANY agent.

        A24.12. NULL `operation_key` rows are skipped rather than matched:
        a proposal made before the concept existed is not comparable, and
        treating "unknown" as "equal" would fence devices on evidence
        nobody ever recorded.
        """
        if not operation_key:
            return None
        return (
            await self.session.execute(
                select(CCAgentProposal)
                .where(
                    CCAgentProposal.tenant_id == tenant_id,
                    CCAgentProposal.operation_key == operation_key,
                    CCAgentProposal.status.in_(self.IN_FLIGHT_STATUSES),
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
        # A30.25: a proposal targets one device, so the queue follows the
        # owner rule like a node action's route does.
        stmt = _where(
            select(CCAgentProposal).where(
                CCAgentProposal.tenant_id == tenant_id,
                CCAgentProposal.status == "awaiting_approval",
            ),
            scope_device_owned(
                CCAgentProposal.site_id, CCAgentProposal.device_agent_id, scope,
            ),
        )
        return (
            await self.session.execute(stmt.order_by(CCAgentProposal.created_at))
        ).scalars().all()

    async def page_by_status(
        self,
        tenant_id: str,
        statuses: Sequence[str],
        *,
        after: Optional[tuple[datetime, str]] = None,
        limit: int = 500,
    ) -> Sequence[CCAgentProposal]:
        """One keyset page of proposals in `statuses`, by `(created_at, id)`.

        A30.41: settlement walks EVERY dispatched proposal with this, page
        after page; PB-0 (A30.42 D-PB6) moved dispatch onto it too. The
        oldest-`limit` read both used to take handed back 500 rows and no
        more, so 500 proposals that never moved -- never heard back, or
        were withheld -- starved every newer one. `after` is the last
        page's final `(created_at, id)`; a row that leaves `statuses`
        meanwhile simply is not met again.
        """
        stmt = (
            select(CCAgentProposal)
            .where(
                CCAgentProposal.tenant_id == tenant_id,
                CCAgentProposal.status.in_(list(statuses)),
            )
            .order_by(CCAgentProposal.created_at, CCAgentProposal.id)
            .limit(limit)
        )
        if after is not None:
            created_at, proposal_id = after
            stmt = stmt.where(or_(
                CCAgentProposal.created_at > created_at,
                and_(
                    CCAgentProposal.created_at == created_at,
                    CCAgentProposal.id > proposal_id,
                ),
            ))
        return (await self.session.execute(stmt)).scalars().all()

    async def count_admitted_since(
        self, tenant_id: str, agent_id: str, since: datetime
    ) -> int:
        """Proposals CHARGED to this agent's admission budget since `since`.

        PB-1 (A30.42 D-PB2, A30.44): every admitted row that is not
        `blocked`, across the agent's configuration versions. `blocked` is
        written only at creation and no transition reaches it later, so a
        row's charge is fixed at insert and nothing releases it (D-PB14) --
        approval, denial, dispatch, failure and revocation all leave the
        count where it was. Read by `admit_proposal` UNDER the tenant
        admission lock; every other reader reports the same number.
        """
        result = await self.session.execute(
            select(func.count(CCAgentProposal.id)).where(
                CCAgentProposal.tenant_id == tenant_id,
                CCAgentProposal.agent_id == agent_id,
                CCAgentProposal.created_at >= since,
                CCAgentProposal.status != "blocked",
            )
        )
        return int(result.scalar() or 0)

    async def count_unattended(
        self, tenant_id: str, agent_id: str, since: datetime
    ) -> int:
        """Unattended dispatches charged to this agent's execution budget.

        PB-1 (A30.42 D-PB4, A30.44): ONLY `autonomous_grant` work counts --
        a human's decision never spends delegated capacity. Derived from
        the proposal rows, per proposal, so it cannot drift from them:

          committed  the Site Manager accepted it: `mark_dispatched` set
                     `dispatched_at`, inside the window. Kept whatever the
                     node later did with it -- a node refusal is a FAILURE
                     outcome, never a refund.
          reserved   still `approved`, a dispatch claim held, no
                     directive yet -- a send in progress, or one a crashed
                     claimer left for the lease. Counted from the claim's
                     own time, so a stale claim leaves the window with it.

        Released is everything else: a Site Manager refusal before queueing
        marks the proposal failed with no `dispatched_at`; a transport
        error releases the claim. Read UNDER the per-agent execution lock
        by the background dispatch pass.
        """
        result = await self.session.execute(
            select(func.count(CCAgentProposal.id)).where(
                CCAgentProposal.tenant_id == tenant_id,
                CCAgentProposal.agent_id == agent_id,
                CCAgentProposal.authorization_basis == "autonomous_grant",
                or_(
                    CCAgentProposal.dispatched_at >= since,
                    and_(
                        CCAgentProposal.status == "approved",
                        CCAgentProposal.dispatch_claimed_at.is_not(None),
                        CCAgentProposal.dispatch_claimed_at >= since,
                    ),
                ),
            )
        )
        return int(result.scalar() or 0)

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
        # PB-0: the dispatcher claimed the proposal to withhold it. Back in
        # the human queue it is a fresh decision, and the approval that
        # follows must not wait out a lease nobody is using.
        proposal.dispatch_claimed_at = None

    async def settle(self, proposal: CCAgentProposal, outcome: str) -> None:
        proposal.status = "completed" if outcome == "SUCCESS" else "failed"
        proposal.outcome = outcome
        proposal.outcome_at = utcnow()


class AgentSubmissionRepo:
    """A24: the external ingress ledger -- replay safety and rate truth.

    Two jobs, and they are the same rows because a refused submission
    must cost the same as an accepted one. A refusal that left no row
    would be free to repeat, which turns the cheapest possible request
    into the cheapest possible flood.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def find(
        self, tenant_id: str, agent_id: str, idempotency_key: str
    ) -> Optional[CCAgentSubmission]:
        """The prior submission under this key, if there is one."""
        return (
            await self.session.execute(
                select(CCAgentSubmission).where(
                    CCAgentSubmission.tenant_id == tenant_id,
                    CCAgentSubmission.agent_id == agent_id,
                    CCAgentSubmission.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()

    async def get(
        self, tenant_id: str, submission_id: str
    ) -> Optional[CCAgentSubmission]:
        return (
            await self.session.execute(
                select(CCAgentSubmission).where(
                    CCAgentSubmission.id == submission_id,
                    CCAgentSubmission.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()

    async def recent_refusals(
        self, tenant_id: str, agent_id: str, *, limit: int = 20
    ) -> tuple[list[CCAgentSubmission], Optional[datetime]]:
        """The newest refusals, and when something last landed.

        A27.9: `cc_agent_submissions` is the DURABLE idempotency ledger
        and is deliberately never pruned, so it is the one structure that
        must never be scanned whole. Deterministic ordering, fixed limit,
        and "last accepted" is one indexed MAX rather than a walk looking
        for it.

        A refusal is a submission that produced no proposal -- which is
        exactly what `proposal_id IS NULL` means on this table.
        """
        refusals = list(
            (
                await self.session.execute(
                    select(CCAgentSubmission)
                    .where(
                        CCAgentSubmission.tenant_id == tenant_id,
                        CCAgentSubmission.agent_id == agent_id,
                        CCAgentSubmission.proposal_id.is_(None),
                    )
                    .order_by(CCAgentSubmission.created_at.desc())
                    .limit(limit)
                )
            ).scalars().all()
        )
        last_accepted = (
            await self.session.execute(
                select(func.max(CCAgentSubmission.created_at)).where(
                    CCAgentSubmission.tenant_id == tenant_id,
                    CCAgentSubmission.agent_id == agent_id,
                    CCAgentSubmission.proposal_id.is_not(None),
                )
            )
        ).scalar()
        return refusals, last_accepted

    async def for_proposals(
        self, tenant_id: str, proposal_ids: list[str]
    ) -> list[CCAgentSubmission]:
        """The submissions behind these proposals, in ONE query.

        A27.6 wants the submission id beside an externally-originated
        proposal on a queue. Fetching it per row would make a fifty-item
        queue fifty extra queries; this is one `IN` over the page.
        """
        if not proposal_ids:
            return []
        return list(
            (
                await self.session.execute(
                    select(CCAgentSubmission).where(
                        CCAgentSubmission.tenant_id == tenant_id,
                        CCAgentSubmission.proposal_id.in_(proposal_ids),
                    )
                )
            ).scalars().all()
        )

    async def record(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        agent_version: int,
        idempotency_key: str,
        request_digest: str,
        candidate_ref: str,
        proposal_id: Optional[str],
        code: str = "",
        reason: str = "",
    ) -> CCAgentSubmission:
        row = CCAgentSubmission(
            tenant_id=tenant_id,
            agent_id=agent_id,
            agent_version=agent_version,
            idempotency_key=idempotency_key,
            request_digest=request_digest,
            candidate_ref=candidate_ref,
            proposal_id=proposal_id,
            code=code,
            reason=reason,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def count_since(
        self, tenant_id: str, agent_id: str, since: datetime
    ) -> int:
        """Submissions by this agent since `since` -- the rate window.

        A COUNT over committed rows, deliberately. Central Command runs
        multi-replica (R5-2 added advisory locking for exactly that
        reason), so an in-process counter would be decorative: two
        replicas would each permit the whole allowance and the limit
        would be a comment. This is correct across replicas and needs no
        Redis, no Kafka and no new infrastructure.
        """
        return int(
            (
                await self.session.execute(
                    select(func.count())
                    .select_from(CCAgentSubmission)
                    .where(
                        CCAgentSubmission.tenant_id == tenant_id,
                        CCAgentSubmission.agent_id == agent_id,
                        CCAgentSubmission.created_at >= since,
                    )
                )
            ).scalar()
            or 0
        )


class AgentIngressAttemptRepo:
    """A24.13: the append-only record a rate control can actually meter."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record(
        self, *, tenant_id: str, agent_id: str, outcome: str
    ) -> CCAgentIngressAttempt:
        row = CCAgentIngressAttempt(
            tenant_id=tenant_id, agent_id=agent_id, outcome=outcome,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def count_since(
        self, tenant_id: str, agent_id: str, since: datetime
    ) -> int:
        return int(
            (
                await self.session.execute(
                    select(func.count())
                    .select_from(CCAgentIngressAttempt)
                    .where(
                        CCAgentIngressAttempt.tenant_id == tenant_id,
                        CCAgentIngressAttempt.agent_id == agent_id,
                        CCAgentIngressAttempt.created_at >= since,
                    )
                )
            ).scalar()
            or 0
        )

    async def counts_since(
        self, tenant_id: str, agent_id: str, since: datetime
    ) -> dict[str, int]:
        """Attempt outcomes in the window, GROUPED BY the database.

        A27.9: an aggregate over the window that already prunes itself --
        never a row-by-row read and never a scan outside the window. The
        grouping is done in SQL so the payload is bounded by the number
        of OUTCOMES, not by traffic.
        """
        rows = (
            await self.session.execute(
                select(CCAgentIngressAttempt.outcome, func.count().label("n"))
                .where(
                    CCAgentIngressAttempt.tenant_id == tenant_id,
                    CCAgentIngressAttempt.agent_id == agent_id,
                    CCAgentIngressAttempt.created_at >= since,
                )
                .group_by(CCAgentIngressAttempt.outcome)
            )
        ).all()
        return {(outcome or "unknown"): int(n) for outcome, n in rows}

    async def last_attempt_at(
        self, tenant_id: str, agent_id: str
    ) -> Optional[datetime]:
        """When this agent last tried anything. One indexed MAX."""
        return (
            await self.session.execute(
                select(func.max(CCAgentIngressAttempt.created_at)).where(
                    CCAgentIngressAttempt.tenant_id == tenant_id,
                    CCAgentIngressAttempt.agent_id == agent_id,
                )
            )
        ).scalar()

    async def prune(
        self, tenant_id: str, agent_id: str, before: datetime
    ) -> None:
        """Drop rows outside the window. They have no other reader."""
        await self.session.execute(
            sa_delete(CCAgentIngressAttempt).where(
                CCAgentIngressAttempt.tenant_id == tenant_id,
                CCAgentIngressAttempt.agent_id == agent_id,
                CCAgentIngressAttempt.created_at < before,
            )
        )


class AgentReadWindowRepo:
    """A25.6: the read counter. One row per agent per window."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def increment(
        self, *, tenant_id: str, agent_id: str, window_start: datetime
    ) -> int:
        """Add one read and return the running total for this window.

        Atomic by construction rather than by lock: the UPDATE is a single
        statement, and the INSERT that opens a window is guarded by the
        unique constraint, so two replicas racing to open the same window
        cannot both succeed -- the loser re-reads and updates instead.
        """
        updated = await self.session.execute(
            sa_update(CCAgentReadWindow)
            .where(
                CCAgentReadWindow.tenant_id == tenant_id,
                CCAgentReadWindow.agent_id == agent_id,
                CCAgentReadWindow.window_start == window_start,
            )
            .values(reads=CCAgentReadWindow.reads + 1)
        )
        if updated.rowcount:
            return await self._current(tenant_id, agent_id, window_start)
        # Opening the window. Inside a SAVEPOINT, because losing the race
        # here must cost this statement and nothing else: the first
        # implementation called `session.rollback()`, which discards the
        # CALLER's whole transaction -- a helper cannot quietly undo work
        # it never knew about, whatever else it gets right.
        try:
            async with self.session.begin_nested():
                self.session.add(CCAgentReadWindow(
                    tenant_id=tenant_id, agent_id=agent_id,
                    window_start=window_start, reads=1,
                ))
                await self.session.flush()
            return 1
        except IntegrityError:
            # Another replica opened it first; the savepoint rolled back,
            # the outer transaction is untouched, and the ordinary path
            # now applies.
            await self.session.execute(
                sa_update(CCAgentReadWindow)
                .where(
                    CCAgentReadWindow.tenant_id == tenant_id,
                    CCAgentReadWindow.agent_id == agent_id,
                    CCAgentReadWindow.window_start == window_start,
                )
                .values(reads=CCAgentReadWindow.reads + 1)
            )
            return await self._current(tenant_id, agent_id, window_start)

    async def _current(
        self, tenant_id: str, agent_id: str, window_start: datetime
    ) -> int:
        return int(
            (
                await self.session.execute(
                    select(CCAgentReadWindow.reads).where(
                        CCAgentReadWindow.tenant_id == tenant_id,
                        CCAgentReadWindow.agent_id == agent_id,
                        CCAgentReadWindow.window_start == window_start,
                    )
                )
            ).scalar()
            or 0
        )

    #: A29.16: the CLOSED reason vocabulary, mapped to its column. A
    #: reason outside this table is counted in the total and in nothing
    #: else, so an unrecognised value can never mint a column or widen
    #: the schema.
    REFUSAL_COLUMNS = {
        "surface_not_allowed": "refused_surface_not_allowed",
        "machine_job_not_bound": "refused_job_not_bound",
    }

    async def record_refusal(
        self, *, tenant_id: str, agent_id: str, window_start: datetime,
        reason: str, at: datetime,
    ) -> int:
        """Add bounded refusal evidence to THIS window. Returns the total.

        Atomic by the same single-statement UPDATE `increment` stands on,
        and it deliberately does NOT create the row: the refusal was
        already charged through `admit_read`, which opened it. If the row
        is somehow absent the evidence is dropped rather than a second
        row minted -- accounting must never be the thing that grows
        storage under a flood.

        Identity is the CALLER's, derived server-side by the caller from
        the validated token; nothing here reads a path, a query or a body.
        """
        values = {
            "surface_refused": CCAgentReadWindow.surface_refused + 1,
            "last_surface_refused_at": at,
        }
        column = self.REFUSAL_COLUMNS.get(reason)
        if column:
            values[column] = getattr(CCAgentReadWindow, column) + 1
        await self.session.execute(
            sa_update(CCAgentReadWindow)
            .where(
                CCAgentReadWindow.tenant_id == tenant_id,
                CCAgentReadWindow.agent_id == agent_id,
                CCAgentReadWindow.window_start == window_start,
            )
            .values(**values)
        )
        return int((
            await self.session.execute(
                select(CCAgentReadWindow.surface_refused).where(
                    CCAgentReadWindow.tenant_id == tenant_id,
                    CCAgentReadWindow.agent_id == agent_id,
                    CCAgentReadWindow.window_start == window_start,
                )
            )
        ).scalar() or 0)

    async def refusals_since(
        self, tenant_id: str, agent_id: str, since: datetime
    ) -> tuple[dict[str, int], Optional[datetime]]:
        """Refusal evidence in the window, and the most recent one.

        Two aggregates the database computes over rows bounded by the
        window alignment -- never a row-per-request read (A27.9). A READ:
        an operator inspecting a runtime's refusals must not spend that
        runtime's allowance, exactly as `usage` established.
        """
        row = (
            await self.session.execute(
                select(
                    func.coalesce(
                        func.sum(CCAgentReadWindow.surface_refused), 0),
                    func.coalesce(
                        func.sum(
                            CCAgentReadWindow.refused_surface_not_allowed), 0),
                    func.coalesce(
                        func.sum(CCAgentReadWindow.refused_job_not_bound), 0),
                    func.max(CCAgentReadWindow.last_surface_refused_at),
                ).where(
                    CCAgentReadWindow.tenant_id == tenant_id,
                    CCAgentReadWindow.agent_id == agent_id,
                    CCAgentReadWindow.window_start >= since,
                )
            )
        ).one()
        return {
            "total": int(row[0] or 0),
            "surface_not_allowed": int(row[1] or 0),
            "machine_job_not_bound": int(row[2] or 0),
        }, row[3]

    async def usage(
        self, *, tenant_id: str, agent_id: str, window_start: datetime
    ) -> int:
        """How much of the CURRENT window this agent has spent.

        A read, not an increment: an operator inspecting a runtime's
        throttle state must not consume that runtime's allowance. The
        increment stays in `admit_read`, which remains the only writer.
        """
        return await self._current(tenant_id, agent_id, window_start)

    async def prune(self, before: datetime) -> None:
        """Windows outside the retention horizon have no reader."""
        await self.session.execute(
            sa_delete(CCAgentReadWindow).where(
                CCAgentReadWindow.window_start < before
            )
        )


class AgentThrottleWindowRepo:
    """A27.13: the rate-rejection counter. One row per agent per window.

    Shaped exactly like `AgentReadWindowRepo` because it solves the same
    problem: a signal that must survive concurrent writers from several
    replicas without a row per request. The difference is what it counts
    -- requests that were REFUSED, which is the one thing the attempt
    ledger deliberately cannot hold.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record(
        self, *, tenant_id: str, agent_id: str, window_start: datetime, at: datetime
    ) -> int:
        """Mark one ACTUAL rejection and return this window's running total.

        Atomic by construction rather than by lock: the UPDATE is a
        single statement, and the INSERT that opens a window is guarded
        by the unique constraint, so two replicas racing to open the
        same window cannot both succeed. The ingress advisory lock is
        also held by the production caller, which makes this belt and
        braces rather than the only defence -- deliberately, because a
        counter whose correctness depends on a lock somebody else takes
        is a counter that will one day be called from somewhere else.
        """
        updated = await self.session.execute(
            sa_update(CCAgentThrottleWindow)
            .where(
                CCAgentThrottleWindow.tenant_id == tenant_id,
                CCAgentThrottleWindow.agent_id == agent_id,
                CCAgentThrottleWindow.window_start == window_start,
            )
            .values(
                rejected=CCAgentThrottleWindow.rejected + 1, last_at=at,
            )
        )
        if updated.rowcount:
            return await self._current(tenant_id, agent_id, window_start)
        # Opening the window. Inside a SAVEPOINT: losing this race must
        # cost this statement and nothing else -- a helper must never
        # roll back a caller's transaction it knows nothing about
        # (the defect A25.12 found in the read counter).
        try:
            async with self.session.begin_nested():
                self.session.add(CCAgentThrottleWindow(
                    tenant_id=tenant_id, agent_id=agent_id,
                    window_start=window_start, rejected=1, last_at=at,
                ))
                await self.session.flush()
            return 1
        except IntegrityError:
            await self.session.execute(
                sa_update(CCAgentThrottleWindow)
                .where(
                    CCAgentThrottleWindow.tenant_id == tenant_id,
                    CCAgentThrottleWindow.agent_id == agent_id,
                    CCAgentThrottleWindow.window_start == window_start,
                )
                .values(
                    rejected=CCAgentThrottleWindow.rejected + 1, last_at=at,
                )
            )
            return await self._current(tenant_id, agent_id, window_start)

    async def _current(
        self, tenant_id: str, agent_id: str, window_start: datetime
    ) -> int:
        return int(
            (
                await self.session.execute(
                    select(CCAgentThrottleWindow.rejected).where(
                        CCAgentThrottleWindow.tenant_id == tenant_id,
                        CCAgentThrottleWindow.agent_id == agent_id,
                        CCAgentThrottleWindow.window_start == window_start,
                    )
                )
            ).scalar()
            or 0
        )

    async def observed(
        self, tenant_id: str, agent_id: str, since: datetime
    ) -> tuple[int, Optional[datetime]]:
        """How many rejections in the window, and the most recent one.

        Two aggregates the database computes, over rows bounded by the
        alignment -- never a row-per-request read (A27.9).
        """
        row = (
            await self.session.execute(
                select(
                    func.coalesce(func.sum(CCAgentThrottleWindow.rejected), 0),
                    func.max(CCAgentThrottleWindow.last_at),
                ).where(
                    CCAgentThrottleWindow.tenant_id == tenant_id,
                    CCAgentThrottleWindow.agent_id == agent_id,
                    CCAgentThrottleWindow.window_start >= since,
                )
            )
        ).one()
        return int(row[0] or 0), row[1]

    async def prune(self, before: datetime) -> None:
        """Windows outside the retention horizon have no reader."""
        await self.session.execute(
            sa_delete(CCAgentThrottleWindow).where(
                CCAgentThrottleWindow.window_start < before
            )
        )


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
        scope = require_read_reach(scope)
        if scope is None or scope.tenant_wide:
            return rows
        # A campaign is visible when the caller can see at least one site
        # it reaches. Filtering on the campaign's own sites rather than a
        # column keeps this consistent with how every other site-anchored
        # read is scoped.
        visible = set(scope.site_ids)
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
        # A24 addendum (the sixth finding): read -> if-empty -> seed is a
        # race. Two concurrent requests on a tenant whose catalogue is
        # unseeded both see nothing, both seed, and one raises a unique
        # violation -- a 500 reproduced by two concurrent A6 submissions.
        # Pre-existing, but A6 is what lets an external caller reach it.
        #
        # Serialized on the tenant, then RE-READ under the lock: the
        # loser must find the winner's rows rather than seed again.
        from harkeniq.audit.chain import pg_advisory_chain_lock

        await pg_advisory_chain_lock(
            self.session, f"cc.capability_catalogue.{tenant_id}"
        )
        rows = (
            await self.session.execute(
                select(CCCapabilityCatalogue)
                .where(CCCapabilityCatalogue.tenant_id == tenant_id)
                # PB-2 (A30.42 D-PB8): candidates in catalogue order on EVERY
                # read, this re-read under the lock included.
                .order_by(
                    CCCapabilityCatalogue.subsystem,
                    CCCapabilityCatalogue.action_type,
                )
            )
        ).scalars().all()
        if rows:
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
