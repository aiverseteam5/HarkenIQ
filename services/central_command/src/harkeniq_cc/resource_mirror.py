"""The legacy mirror: the Resource Catalogue's one writer (TX-1, spec A31.5).

Reconciles the catalogue FROM the authoritative objects -- `cc_sites` and
`cc_fleet_cache` -- and never the other way round. Nothing it writes is an
input to any authorization, execution, safety, dispatch or correlation
decision; visibility is derived from the canonical objects at read time
(`repos.scope_resources`), so a lagging mirror can delay when a resource
EXISTS but never widen or narrow who may see it.

What it does, per pass:

* one resource per site and per Harken Node `agent_id`, created on first
  sight and described from the canonical row thereafter;
* a device's primary containment parent is its site -- unless the same
  `agent_id` is reported at two sites at once (a move in flight), when its
  existing placement is left exactly as it was;
* a resource whose canonical object has gone is RETIRED, never deleted: it
  keeps its id, last placement and legacy link, so a historical deep link
  still resolves; if the object comes back, the same resource revives;
* the identifiers HarkenIQ issues itself are bound -- and a binding that
  already names another resource is reported and left alone, never merged.

Idempotent: a pass over an unchanged estate writes nothing. Serialized
across Central Command replicas by a transaction-scoped advisory lock.
It writes no audit entry: the catalogue is a projection of state recorded
at its source, not a governance action.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import select

from harkeniq_cc.db.models import CCFleetCache, CCSite
from harkeniq_cc.db.repos import ResourceRepo
from harkeniq_cc.resource_catalogue import (
    EXTERNAL_SOURCE_HARKENIQ,
    KIND_PHYSICAL_CONTAINER,
    KIND_PHYSICAL_DEVICE,
    LEGACY_DEVICE,
    LEGACY_SITE,
    LIFECYCLE_ACTIVE,
    LIFECYCLE_RETIRED,
    NAMESPACE_HARKEN_NODE,
    NAMESPACE_SITE,
    SOURCE_LEGACY_MIRROR,
    TYPE_SITE,
    CatalogueError,
    IdentityConflict,
    device_type_for_class,
)

logger = logging.getLogger("harkeniq.cc.resource_mirror")

_LOCK = "cc.resource_mirror:{tenant_id}"


@dataclass
class MirrorReport:
    created: int = 0
    updated: int = 0
    retired: int = 0
    revived: int = 0
    reparented: int = 0
    bound: int = 0
    #: An identifier already bound to a different resource. Reported, never
    #: resolved -- resolving it would be the silent merge A31.2 forbids.
    conflicts: int = 0
    #: Devices reported at more than one site this pass (placement held).
    ambiguous: int = 0

    @property
    def changed(self) -> bool:
        return bool(
            self.created or self.updated or self.retired
            or self.revived or self.reparented or self.bound
        )


async def _lock(session, tenant_id: str) -> bool:
    from harkeniq.audit.chain import pg_advisory_chain_lock

    return await pg_advisory_chain_lock(session, _LOCK.format(tenant_id=tenant_id))


async def _bind(
    repo: ResourceRepo,
    report: MirrorReport,
    tenant_id: str,
    resource,
    namespace: str,
    external_id: str,
    bound: dict[str, str],
) -> None:
    owner = bound.get(external_id)
    if owner == resource.id:
        return
    if owner is None:
        try:
            await repo.bind_external_id(
                tenant_id, resource.id,
                source_system=EXTERNAL_SOURCE_HARKENIQ, namespace=namespace,
                external_id=external_id, source=SOURCE_LEGACY_MIRROR,
            )
            bound[external_id] = resource.id
            report.bound += 1
            return
        except IdentityConflict:
            pass
    report.conflicts += 1
    logger.warning(
        "Resource catalogue: %s/%s/%s already names another resource; "
        "resource %s is left without it (no merge, no reassignment)",
        EXTERNAL_SOURCE_HARKENIQ, namespace, external_id, resource.id,
    )


async def reconcile(session, tenant_id: str) -> MirrorReport:
    """One pass: make the catalogue reflect this tenant's sites and fleet.

    The caller commits. Everything is read inside one transaction holding
    the mirror's advisory lock, so a concurrent replica waits and then
    finds nothing left to do.
    """
    await _lock(session, tenant_id)
    repo = ResourceRepo(session)
    report = MirrorReport()

    # -- sites ------------------------------------------------------------
    sites = (
        await session.execute(
            select(CCSite).where(CCSite.tenant_id == tenant_id).order_by(CCSite.id)
        )
    ).scalars().all()
    site_resources = {
        r.legacy_ref: r for r in await repo.list_mirrored(tenant_id, LEGACY_SITE)
    }
    sites_bound = await repo.bound_external_ids(
        tenant_id, EXTERNAL_SOURCE_HARKENIQ, NAMESPACE_SITE
    )
    for site in sites:
        resource = site_resources.get(site.id)
        if resource is None:
            resource = await repo.create_mirrored(
                tenant_id,
                legacy_kind=LEGACY_SITE, legacy_ref=site.id, legacy_site_id=site.id,
                resource_kind=KIND_PHYSICAL_CONTAINER, resource_type=TYPE_SITE,
                display_name=site.site_name,
            )
            site_resources[site.id] = resource
            report.created += 1
        else:
            if await repo.refresh_mirrored(
                resource, display_name=site.site_name,
                resource_type=TYPE_SITE, legacy_site_id=site.id,
            ):
                report.updated += 1
            if resource.lifecycle_state == LIFECYCLE_RETIRED:
                await repo.revive(resource)
                report.revived += 1
        await _bind(
            repo, report, tenant_id, resource, NAMESPACE_SITE, site.id, sites_bound
        )
    present_sites = {site.id for site in sites}
    for ref, resource in site_resources.items():
        if ref not in present_sites and resource.lifecycle_state == LIFECYCLE_ACTIVE:
            await repo.retire(resource)
            report.retired += 1

    # -- devices ------------------------------------------------------------
    fleet = (
        await session.execute(
            select(CCFleetCache)
            .join(CCSite, CCSite.id == CCFleetCache.site_id)
            .where(CCSite.tenant_id == tenant_id)
            .order_by(CCFleetCache.agent_id, CCFleetCache.site_id)
        )
    ).scalars().all()
    by_agent: dict[str, list[CCFleetCache]] = {}
    for row in fleet:
        if row.agent_id:
            by_agent.setdefault(row.agent_id, []).append(row)

    device_resources = {
        r.legacy_ref: r for r in await repo.list_mirrored(tenant_id, LEGACY_DEVICE)
    }
    nodes_bound = await repo.bound_external_ids(
        tenant_id, EXTERNAL_SOURCE_HARKENIQ, NAMESPACE_HARKEN_NODE
    )
    placed = await repo.primary_parent_edges(
        tenant_id, [r.id for r in device_resources.values()]
    )
    for agent_id, rows in by_agent.items():
        at_sites = sorted({row.site_id for row in rows})
        single = len(at_sites) == 1
        row = rows[0]
        name = row.agent_name or agent_id
        resource_type = device_type_for_class(row.device_class)
        resource = device_resources.get(agent_id)
        if resource is None:
            resource = await repo.create_mirrored(
                tenant_id,
                legacy_kind=LEGACY_DEVICE, legacy_ref=agent_id,
                # First seen mid-move: no placement until it resolves.
                legacy_site_id=at_sites[0] if single else None,
                resource_kind=KIND_PHYSICAL_DEVICE, resource_type=resource_type,
                display_name=name,
            )
            device_resources[agent_id] = resource
            report.created += 1
        else:
            if single and await repo.refresh_mirrored(
                resource, display_name=name,
                resource_type=resource_type, legacy_site_id=at_sites[0],
            ):
                report.updated += 1
            if resource.lifecycle_state == LIFECYCLE_RETIRED:
                await repo.revive(resource)
                report.revived += 1
        await _bind(
            repo, report, tenant_id, resource,
            NAMESPACE_HARKEN_NODE, agent_id, nodes_bound,
        )
        if not single:
            # Two sites at once is a move in flight. Hold the placement
            # rather than flap it between them; the next pass that sees one
            # site settles it.
            report.ambiguous += 1
            continue
        parent = site_resources[at_sites[0]]
        edge = placed.get(resource.id)
        if edge is None or edge.from_resource_id != parent.id:
            try:
                await repo.set_primary_parent(
                    tenant_id, resource.id, parent.id, source=SOURCE_LEGACY_MIRROR
                )
                report.reparented += 1
            except CatalogueError as exc:
                logger.warning(
                    "Resource catalogue: device %s not placed under site %s: %s",
                    agent_id, at_sites[0], exc,
                )
    for ref, resource in device_resources.items():
        if ref not in by_agent and resource.lifecycle_state == LIFECYCLE_ACTIVE:
            # Gone from every site: a tombstone, keeping its last placement.
            await repo.retire(resource)
            report.retired += 1
    return report


async def resource_mirror_loop(state) -> None:
    """Reconcile at boot, then every `site_poll_interval_s` (A31.5).

    The first pass runs immediately, so an upgraded deployment's catalogue
    is populated without waiting a poll interval. A failed pass is logged
    and retried on the next tick; it never stops the loop.
    """
    interval = state.config.site_poll_interval_s
    logger.info("Resource catalogue mirror started (interval=%.0fs)", interval)
    while True:
        try:
            async with state.sessionmaker() as session:
                report = await reconcile(session, state.config.tenant_id)
                await session.commit()
            if report.changed or report.conflicts:
                logger.info("Resource catalogue reconciled: %s", report)
        except Exception as exc:
            logger.error("Resource catalogue reconcile failed: %s", exc)
        await asyncio.sleep(interval)
