"""Resource Catalogue API: three reads (TX-1, spec A31.6).

Human-only (absent from `MACHINE_SURFACE`), `fleet.view`, `READ_SCOPED`.
Every read filters through `repos.scope_resources`, which derives a
resource's visibility from its CANONICAL legacy object -- the Sites and
Fleet predicates -- and from nothing in the catalogue. A hidden resource
reads exactly as a nonexistent one: 404, or absent from a list and from
its count. Nothing here grants, implies or describes authority.
"""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_cc.api.deps import get_scope, get_session, require_permission
from harkeniq_cc.auth import UserContext
from harkeniq_cc.db.repos import ResourceRepo
from harkeniq_cc.scope import read_reach

router = APIRouter(prefix="/api/resources", tags=["resources"])

#: The closed `resource_kind` vocabulary, as a query type. A test holds it
#: equal to `resource_catalogue.RESOURCE_KINDS`.
ResourceKindFilter = Literal[
    "component", "logical_resource", "physical_container", "physical_device",
]


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


def _resource_dict(row) -> dict:
    out = _summary(row)
    out.update({
        "source": row.source,
        # The canonical object this resource mirrors -- an id the reader
        # can already see through Sites or Fleet. `legacy_site_id` is an
        # internal anchor for tombstone visibility and is not published.
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
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    """The resources the caller may read, paginated.

    `source_system` + `namespace` + `external_id` together look a resource
    up by an identifier; a hidden resource's identifier finds nothing.
    """
    lookup = (source_system, namespace, external_id)
    if any(lookup) and not all(lookup):
        raise HTTPException(
            status_code=422,
            detail="an external-id lookup names source_system, namespace "
                   "and external_id together",
        )
    reach = read_reach(scope, "fleet.view")
    rows, total = await ResourceRepo(session).list_visible(
        user.tenant_id,
        scope=reach,
        resource_kind=kind,
        resource_type=resource_type,
        lifecycle_state=None if lifecycle_state == "any" else lifecycle_state,
        external=lookup if all(lookup) else None,
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
    when the caller may read the parent too; otherwise it reads as none."""
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
        }
        for e in await repo.external_ids(user.tenant_id, row.id)
    ]
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
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    """The typed edges touching one resource whose other end the caller may
    also read. `outgoing`: "<this> TYPE <other>"; `incoming`: the reverse.
    A relationship is a description: it neither shows a hidden resource
    nor confers anything over a visible one."""
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
        page=page,
        page_size=page_size,
    )
    return {
        "resource_id": row.id,
        "relationships": [
            {
                "id": edge.id,
                "relationship_type": edge.relationship_type,
                "direction": way,
                "is_primary": bool(edge.is_primary),
                "source": edge.source,
                "resource": _summary(other),
            }
            for edge, other, way in edges
        ],
        "page": page,
        "page_size": page_size,
        "total": total,
    }
