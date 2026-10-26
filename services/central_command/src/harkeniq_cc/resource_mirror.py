"""The legacy mirror: the Resource Catalogue's one writer (TX-1 spec A31.5,
TX-2 spec A32.4, TX-3 spec A33).

Reconciles the catalogue FROM the authoritative objects -- `cc_sites`,
`cc_fleet_cache`, and the racks each site's Site Manager reports
(`cc_site_topology`) -- and never the other way round. Nothing it writes is
an input to any authorization, execution, safety, dispatch or correlation
decision; visibility is derived from the canonical objects at read time
(`repos.scope_resources`), so a lagging mirror can delay when a resource
EXISTS but never widen or narrow who may see it.

What it does, per pass:

* one resource per site, per Site Manager rack and per Harken Node
  `agent_id`, created on first sight and described from the canonical row
  thereafter;
* a rack's primary containment parent is its site; a device's is its rack
  when its own site's report places it in a rack of that site, and its
  site otherwise -- unless the same `agent_id` is reported at two sites at
  once (a move in flight), when its existing placement is left exactly as
  it was;
* a site whose Site Manager has not reported its racks (one predating
  TX-2) is UNKNOWN, not empty: its racks are neither created nor retired,
  and a device already in one of its racks stays there;
* a placement that names a rack of ANOTHER site, or that two racks claim,
  is refused, and the device stays under its site; a rack id reported at
  two sites at once is held, and so are the devices it claims;
* a re-placement retires the old edge and creates or revives the new one,
  so a former placement survives as history (A32.2);
* a resource whose canonical object has gone is RETIRED, never deleted: it
  keeps its id, last placement, legacy link, identifier, aliases and last
  observation, so a historical deep link still resolves; if the object
  comes back, the same resource revives;
* the identifiers HarkenIQ issues itself are bound -- and a binding that
  already names another resource is reported and left alone, never merged;
* TX-3 (A33.3): a device's reported `service_tag` is recorded as an alias,
  retired when the node reports another; a replaced display name is kept
  as a `historical_name` alias. Neither is ever an identity;
* TX-3 (A33.4): every fact it asserts carries its origin -- `harkeniq` and
  the feed it came from -- and the canonical object's own observation
  stamp, advanced at most once per OBSERVATION_REFRESH. A held placement is
  not re-asserted, so it is not observed either.

Idempotent: a pass over an unchanged estate writes nothing within an
observation refresh. Serialized across Central Command replicas by a
transaction-scoped advisory lock, which every catalogue write runs inside.
It writes no audit entry: the catalogue is a projection of state recorded
at its source, not a governance action.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import select

from harkeniq_cc.db.models import CCFleetCache, CCSite
from harkeniq_cc.db.repos import ResourceRepo, SiteTopologyRepo, clean_topology_racks
from harkeniq_cc.resource_catalogue import (
    ALIAS_SERVICE_TAG,
    EXTERNAL_SOURCE_HARKENIQ,
    FEED_FLEET_SNAPSHOT,
    FEED_SITE_REGISTRY,
    FEED_SITE_TOPOLOGY,
    KIND_PHYSICAL_CONTAINER,
    KIND_PHYSICAL_DEVICE,
    LEGACY_DEVICE,
    LEGACY_RACK,
    LEGACY_SITE,
    LIFECYCLE_ACTIVE,
    LIFECYCLE_RETIRED,
    NAMESPACE_HARKEN_NODE,
    NAMESPACE_RACK,
    NAMESPACE_SITE,
    SOURCE_LEGACY_MIRROR,
    TYPE_RACK,
    TYPE_SITE,
    CatalogueError,
    IdentityConflict,
    device_type_for_class,
    normalize_alias,
)

logger = logging.getLogger("harkeniq.cc.resource_mirror")

_LOCK = "cc.resource_mirror:{tenant_id}"

#: The mirror's three feeds, as fact origins (A33.4).
_SITES = (EXTERNAL_SOURCE_HARKENIQ, FEED_SITE_REGISTRY)
_FLEET = (EXTERNAL_SOURCE_HARKENIQ, FEED_FLEET_SNAPSHOT)
_TOPOLOGY = (EXTERNAL_SOURCE_HARKENIQ, FEED_SITE_TOPOLOGY)


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
    #: Devices reported at more than one site this pass, and rack ids
    #: reported more than once (placement held).
    ambiguous: int = 0
    #: Rack placements refused this pass -- a rack of another site, or two
    #: racks claiming one device; the device stays under its site (A32.4).
    refused: int = 0
    #: Facts whose provenance was recorded or whose observation advanced
    #: (A33.4).
    observed: int = 0
    #: Aliases recorded, revived or retired (A33.3).
    aliased: int = 0

    @property
    def changed(self) -> bool:
        return bool(
            self.created or self.updated or self.retired
            or self.revived or self.reparented or self.bound
            or self.observed or self.aliased
        )


async def _lock(session, tenant_id: str) -> bool:
    from harkeniq.audit.chain import pg_advisory_chain_lock

    return await pg_advisory_chain_lock(session, _LOCK.format(tenant_id=tenant_id))


async def _observe(repo: ResourceRepo, report: MirrorReport, row, stamp, origin=None) -> None:
    if await repo.record_observation(row, stamp, origin=origin):
        report.observed += 1


async def _bind(
    repo: ResourceRepo,
    report: MirrorReport,
    tenant_id: str,
    resource,
    namespace: str,
    external_id: str,
    bound: dict,
    stamp,
) -> None:
    held = bound.get(external_id)
    if held is not None and held.resource_id == resource.id:
        await _observe(repo, report, held, stamp)
        return
    if held is None:
        try:
            bound[external_id] = await repo.bind_external_id(
                tenant_id, resource.id,
                source_system=EXTERNAL_SOURCE_HARKENIQ, namespace=namespace,
                external_id=external_id, source=SOURCE_LEGACY_MIRROR,
                observed_at=stamp,
            )
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


async def _place(
    repo: ResourceRepo,
    report: MirrorReport,
    tenant_id: str,
    child,
    parent,
    placed: dict,
    what: str,
    *,
    origin: tuple[str, str],
    stamp,
    asserted: bool = True,
) -> None:
    """Make `parent` the child's primary parent, unless it already is.

    An existing placement this pass re-asserts is observed; a HELD one --
    `asserted` false -- is not, because nothing new is known about it."""
    edge = placed.get(child.id)
    if edge is not None and edge.from_resource_id == parent.id:
        if asserted:
            await _observe(repo, report, edge, stamp, origin)
        return
    try:
        placed[child.id] = await repo.set_primary_parent(
            tenant_id, child.id, parent.id, source=SOURCE_LEGACY_MIRROR,
            origin=origin, observed_at=stamp,
        )
        report.reparented += 1
    except CatalogueError as exc:
        logger.warning(
            "Resource catalogue: %s not placed under %s: %s", what, parent.id, exc,
        )


async def _service_tags(
    repo: ResourceRepo,
    report: MirrorReport,
    tenant_id: str,
    resource,
    rows,
    known: dict,
    stamp,
) -> None:
    """The node's reported `service_tag`, as an alias -- never an identity
    (A33.3). Retired when the node reports another; a tombstone keeps it."""
    reported: dict[str, str] = {}
    for row in rows:
        shown = (row.service_tag or "").strip()
        if shown:
            reported.setdefault(normalize_alias(shown), shown)
    for normalized, shown in sorted(reported.items()):
        alias = known.get(normalized)
        if alias is None:
            try:
                known[normalized] = await repo.create_alias(
                    tenant_id, resource, alias_type=ALIAS_SERVICE_TAG, value=shown,
                    origin=_FLEET, source=SOURCE_LEGACY_MIRROR, observed_at=stamp,
                )
                report.aliased += 1
            except CatalogueError as exc:
                logger.warning(
                    "Resource catalogue: service tag of %s not recorded: %s",
                    resource.id, exc,
                )
        elif alias.lifecycle_state != LIFECYCLE_ACTIVE or alias.value != shown:
            await repo.revive_alias(alias, value=shown, observed_at=stamp)
            report.aliased += 1
        else:
            await _observe(repo, report, alias, stamp)
    for normalized, alias in sorted(known.items()):
        if normalized not in reported and alias.lifecycle_state == LIFECYCLE_ACTIVE:
            await repo.retire_alias(alias)
            report.aliased += 1


#: A claim made by a rack that is itself held this pass: the device's
#: placement is unknown, not refused.
_HELD = object()


async def reconcile(session, tenant_id: str) -> MirrorReport:
    """One pass: make the catalogue reflect this tenant's sites, racks and
    fleet.

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
    #: A site is observed at its last successful poll (A33.4).
    site_seen = {site.id: site.last_seen_at for site in sites}
    for site in sites:
        stamp = site_seen[site.id]
        resource = site_resources.get(site.id)
        if resource is None:
            resource = await repo.create_mirrored(
                tenant_id,
                legacy_kind=LEGACY_SITE, legacy_ref=site.id, legacy_site_id=site.id,
                resource_kind=KIND_PHYSICAL_CONTAINER, resource_type=TYPE_SITE,
                display_name=site.site_name, observed_at=stamp,
            )
            site_resources[site.id] = resource
            report.created += 1
        else:
            if await repo.refresh_mirrored(
                resource, display_name=site.site_name,
                resource_type=TYPE_SITE, legacy_site_id=site.id, observed_at=stamp,
            ):
                report.updated += 1
            if resource.lifecycle_state == LIFECYCLE_RETIRED:
                await repo.revive(resource, observed_at=stamp)
                report.revived += 1
            await _observe(repo, report, resource, stamp, _SITES)
        await _bind(
            repo, report, tenant_id, resource, NAMESPACE_SITE, site.id, sites_bound,
            stamp,
        )
    present_sites = {site.id for site in sites}
    for ref, resource in site_resources.items():
        if ref not in present_sites and resource.lifecycle_state == LIFECYCLE_ACTIVE:
            await repo.retire(resource)
            report.retired += 1

    # -- racks (TX-2, A32.4) ----------------------------------------------
    # Only reports of THIS tenant's present sites count, and only those
    # that were reported at all: an unreported site is UNKNOWN.
    reports = {
        row.site_id: row
        for row in await SiteTopologyRepo(session).list_for_tenant(tenant_id)
        if row.site_id in present_sites
    }
    known_sites = {site_id for site_id, row in reports.items() if row.reported}
    claims: dict[str, list[tuple[str, dict]]] = {}
    for site_id in sorted(known_sites):
        for rack in clean_topology_racks(reports[site_id].racks):
            claims.setdefault(rack["rack_id"], []).append((site_id, rack))

    rack_resources = {
        r.legacy_ref: r for r in await repo.list_mirrored(tenant_id, LEGACY_RACK)
    }
    racks_bound = await repo.bound_external_ids(
        tenant_id, EXTERNAL_SOURCE_HARKENIQ, NAMESPACE_RACK
    )
    racks_placed = await repo.primary_parent_edges(
        tenant_id, [r.id for r in rack_resources.values()]
    )
    #: agent id -> every (reporting site, rack id or _HELD) claiming it
    members: dict[str, list[tuple[str, object]]] = {}
    for rack_id, entries in sorted(claims.items()):
        if len(entries) > 1:
            # One rack id reported twice -- at two sites at once, or twice
            # by one report. Hold it, like a device at two sites: the next
            # pass that sees it once settles it.
            report.ambiguous += 1
            for site_id, rack in entries:
                for agent_id in rack["device_agent_ids"]:
                    members.setdefault(agent_id, []).append((site_id, _HELD))
            continue
        site_id, rack = entries[0]
        # A rack, and the containment its site's report states, is observed
        # at that site's last successful poll, which carried the report.
        stamp = site_seen[site_id]
        name = rack["name"] or rack_id
        resource = rack_resources.get(rack_id)
        if resource is None:
            resource = await repo.create_mirrored(
                tenant_id,
                legacy_kind=LEGACY_RACK, legacy_ref=rack_id, legacy_site_id=site_id,
                resource_kind=KIND_PHYSICAL_CONTAINER, resource_type=TYPE_RACK,
                display_name=name, observed_at=stamp,
            )
            rack_resources[rack_id] = resource
            report.created += 1
        else:
            if await repo.refresh_mirrored(
                resource, display_name=name,
                resource_type=TYPE_RACK, legacy_site_id=site_id, observed_at=stamp,
            ):
                report.updated += 1
            if resource.lifecycle_state == LIFECYCLE_RETIRED:
                await repo.revive(resource, observed_at=stamp)
                report.revived += 1
            await _observe(repo, report, resource, stamp, _TOPOLOGY)
        await _bind(
            repo, report, tenant_id, resource, NAMESPACE_RACK, rack_id, racks_bound,
            stamp,
        )
        await _place(
            repo, report, tenant_id, resource, site_resources[site_id],
            racks_placed, f"rack {rack_id}", origin=_TOPOLOGY, stamp=stamp,
        )
        for agent_id in rack["device_agent_ids"]:
            members.setdefault(agent_id, []).append((site_id, rack_id))
    for rack_id, resource in rack_resources.items():
        if rack_id in claims or resource.lifecycle_state != LIFECYCLE_ACTIVE:
            continue
        if (resource.legacy_site_id in known_sites
                or resource.legacy_site_id not in present_sites):
            # Gone from its site's current report, or its site is gone: a
            # tombstone, keeping its last placement. An UNKNOWN report
            # holds it as it is.
            await repo.retire(resource)
            report.retired += 1
    racks_by_resource = {r.id: r for r in rack_resources.values()}

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
    tags = await repo.aliases_by_resource(
        tenant_id, ALIAS_SERVICE_TAG,
        source_system=_FLEET[0], source_namespace=_FLEET[1],
    )
    for agent_id, rows in by_agent.items():
        at_sites = sorted({row.site_id for row in rows})
        single = len(at_sites) == 1
        row = rows[0]
        name = row.agent_name or agent_id
        resource_type = device_type_for_class(row.device_class)
        # A device is observed when Central Command last refreshed its fleet
        # row from a successful poll of its site (A33.4).
        stamp = max(r.snapshot_at for r in rows)
        resource = device_resources.get(agent_id)
        if resource is None:
            resource = await repo.create_mirrored(
                tenant_id,
                legacy_kind=LEGACY_DEVICE, legacy_ref=agent_id,
                # First seen mid-move: no placement until it resolves.
                legacy_site_id=at_sites[0] if single else None,
                resource_kind=KIND_PHYSICAL_DEVICE, resource_type=resource_type,
                display_name=name, observed_at=stamp,
            )
            device_resources[agent_id] = resource
            report.created += 1
        else:
            if single and await repo.refresh_mirrored(
                resource, display_name=name,
                resource_type=resource_type, legacy_site_id=at_sites[0],
                observed_at=stamp,
            ):
                report.updated += 1
            if resource.lifecycle_state == LIFECYCLE_RETIRED:
                await repo.revive(resource, observed_at=stamp)
                report.revived += 1
            await _observe(repo, report, resource, stamp, _FLEET)
        await _bind(
            repo, report, tenant_id, resource,
            NAMESPACE_HARKEN_NODE, agent_id, nodes_bound, stamp,
        )
        await _service_tags(
            repo, report, tenant_id, resource, rows,
            tags.setdefault(resource.id, {}), stamp,
        )
        if not single:
            # Two sites at once is a move in flight. Hold the placement
            # rather than flap it between them; the next pass that sees one
            # site settles it.
            report.ambiguous += 1
            continue
        site_id = at_sites[0]
        parent, asserted = _device_parent(
            report, agent_id, site_id,
            site=site_resources[site_id],
            claims=members.get(agent_id, []),
            known=site_id in known_sites,
            current=placed.get(resource.id),
            racks_by_resource=racks_by_resource,
            rack_resources=rack_resources,
        )
        in_rack = parent.legacy_kind == LEGACY_RACK
        await _place(
            repo, report, tenant_id, resource, parent, placed, f"device {agent_id}",
            origin=_TOPOLOGY if in_rack else _FLEET,
            stamp=site_seen[site_id] if in_rack else stamp,
            asserted=asserted,
        )
    for ref, resource in device_resources.items():
        if ref not in by_agent and resource.lifecycle_state == LIFECYCLE_ACTIVE:
            # Gone from every site: a tombstone, keeping its last placement.
            await repo.retire(resource)
            report.retired += 1
    return report


def _device_parent(
    report: MirrorReport,
    agent_id: str,
    site_id: str,
    *,
    site,
    claims: list[tuple[str, object]],
    known: bool,
    current,
    racks_by_resource: dict,
    rack_resources: dict,
):
    """Where one device, reported at exactly one site, is placed (A32.4),
    and whether this pass ASSERTS that placement (A33.4).

    Its rack, when its OWN site's report places it in exactly one rack of
    that site; its site otherwise. A claim from another site's report, or a
    second claim, refuses the rack placement -- the device stays under its
    site, which is always true. A site whose report is unknown, or a claim
    by a held rack, keeps a placement the device already has in a live rack
    of this same site, because nothing new is known about it: that one is
    held, not asserted.
    """
    own = [rack for claim_site, rack in claims if claim_site == site_id]
    foreign = [rack for claim_site, rack in claims if claim_site != site_id]
    decided = known and _HELD not in own
    refused = bool(foreign) or (decided and len(own) > 1)
    if refused:
        # A rack of another site never takes this device, and two racks
        # claiming one device is a report that cannot both be true.
        report.refused += 1
        logger.debug(
            "Resource catalogue: device %s's rack placement refused (%d claim(s) "
            "at its site %s, %d from another site); not placed in a rack",
            agent_id, len(own), site_id, len(foreign),
        )
    if decided:
        if refused or not own:
            return site, True
        return rack_resources[own[0]], True
    # Unknown: hold a live same-site rack placement, or fall back to the site.
    held = racks_by_resource.get(current.from_resource_id) if current is not None else None
    if (held is not None and held.lifecycle_state == LIFECYCLE_ACTIVE
            and held.legacy_site_id == site_id):
        return held, False
    return site, True


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
            if report.changed or report.conflicts or report.refused:
                logger.info("Resource catalogue reconciled: %s", report)
        except Exception as exc:
            logger.error("Resource catalogue reconcile failed: %s", exc)
        await asyncio.sleep(interval)
