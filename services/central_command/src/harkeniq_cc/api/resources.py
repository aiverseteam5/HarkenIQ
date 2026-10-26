"""Resource Catalogue API: six reads (TX-1 spec A31.6, TX-2 spec A32.8,
TX-3 spec A33.10).

Human-only (absent from `MACHINE_SURFACE`), `fleet.view`, `READ_SCOPED`.
Every read filters EVERY resource it returns through
`repos.scope_resources`, which derives a resource's visibility from its
CANONICAL legacy object -- the Sites and Fleet predicates -- and from
nothing in the catalogue. A hidden resource reads exactly as a nonexistent
one: 404, or absent from a list and from its count. The topology reads
(parents, children, a subtree) walk containment edges, and the walk is a
description too: it stops at, or skips, what its reader cannot read, and
never shows what lies beyond. Nothing here grants, implies or describes
authority.

TX-3 adds no route. A resource now shows where its description came from
(provenance), the names and labels it is known by (aliases -- never
identity), and the other visible resources that MAY be the same physical
thing (identity candidates -- never a match, never acted on).
"""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_cc.api.deps import get_scope, get_session, require_permission
from harkeniq_cc.auth import UserContext
from harkeniq_cc.db.repos import ResourceRepo
from harkeniq_cc.resource_catalogue import (
    IDENTITY_EVIDENCE_ALIAS_TYPES,
    MAX_ALIAS_LENGTH,
    MAX_IDENTITY_CANDIDATES,
    MAX_TOPOLOGY_DEPTH,
    MAX_TOPOLOGY_NODES,
)
from harkeniq_cc.scope import read_reach

router = APIRouter(prefix="/api/resources", tags=["resources"])

#: The closed `resource_kind` vocabulary, as a query type. A test holds it
#: equal to `resource_catalogue.RESOURCE_KINDS`.
ResourceKindFilter = Literal[
    "component", "logical_resource", "physical_container", "physical_device",
]
LifecycleFilter = Literal["active", "retired", "any"]
#: The closed `alias_type` vocabulary (A33.3), held equal to
#: `resource_catalogue.ALIAS_TYPES` by a test.
AliasTypeFilter = Literal[
    "asset_tag", "fqdn", "historical_name", "hostname", "operator_label",
    "service_tag",
]


def _lifecycle(value: str) -> Optional[str]:
    return None if value == "any" else value


def _iso(value) -> Optional[str]:
    return value.isoformat() if value else None


def _summary(row) -> dict:
    """What a resource is. Never what anybody may do to it."""
    return {
        "id": row.id,
        "resource_kind": row.resource_kind,
        "resource_type": row.resource_type,
        "display_name": row.display_name,
        "lifecycle_state": row.lifecycle_state,
    }


def _observed(row) -> dict:
    """When the fact's source observed it (A33.4) -- null: not recorded."""
    return {
        "first_observed_at": _iso(row.first_observed_at),
        "last_observed_at": _iso(row.last_observed_at),
    }


def _provenance(row) -> dict:
    """Where a description came from. Never a reason to trust or act."""
    return {
        "source_system": row.source_system,
        "source_namespace": row.source_namespace,
        **_observed(row),
    }


def _edge_summary(edge) -> dict:
    """What a relationship says. A description, never a grant."""
    return {
        "id": edge.id,
        "relationship_type": edge.relationship_type,
        "is_primary": bool(edge.is_primary),
        "source": edge.source,
        "lifecycle_state": edge.lifecycle_state,
        "provenance": _provenance(edge),
    }


def _alias_dict(alias) -> dict:
    """A name or label the resource is known by. Never its identity."""
    return {
        "alias_type": alias.alias_type,
        "value": alias.value,
        "lifecycle_state": alias.lifecycle_state,
        "retired_at": _iso(alias.retired_at),
        "provenance": _provenance(alias),
    }


def _resource_dict(row) -> dict:
    out = _summary(row)
    out.update({
        "source": row.source,
        "provenance": _provenance(row),
        # The canonical object this resource mirrors -- an id the reader
        # can already see through Sites or Fleet, or (TX-2) the Site
        # Manager's own id for a rack at a site the reader can see.
        # `legacy_site_id` is an internal anchor for site-owned visibility
        # and is not published.
        "legacy": (
            {"kind": row.legacy_kind, "ref": row.legacy_ref}
            if row.legacy_kind else None
        ),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
        "retired_at": _iso(row.retired_at),
    })
    return out


@router.get(
    "/",
    dependencies=[Depends(require_permission("fleet.view"))],
)
async def list_resources(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    kind: Optional[ResourceKindFilter] = None,
    resource_type: Optional[str] = Query(None, max_length=128),
    lifecycle_state: Literal["active", "retired", "any"] = "active",
    source_system: Optional[str] = Query(None, min_length=1, max_length=64),
    namespace: Optional[str] = Query(None, min_length=1, max_length=128),
    external_id: Optional[str] = Query(None, min_length=1, max_length=512),
    alias_type: Optional[AliasTypeFilter] = None,
    alias: Optional[str] = Query(None, min_length=1, max_length=MAX_ALIAS_LENGTH),
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    """The resources the caller may read, paginated.

    `source_system` + `namespace` + `external_id` together look a resource
    up by an identifier; a hidden resource's identifier finds nothing.
    `alias_type` + `alias` together find the resources known by that name
    or label, current or former (A33.3) -- possibly many, because an alias
    is never an identity; a hidden resource's alias finds nothing.
    """
    lookup = (source_system, namespace, external_id)
    if any(lookup) and not all(lookup):
        raise HTTPException(
            status_code=422,
            detail="an external-id lookup names source_system, namespace "
                   "and external_id together",
        )
    if (alias_type is None) != (alias is None):
        raise HTTPException(
            status_code=422,
            detail="an alias lookup names alias_type and alias together",
        )
    reach = read_reach(scope, "fleet.view")
    rows, total = await ResourceRepo(session).list_visible(
        user.tenant_id,
        scope=reach,
        resource_kind=kind,
        resource_type=resource_type,
        lifecycle_state=None if lifecycle_state == "any" else lifecycle_state,
        external=lookup if all(lookup) else None,
        alias=(alias_type, alias) if alias_type is not None else None,
        page=page,
        page_size=page_size,
    )
    items = [_resource_dict(r) for r in rows]
    return {
        "resources": items,
        # alias for console-ui's PaginatedResponse shape
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "tenant_id": user.tenant_id,
    }


@router.get(
    "/{resource_id}",
    dependencies=[Depends(require_permission("fleet.view"))],
)
async def get_resource(
    resource_id: str,
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    """One resource, any lifecycle state -- a retired one is a tombstone a
    historical link still resolves to. Its primary parent appears only
    when the caller may read the parent too; otherwise it reads as none.

    TX-3 (A33): its identifier with when it was observed; its aliases,
    current and former; and its identity candidates -- the OTHER resources
    the caller may read that share a serial-like alias with it, and so may
    be the same physical resource. A candidate is a hint for a person:
    nothing merges, and one the caller cannot read is neither shown nor
    counted."""
    reach = read_reach(scope, "fleet.view")
    repo = ResourceRepo(session)
    row = await repo.get_visible(user.tenant_id, resource_id, scope=reach)
    if row is None:
        raise HTTPException(status_code=404, detail="resource not found")
    parent = await repo.primary_parent_visible(
        user.tenant_id, row.id, scope=reach
    )
    detail = _resource_dict(row)
    detail["primary_parent"] = _summary(parent) if parent is not None else None
    detail["external_ids"] = [
        {
            "source_system": e.source_system,
            "namespace": e.namespace,
            "external_id": e.external_id,
            **_observed(e),
        }
        for e in await repo.external_ids(user.tenant_id, row.id)
    ]
    detail["aliases"] = [
        _alias_dict(a) for a in await repo.aliases(user.tenant_id, row.id)
    ]
    candidates, truncated = await repo.identity_candidates_visible(
        user.tenant_id, row.id, scope=reach, limit=MAX_IDENTITY_CANDIDATES,
    )
    detail["identity_candidates"] = {
        "basis": sorted(IDENTITY_EVIDENCE_ALIAS_TYPES),
        "candidates": [
            {
                "resource": _summary(other),
                "shared": [
                    {"alias_type": a.alias_type, "value": a.value} for a in shared
                ],
            }
            for other, shared in candidates
        ],
        "truncated": truncated,
    }
    return detail


@router.get(
    "/{resource_id}/relationships",
    dependencies=[Depends(require_permission("fleet.view"))],
)
async def list_relationships(
    resource_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    relationship_type: Optional[str] = Query(None, max_length=32),
    direction: Optional[Literal["outgoing", "incoming"]] = None,
    lifecycle_state: LifecycleFilter = "active",
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    """The typed edges touching one resource whose other end the caller may
    also read. `outgoing`: "<this> TYPE <other>"; `incoming`: the reverse.
    `lifecycle_state` filters the edges (A32.2): the current ones by
    default, former placements with `retired`. A relationship is a
    description: it neither shows a hidden resource nor confers anything
    over a visible one."""
    reach = read_reach(scope, "fleet.view")
    repo = ResourceRepo(session)
    row = await repo.get_visible(user.tenant_id, resource_id, scope=reach)
    if row is None:
        raise HTTPException(status_code=404, detail="resource not found")
    edges, total = await repo.relationships_visible(
        user.tenant_id,
        row.id,
        scope=reach,
        relationship_type=relationship_type,
        direction=direction,
        lifecycle_state=_lifecycle(lifecycle_state),
        page=page,
        page_size=page_size,
    )
    return {
        "resource_id": row.id,
        "relationships": [
            {
                **_edge_summary(edge),
                "direction": way,
                "resource": _summary(other),
            }
            for edge, other, way in edges
        ],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


@router.get(
    "/{resource_id}/parents",
    dependencies=[Depends(require_permission("fleet.view"))],
)
async def list_parents(
    resource_id: str,
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    """The resource's primary-containment ancestors the caller may read,
    nearest first -- a breadcrumb (A32.8). The chain stops at the first
    ancestor the caller cannot read, and then reads exactly as a chain that
    ends at a root: nothing beyond a hidden ancestor is shown or implied."""
    reach = read_reach(scope, "fleet.view")
    repo = ResourceRepo(session)
    row = await repo.get_visible(user.tenant_id, resource_id, scope=reach)
    if row is None:
        raise HTTPException(status_code=404, detail="resource not found")
    chain = await repo.ancestors_visible(user.tenant_id, row.id, scope=reach)
    return {
        "resource_id": row.id,
        "parents": [
            {
                "depth": depth,
                "relationship": _edge_summary(edge),
                "resource": _summary(parent),
            }
            for depth, (edge, parent) in enumerate(chain, start=1)
        ],
    }


@router.get(
    "/{resource_id}/children",
    dependencies=[Depends(require_permission("fleet.view"))],
)
async def list_children(
    resource_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    lifecycle_state: LifecycleFilter = "active",
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    """What the resource primarily contains that the caller may read,
    paginated (A32.8). A hidden child is not listed and not counted.
    `lifecycle_state` filters the CHILD: a tombstone still placed here is
    `retired`."""
    reach = read_reach(scope, "fleet.view")
    repo = ResourceRepo(session)
    row = await repo.get_visible(user.tenant_id, resource_id, scope=reach)
    if row is None:
        raise HTTPException(status_code=404, detail="resource not found")
    children, total = await repo.children_visible(
        user.tenant_id,
        row.id,
        scope=reach,
        lifecycle_state=_lifecycle(lifecycle_state),
        page=page,
        page_size=page_size,
    )
    return {
        "resource_id": row.id,
        "children": [
            {"relationship": _edge_summary(edge), "resource": _summary(child)}
            for edge, child in children
        ],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


@router.get(
    "/{resource_id}/topology",
    dependencies=[Depends(require_permission("fleet.view"))],
)
async def get_topology(
    resource_id: str,
    depth: int = Query(2, ge=1, le=MAX_TOPOLOGY_DEPTH),
    lifecycle_state: LifecycleFilter = "active",
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    """The primary-containment subtree under a resource that the caller
    may read, breadth first, to `depth` levels (A32.8). A hidden resource is
    not shown, and neither is anything under it. `truncated` is true when
    more than `limit` visible resources would have been returned -- it
    counts nothing the caller could not list for themselves."""
    reach = read_reach(scope, "fleet.view")
    repo = ResourceRepo(session)
    row = await repo.get_visible(user.tenant_id, resource_id, scope=reach)
    if row is None:
        raise HTTPException(status_code=404, detail="resource not found")
    nodes, truncated = await repo.subtree_visible(
        user.tenant_id,
        row.id,
        scope=reach,
        depth=depth,
        lifecycle_state=_lifecycle(lifecycle_state),
        max_nodes=MAX_TOPOLOGY_NODES,
    )
    return {
        "root": _summary(row),
        "depth": depth,
        "nodes": [
            {
                **_summary(node),
                "level": level,
                # Always the root or a node above in this list: the walk
                # only ever descends through what the caller may read.
                "parent_id": edge.from_resource_id,
                "relationship": _edge_summary(edge),
            }
            for edge, node, level in nodes
        ],
        "limit": MAX_TOPOLOGY_NODES,
        "truncated": truncated,
    }
