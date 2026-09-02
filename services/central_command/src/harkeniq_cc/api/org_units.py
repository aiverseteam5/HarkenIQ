"""Organizational tree API (E1.1, 2026-08-30).

The tenant's own organization: regions, clusters, circles, trusts,
territories -- whatever the customer calls its levels -- with each site
attached to exactly one node.

Governance
----------
Reads need `site.view`, mutations need `site.manage`. No new permission,
because the vocabulary is fixed (spec §4) and an org tree is site
administration.

**This router has no authorization effect.** Creating a unit grants
nobody anything; moving a site between units changes who owns it on
paper and changes nothing about who may act on it. Authorization
arrives at E1.2 as scope grants that happen to reference these units.
Ratified decision B keeps the two separate on purpose: an org chart
edit must never be a privilege change.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_cc.api.deps import forbid_out_of_scope, get_scope, get_session, require_permission
from harkeniq_cc.actor import actor_of
from harkeniq_cc.auth import UserContext
from harkeniq_cc.db.repos import AuditRepo, OrgUnitRepo
from harkeniq_cc.grant_integrity import (
    GrantIntegrityError,
    refuse_unit_delete_under_grants,
)
from harkeniq_cc.org_tree import (
    MAX_DEPTH,
    flatten,
    OrgTreeError,
    ancestor_ids,
    assemble_tree,
    check_depth,
    check_move,
    normalize_name,
    normalize_unit_type,
    total_site_count,
)

logger = logging.getLogger("harkeniq.cc.api.org_units")

router = APIRouter(prefix="/api/org-units", tags=["org-units"])


# ---------------------------------------------------------------------------
# Bodies
# ---------------------------------------------------------------------------


class OrgUnitCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    unit_type: str = Field("region", max_length=32)
    parent_id: Optional[str] = Field(
        None, description="omit to create a root unit"
    )
    sort_order: int = 0


class OrgUnitUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    unit_type: Optional[str] = Field(None, max_length=32)
    sort_order: Optional[int] = None
    #: Present-and-null means "make this a root"; absent means "leave the
    #: parent alone". Pydantic cannot tell those apart on its own, so the
    #: handler reads `model_fields_set`.
    parent_id: Optional[str] = None


class SiteAttach(BaseModel):
    org_unit_id: str = Field(..., min_length=1, max_length=32)


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _unit_dict(unit, *, site_count: int = 0) -> dict:
    return {
        "id": unit.id,
        "parent_id": unit.parent_id,
        "unit_type": unit.unit_type,
        "name": unit.name,
        "path": unit.path,
        "depth": unit.depth,
        "sort_order": unit.sort_order,
        "site_count": site_count,
        "created_by": unit.created_by,
        "created_at": unit.created_at.isoformat() if unit.created_at else None,
        "updated_at": unit.updated_at.isoformat() if unit.updated_at else None,
    }


def _bad(exc: OrgTreeError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def _visible_units(units, scope) -> tuple[list, set[str]]:
    """Split the tenant's units into what this caller may see (L3).

    Returns (visible units, ids that are contextual-only). A tenant-wide
    caller sees everything and nothing is contextual.

    A unit is AUTHORITATIVE when a grant covers it -- that is, when its
    path sits at or below one of the caller's granted paths, or the
    caller holds the site beneath it. It is CONTEXTUAL when it is merely
    an ancestor of something authoritative: needed to render a
    breadcrumb, and never a thing to act on.

    Note what is deliberately absent: a contextual ancestor's OTHER
    children. Showing them would leak the sibling branches L3 requires
    to be invisible.
    """
    if scope is None or getattr(scope, "tenant_wide", False):
        return list(units), set()

    by_id = {u.id: u for u in units}
    authoritative: set[str] = set()
    for unit in units:
        if scope.covers_org_unit(unit.path):
            authoritative.add(unit.id)
            continue
        # A site-scoped principal (no org grant) still needs to see the
        # unit their site hangs from, or the tree renders empty for them.
        for site_id in scope.site_ids:
            if scope.site_unit_paths.get(site_id, "") == unit.path:
                authoritative.add(unit.id)
                break

    contextual: set[str] = set()
    for unit_id in authoritative:
        for ancestor in ancestor_ids(by_id[unit_id].path):
            if ancestor not in authoritative:
                contextual.add(ancestor)

    visible_ids = authoritative | contextual
    return [u for u in units if u.id in visible_ids], contextual


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


@router.get("/")
async def list_org_units(
    user: UserContext = Depends(require_permission("site.view")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    """The tenant's tree, nested, with per-node and rolled-up site counts.

    Ratified L3: a scoped caller sees their reachable subtree in full,
    plus the minimal ancestor chain needed to navigate to it. Sibling
    and unrelated branches are absent entirely.

    **Ancestor visibility is contextual only and confers no authority.**
    Each ancestor comes back marked `contextual: true, authority: false`
    and carries none of its other children, so "can see the ancestor for
    context" and "can act across the ancestor" stay different things --
    a distinction the object gate enforces independently, because
    `ResolvedScope.permits` never reads `contextual_unit_ids`.
    """
    repo = OrgUnitRepo(session)
    units = await repo.list_all(user.tenant_id)
    counts = await repo.site_counts(user.tenant_id)

    visible, contextual_ids = _visible_units(units, scope)
    roots = assemble_tree(visible, site_counts=counts)
    for node in flatten(roots):
        node["contextual"] = node["id"] in contextual_ids
        node["authority"] = node["id"] not in contextual_ids
    for root in roots:
        root["subtree_site_count"] = total_site_count(root)
    return {
        "tenant_id": user.tenant_id,
        "max_depth": MAX_DEPTH,
        "unit_count": len(visible),
        "tenant_wide": bool(getattr(scope, "tenant_wide", False)),
        "tree": roots,
    }


@router.get("/{unit_id}")
async def get_org_unit(
    unit_id: str,
    user: UserContext = Depends(require_permission("site.view")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    repo = OrgUnitRepo(session)
    unit = await repo.get(user.tenant_id, unit_id)
    if unit is None:
        raise HTTPException(status_code=404, detail="org unit not found")

    # E1.2: a unit the caller can neither act on nor see as an ancestor
    # reads as absent. A contextual ancestor IS readable -- that is what
    # makes a breadcrumb work -- and carries `authority: false`.
    contextual = unit.id in getattr(scope, "contextual_unit_ids", frozenset())
    if not scope.covers_org_unit(unit.path) and not contextual:
        raise HTTPException(status_code=404, detail="org unit not found")

    counts = await repo.site_counts(user.tenant_id)
    ancestors = {
        row.id: row
        for row in await repo.list_by_ids(user.tenant_id, ancestor_ids(unit.path))
    }
    children = [
        row
        for row in await repo.list_subtree(user.tenant_id, unit.path)
        if row.parent_id == unit.id
    ]
    sites = await repo.sites_in(user.tenant_id, unit.id)

    return {
        "unit": {
            **_unit_dict(unit, site_count=counts.get(unit.id, 0)),
            "contextual": contextual,
            "authority": not contextual,
        },
        # Root first: this is the breadcrumb.
        "ancestors": [
            _unit_dict(ancestors[uid], site_count=counts.get(uid, 0))
            for uid in ancestor_ids(unit.path)
            if uid in ancestors
        ],
        "children": [
            _unit_dict(row, site_count=counts.get(row.id, 0)) for row in children
        ],
        "sites": [
            {"id": s.id, "site_name": s.site_name, "status": s.status} for s in sites
        ],
        "subtree_site_count": await repo.site_count_in_subtree(
            user.tenant_id, unit.path
        ),
    }


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


@router.post("/", status_code=201)
async def create_org_unit(
    body: OrgUnitCreate,
    user: UserContext = Depends(require_permission("site.manage")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    repo = OrgUnitRepo(session)
    try:
        name = normalize_name(body.name)
        unit_type = normalize_unit_type(body.unit_type)
    except OrgTreeError as exc:
        raise _bad(exc)

    parent = None
    if body.parent_id:
        parent = await repo.get(user.tenant_id, body.parent_id)
        if parent is None:
            raise HTTPException(status_code=404, detail="parent org unit not found")
        # E1.2 layer 3. Seeing a parent as a breadcrumb is not authority
        # to hang new units off it.
        forbid_out_of_scope(
            scope, "site.manage",
            what=f"org unit {parent.name!r}", org_unit_path=parent.path,
        )
        try:
            check_depth(parent.depth)
        except OrgTreeError as exc:
            raise _bad(exc)
    else:
        # A new ROOT unit restructures the tenant itself.
        forbid_out_of_scope(
            scope, "site.manage",
            what="creating a root organizational unit", tenant_object=True,
        )

    if await repo.sibling_named(user.tenant_id, body.parent_id or None, name):
        raise HTTPException(
            status_code=409,
            detail=f"a sibling unit named {name!r} already exists here",
        )

    unit = await repo.create(
        user.tenant_id,
        name=name,
        unit_type=unit_type,
        parent=parent,
        sort_order=body.sort_order,
        created_by=user.user_id,
    )
    await AuditRepo(session).append(
        actor=user.user_id, actor_ref=actor_of(user),
        action="org_unit.created",
        subject=unit.id,
        tenant_id=user.tenant_id,
        detail={
            "name": unit.name,
            "unit_type": unit.unit_type,
            "parent_id": unit.parent_id,
            "path": unit.path,
            "depth": unit.depth,
        },
    )
    await session.commit()
    return _unit_dict(unit)


@router.patch("/{unit_id}")
async def update_org_unit(
    unit_id: str,
    body: OrgUnitUpdate,
    user: UserContext = Depends(require_permission("site.manage")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    repo = OrgUnitRepo(session)
    unit = await repo.get(user.tenant_id, unit_id)
    if unit is None:
        raise HTTPException(status_code=404, detail="org unit not found")
    forbid_out_of_scope(
        scope, "site.manage",
        what=f"org unit {unit.name!r}", org_unit_path=unit.path,
    )

    changes: dict = {}

    if body.name is not None:
        try:
            name = normalize_name(body.name)
        except OrgTreeError as exc:
            raise _bad(exc)
        if name != unit.name:
            if await repo.sibling_named(
                user.tenant_id, unit.parent_id, name, exclude_id=unit.id
            ):
                raise HTTPException(
                    status_code=409,
                    detail=f"a sibling unit named {name!r} already exists here",
                )
            changes["name"] = [unit.name, name]
            unit.name = name

    if body.unit_type is not None:
        try:
            unit_type = normalize_unit_type(body.unit_type)
        except OrgTreeError as exc:
            raise _bad(exc)
        if unit_type != unit.unit_type:
            changes["unit_type"] = [unit.unit_type, unit_type]
            unit.unit_type = unit_type

    if body.sort_order is not None and body.sort_order != unit.sort_order:
        changes["sort_order"] = [unit.sort_order, body.sort_order]
        unit.sort_order = body.sort_order

    # Re-parent. `parent_id: null` in the body means "promote to root";
    # an absent key means "do not touch the parent".
    if "parent_id" in body.model_fields_set and body.parent_id != unit.parent_id:
        new_parent = None
        if body.parent_id:
            new_parent = await repo.get(user.tenant_id, body.parent_id)
            if new_parent is None:
                raise HTTPException(
                    status_code=404, detail="destination parent not found"
                )
            # BOTH ends. Without the destination check a cluster manager
            # could move their own subtree under a branch they do not
            # administer; without the source check (above) they could
            # move somebody else's.
            forbid_out_of_scope(
                scope, "site.manage",
                what=f"destination org unit {new_parent.name!r}",
                org_unit_path=new_parent.path,
            )
        else:
            forbid_out_of_scope(
                scope, "site.manage",
                what="promoting a unit to a tenant root", tenant_object=True,
            )
        try:
            check_move(
                unit.path,
                unit.id,
                new_parent.path if new_parent else None,
                new_parent.depth if new_parent else 0,
                await repo.subtree_height(user.tenant_id, unit.path),
            )
        except OrgTreeError as exc:
            raise _bad(exc)

        if await repo.sibling_named(
            user.tenant_id,
            new_parent.id if new_parent else None,
            unit.name,
            exclude_id=unit.id,
        ):
            raise HTTPException(
                status_code=409,
                detail=f"a unit named {unit.name!r} already exists at the destination",
            )

        old_path, new_path = await repo.move(
            user.tenant_id, unit, new_parent, actor=user.user_id
        )
        changes["parent_id"] = [
            old_path, new_path,
        ]

    if not changes:
        return _unit_dict(unit)

    unit.updated_by = user.user_id
    await AuditRepo(session).append(
        actor=user.user_id, actor_ref=actor_of(user),
        action="org_unit.moved" if "parent_id" in changes else "org_unit.updated",
        subject=unit.id,
        tenant_id=user.tenant_id,
        detail={"changes": changes, "path": unit.path, "depth": unit.depth},
    )
    await session.commit()
    return _unit_dict(unit)


@router.delete("/{unit_id}")
async def delete_org_unit(
    unit_id: str,
    user: UserContext = Depends(require_permission("site.manage")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    """Refuse to delete a unit that still holds children or sites.

    Cascading would orphan sites silently -- a site with no
    organizational path is a site nobody owns, and at E1.2 it becomes a
    site nobody can be granted. The operator moves the contents first.
    """
    repo = OrgUnitRepo(session)
    unit = await repo.get(user.tenant_id, unit_id)
    if unit is None:
        raise HTTPException(status_code=404, detail="org unit not found")
    forbid_out_of_scope(
        scope, "site.manage",
        what=f"org unit {unit.name!r}", org_unit_path=unit.path,
    )

    children = await repo.child_count(user.tenant_id, unit.id)
    sites = await repo.sites_in(user.tenant_id, unit.id)
    if children or sites:
        raise HTTPException(
            status_code=409,
            detail=(
                f"unit still holds {children} child unit(s) and {len(sites)} "
                "site(s); move them before deleting"
            ),
        )

    # A23.9: a unit is not deleted from under an active grant. The
    # refusal is a security event -- somebody tried to make a grant's
    # target vanish -- and is recorded on the chain with the grants it
    # named, so the operator can reassign or revoke them.
    try:
        await refuse_unit_delete_under_grants(session, user.tenant_id, unit)
    except GrantIntegrityError as exc:
        await AuditRepo(session).append(
            actor=user.user_id, actor_ref=actor_of(user),
            action=exc.audit or "org_unit.delete_refused",
            subject=unit.id,
            tenant_id=user.tenant_id,
            detail={"name": unit.name, "path": unit.path, **exc.detail},
        )
        await session.commit()
        raise HTTPException(status_code=exc.status, detail=exc.reason)

    detail = {
        "name": unit.name,
        "unit_type": unit.unit_type,
        "path": unit.path,
        "parent_id": unit.parent_id,
    }
    await repo.delete(unit)
    await AuditRepo(session).append(
        actor=user.user_id, actor_ref=actor_of(user),
        action="org_unit.deleted",
        subject=unit_id,
        tenant_id=user.tenant_id,
        detail=detail,
    )
    await session.commit()
    return {"deleted": unit_id}
