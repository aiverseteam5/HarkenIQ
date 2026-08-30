"""Scoped authorization: one resolver, for humans and for agents.

E1.2 (2026-08-30). Central Command had exactly one authorization
question -- does this role hold this permission -- and no answer at all
to "over which objects". This module is that second answer.

The model
--------
::

    principal -> grant(s) -> permission subset -> scope refs
              -> resolved authorization -> target-object check

Two questions, deliberately different, asked in different places:

* **"Could this actor ever possess this permission?"** -- the route
  guard, unchanged, answered from the role. Cheap fail-fast.
* **"Does this actor possess this permission over THIS target?"** --
  the repository read filter and the object gate, answered here.

Why they cannot be the same question
------------------------------------
``permission_subset`` is **per grant**. A principal may hold
``site.manage`` over Cluster A1 and read-only over Region B, so there is
no single set of permissions that is true everywhere -- which is why
nothing in this module ever flattens grants into one unconditional
authority set. :meth:`ResolvedScope.may_ever` exists for the fail-fast
and for rendering a UI, and it is **never** consulted by
:meth:`ResolvedScope.permits`.

Invariants this module is responsible for
-----------------------------------------
* A subset only ever **narrows**: ``role_permissions & subset``. A
  subset naming a permission the role lacks grants nothing.
* Grants **do not union into broader authority**. Coverage and
  permission are checked together, on the same grant.
* Ancestors are **visible, never authoritative**.
  ``contextual_unit_ids`` is a separate field and no decision method
  reads it.
* Revoked and expired grants resolve to nothing.
* Humans and agents run through this one resolver. What differs is what
  they are authorized to *do* (role permissions vs A0 capability
  bindings), never *where*.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

from harkeniq_cc.org_tree import ancestor_ids, is_descendant

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

SCOPE_TENANT = "tenant"
SCOPE_ORG_UNIT = "org_unit"
SCOPE_SITE = "site"
SCOPE_DEVICE_CLASS = "device_class"
SCOPE_DEVICE = "device"

#: Five types. `site`, `device_class` and `device` come from A0's
#: `cc_agent_scopes` and are kept EXACTLY as they were -- dropping them
#: in the merge would take reach away from every shipped agent.
SCOPE_TYPES = (
    SCOPE_TENANT,
    SCOPE_ORG_UNIT,
    SCOPE_SITE,
    SCOPE_DEVICE_CLASS,
    SCOPE_DEVICE,
)

PRINCIPAL_USER = "user"
PRINCIPAL_AGENT = "agent"
PRINCIPAL_TYPES = (PRINCIPAL_USER, PRINCIPAL_AGENT)

#: Per-tenant enforcement posture.
ENFORCEMENT_LEGACY_OPEN = "legacy_open"
ENFORCEMENT_STRICT = "strict"
ENFORCEMENT_MODES = (ENFORCEMENT_LEGACY_OPEN, ENFORCEMENT_STRICT)

#: The permission an L1 preflight requires somebody to hold at tenant
#: scope before a tenant may be switched to strict.
ADMIN_PERMISSION = "role.manage"


class ScopeError(ValueError):
    """A scope rule was violated. Routers map this to 4xx."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    """sqlite hands back naive datetimes for tz-aware writes."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def is_active(grant: Any, *, now: Optional[datetime] = None) -> bool:
    """A grant counts only while it is neither revoked nor expired."""
    now = now or _utcnow()
    if _aware(getattr(grant, "revoked_at", None)) is not None:
        return False
    expires = _aware(getattr(grant, "expires_at", None))
    return expires is None or expires > now


# ---------------------------------------------------------------------------
# One resolved grant
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Grant:
    """One scope row with its effective permissions already narrowed.

    `permissions` is ``role_permissions & permission_subset`` -- the
    intersection is computed once, here, so no caller can accidentally
    apply the subset as a union.
    """

    scope_type: str
    scope_ref: str
    permissions: frozenset[str]
    #: For an org_unit grant: the unit's materialized path, so coverage
    #: is the same prefix match E1.1 proved cross-engine.
    org_unit_path: str = ""
    #: True for the tenant-wide grant `legacy_open` SYNTHESIZES for a
    #: principal who has none. It authorizes exactly as a real grant
    #: does -- that is what "behaviour is unchanged on upgrade" means --
    #: but it is not evidence that anybody was deliberately made an
    #: administrator, so the L1 preflight refuses to count it. Without
    #: this flag, flipping a grantless tenant to strict would pass the
    #: preflight and lock every principal out, which is the exact
    #: failure L1 exists to prevent.
    synthesized: bool = False

    def covers_site(self, site_id: str, site_unit_path: str) -> bool:
        if self.scope_type == SCOPE_TENANT:
            return True
        if self.scope_type == SCOPE_SITE:
            return bool(site_id) and self.scope_ref == site_id
        if self.scope_type == SCOPE_ORG_UNIT:
            return bool(site_unit_path) and is_descendant(
                site_unit_path, self.org_unit_path
            )
        # device_class and device say nothing about a whole site.
        return False

    def covers_org_unit(self, unit_path: str) -> bool:
        if self.scope_type == SCOPE_TENANT:
            return True
        if self.scope_type == SCOPE_ORG_UNIT:
            return bool(unit_path) and is_descendant(unit_path, self.org_unit_path)
        return False

    def covers_device(
        self, agent_id: str, site_id: str, site_unit_path: str, device_class: str
    ) -> bool:
        if self.scope_type == SCOPE_DEVICE:
            return bool(agent_id) and self.scope_ref == agent_id
        if self.scope_type == SCOPE_DEVICE_CLASS:
            return bool(device_class) and self.scope_ref.lower() == device_class.lower()
        return self.covers_site(site_id, site_unit_path)

    def covers_tenant(self) -> bool:
        """Only a tenant grant reaches a tenant-wide object."""
        return self.scope_type == SCOPE_TENANT


# ---------------------------------------------------------------------------
# The resolved scope
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedScope:
    """Everything one principal may reach, and with what, where.

    Constructed by :func:`resolve`. The convenience sets
    (`site_ids`, `org_unit_paths`, ...) exist so a repository can build
    one ``IN`` clause instead of N round trips; **authority still comes
    from the grants**, because a set cannot say which permission applied
    where.
    """

    tenant_id: str
    principal_type: str
    principal_ref: str
    enforcement: str
    grants: tuple[Grant, ...] = ()

    #: Convenience projections for read filtering.
    tenant_wide: bool = False
    site_ids: frozenset[str] = frozenset()
    org_unit_paths: frozenset[str] = frozenset()
    device_classes: frozenset[str] = frozenset()
    device_ids: frozenset[str] = frozenset()

    #: Ancestors of the authority paths. VISIBLE FOR BREADCRUMBS ONLY.
    #: No method below reads this. Ratified decision L3: "can see the
    #: ancestor for context" and "can act across the ancestor" are
    #: different things, and keeping them in different fields is what
    #: makes confusing them a deletion rather than an oversight.
    contextual_unit_ids: frozenset[str] = frozenset()

    #: site_id -> the org-unit path that site hangs from.
    site_unit_paths: Mapping[str, str] = field(default_factory=dict)
    #: org unit id -> its materialized path. Lets a caller ask about a
    #: unit by id without re-reading the tree.
    unit_paths: Mapping[str, str] = field(default_factory=dict)

    # -- the decision -------------------------------------------------

    def permits(
        self,
        permission: str,
        *,
        site_id: str = "",
        org_unit_path: str = "",
        device_agent_id: str = "",
        device_class: str = "",
        tenant_object: bool = False,
    ) -> bool:
        """Does this actor hold `permission` over THIS target?

        Coverage and permission are checked on the **same grant**, which
        is what stops two narrow grants from adding up to a broad one: a
        grant covering the target but lacking the permission does not
        borrow it from a grant that has it elsewhere.
        """
        for grant in self.grants:
            if permission not in grant.permissions and "*" not in grant.permissions:
                continue
            if tenant_object:
                if grant.covers_tenant():
                    return True
                continue
            if device_agent_id or device_class:
                if grant.covers_device(
                    device_agent_id,
                    site_id,
                    self._unit_path_for(site_id, org_unit_path),
                    device_class,
                ):
                    return True
                continue
            if org_unit_path:
                if grant.covers_org_unit(org_unit_path):
                    return True
                continue
            if site_id:
                if grant.covers_site(
                    site_id, self._unit_path_for(site_id, org_unit_path)
                ):
                    return True
                continue
            # No target named: a tenant-wide question.
            if grant.covers_tenant():
                return True
        return False

    def may_ever(self, permission: str) -> bool:
        """Could this actor hold `permission` ANYWHERE?

        The fail-fast, and what a UI may use to decide whether to render
        a control. **Never** an authorization decision on its own:
        `permits` is the only thing that decides.
        """
        return any(
            permission in g.permissions or "*" in g.permissions for g in self.grants
        )

    # -- coverage, without a permission --------------------------------

    def covers_site(self, site_id: str, org_unit_path: str = "") -> bool:
        path = self._unit_path_for(site_id, org_unit_path)
        return any(g.covers_site(site_id, path) for g in self.grants)

    def covers_device(
        self, agent_id: str, site_id: str = "", device_class: str = ""
    ) -> bool:
        path = self._unit_path_for(site_id, "")
        return any(
            g.covers_device(agent_id, site_id, path, device_class) for g in self.grants
        )

    def covers_org_unit(self, unit_path: str) -> bool:
        return any(g.covers_org_unit(unit_path) for g in self.grants)

    def covers_org_unit_id(self, unit_id: str) -> bool:
        """Same question, by id. Unknown id reaches nothing."""
        path = self.unit_paths.get(unit_id, "")
        return bool(path) and self.covers_org_unit(path)

    def is_empty(self) -> bool:
        return not self.grants

    def _unit_path_for(self, site_id: str, given: str) -> str:
        return given or self.site_unit_paths.get(site_id, "")

    # -- delegation ----------------------------------------------------

    def can_delegate(self, other: "ResolvedScope") -> bool:
        """Is every grant in `other` inside this scope?

        "Delegated administration cannot exceed the delegator's
        authority", as arithmetic rather than as review.
        """
        for grant in other.grants:
            if not self._covers_grant(grant):
                return False
        return True

    def _covers_grant(self, grant: Grant) -> bool:
        for mine in self.grants:
            if mine.scope_type == SCOPE_TENANT:
                return True
            if grant.scope_type == SCOPE_TENANT:
                continue
            if grant.scope_type == SCOPE_ORG_UNIT:
                path = grant.org_unit_path or self.unit_paths.get(
                    grant.scope_ref, ""
                )
                if path and mine.covers_org_unit(path):
                    return True
            elif grant.scope_type == SCOPE_SITE:
                if mine.covers_site(
                    grant.scope_ref, self.site_unit_paths.get(grant.scope_ref, "")
                ):
                    return True
            elif grant.scope_type == SCOPE_DEVICE:
                if mine.scope_type == SCOPE_DEVICE and mine.scope_ref == grant.scope_ref:
                    return True
            elif grant.scope_type == SCOPE_DEVICE_CLASS:
                if (
                    mine.scope_type == SCOPE_DEVICE_CLASS
                    and mine.scope_ref.lower() == grant.scope_ref.lower()
                ):
                    return True
        return False


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def effective_permissions(
    role_permissions: Iterable[str], subset: Optional[Sequence[str]]
) -> frozenset[str]:
    """``role & subset``. Never a union.

    A subset naming a permission the role does not hold grants nothing,
    so a delegator cannot hand out authority they never had. ``"*"`` in
    the role means every permission, so the subset passes through as-is.
    """
    role = set(role_permissions)
    if subset is None:
        return frozenset(role)
    requested = {p for p in subset if p}
    if not requested:
        # An empty list is a real statement: "this grant carries no
        # permissions". It is not the same as null.
        return frozenset()
    if "*" in role:
        return frozenset(requested)
    return frozenset(role & requested)


def resolve(
    *,
    tenant_id: str,
    principal_type: str,
    principal_ref: str,
    role_permissions: Iterable[str],
    grant_rows: Iterable[Any],
    org_units: Iterable[Any] = (),
    sites: Iterable[Any] = (),
    enforcement: str = ENFORCEMENT_LEGACY_OPEN,
    now: Optional[datetime] = None,
) -> ResolvedScope:
    """Build a :class:`ResolvedScope` from rows. Pure.

    `grant_rows` are ``cc_scope_grants`` records; `org_units` and
    `sites` provide the tree and the site->unit attachment the org_unit
    scope type needs. Nothing here reads a request, a session or a tree
    response.
    """
    role_permissions = list(role_permissions)
    unit_by_id = {u.id: u for u in org_units}
    site_unit_paths: dict[str, str] = {}
    for site in sites:
        unit = unit_by_id.get(getattr(site, "org_unit_id", None) or "")
        site_unit_paths[site.id] = unit.path if unit is not None else ""

    active = [g for g in grant_rows if is_active(g, now=now)]

    grants: list[Grant] = []
    for row in active:
        scope_type = (row.scope_type or "").strip()
        if scope_type not in SCOPE_TYPES:
            # An unknown scope type grants nothing. Fail closed rather
            # than guess what a future type meant.
            continue
        permissions = effective_permissions(
            role_permissions, getattr(row, "permission_subset", None)
        )
        if not permissions:
            continue
        path = ""
        if scope_type == SCOPE_ORG_UNIT:
            unit = unit_by_id.get(row.scope_ref or "")
            if unit is None:
                # A grant pointing at a unit that no longer exists
                # reaches nothing. It is not an error and not a wildcard.
                continue
            path = unit.path
        grants.append(
            Grant(
                scope_type=scope_type,
                scope_ref=row.scope_ref or "",
                permissions=permissions,
                org_unit_path=path,
            )
        )

    if not grants and enforcement == ENFORCEMENT_LEGACY_OPEN:
        # Upgrade behaviour: a principal with no grants keeps exactly the
        # tenant-wide reach they have today. Central Command cannot
        # enumerate a realm's principals to backfill grants, so it must
        # not pretend the absence of a grant is a decision.
        grants.append(
            Grant(
                scope_type=SCOPE_TENANT,
                scope_ref="",
                permissions=frozenset(role_permissions),
                synthesized=True,
            )
        )

    return _project(
        tenant_id=tenant_id,
        principal_type=principal_type,
        principal_ref=principal_ref,
        enforcement=enforcement,
        grants=grants,
        unit_by_id=unit_by_id,
        sites=list(sites),
        site_unit_paths=site_unit_paths,
    )


def _project(
    *,
    tenant_id: str,
    principal_type: str,
    principal_ref: str,
    enforcement: str,
    grants: Sequence[Grant],
    unit_by_id: Mapping[str, Any],
    sites: Sequence[Any],
    site_unit_paths: Mapping[str, str],
) -> ResolvedScope:
    tenant_wide = any(g.scope_type == SCOPE_TENANT for g in grants)
    org_paths = {g.org_unit_path for g in grants if g.scope_type == SCOPE_ORG_UNIT}
    device_classes = {
        g.scope_ref.lower() for g in grants if g.scope_type == SCOPE_DEVICE_CLASS
    }
    device_ids = {g.scope_ref for g in grants if g.scope_type == SCOPE_DEVICE}

    site_ids = {g.scope_ref for g in grants if g.scope_type == SCOPE_SITE}
    if tenant_wide:
        site_ids |= {s.id for s in sites}
    else:
        for site in sites:
            path = site_unit_paths.get(site.id, "")
            if path and any(is_descendant(path, p) for p in org_paths):
                site_ids.add(site.id)

    # Ancestors of every authority path, for breadcrumbs. Never
    # authority: no method on ResolvedScope reads this field.
    contextual: set[str] = set()
    for path in org_paths:
        contextual.update(ancestor_ids(path))
    contextual -= {
        unit.id for unit in unit_by_id.values() if unit.path in org_paths
    }

    return ResolvedScope(
        tenant_id=tenant_id,
        principal_type=principal_type,
        principal_ref=principal_ref,
        enforcement=enforcement,
        grants=tuple(grants),
        tenant_wide=tenant_wide,
        site_ids=frozenset(site_ids),
        org_unit_paths=frozenset(org_paths),
        device_classes=frozenset(device_classes),
        device_ids=frozenset(device_ids),
        contextual_unit_ids=frozenset(contextual),
        site_unit_paths=dict(site_unit_paths),
        unit_paths={u.id: u.path for u in unit_by_id.values()},
    )


def empty_scope(tenant_id: str, principal_ref: str = "") -> ResolvedScope:
    """A scope that reaches nothing. What strict mode gives an ungranted
    principal, and the only safe default anywhere else."""
    return ResolvedScope(
        tenant_id=tenant_id,
        principal_type=PRINCIPAL_USER,
        principal_ref=principal_ref,
        enforcement=ENFORCEMENT_STRICT,
    )


# ---------------------------------------------------------------------------
# L1: the strict-mode preflight
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    reason: str = ""
    admin_count: int = 0


def preflight_strict(
    grant_rows: Iterable[Any],
    role_permissions_for: Any,
    *,
    caller_scope: Optional[ResolvedScope] = None,
    now: Optional[datetime] = None,
) -> PreflightResult:
    """May this tenant be switched to strict without locking itself out?

    Ratified L1: the flip is allowed only if at least one **active,
    unexpired** principal holds a **tenant-scope** grant whose effective
    permissions contain ``role.manage``.

    Deliberately NOT in the resolver. A resolver that knew about
    administrators would carry a special case into every future caller;
    this is a question the endpoint asks once, at the moment of the flip.
    """
    admins = 0

    # The caller counts when they hold a REAL tenant grant carrying
    # role.manage. Their token is authoritative about their role, which
    # a stored `role` on somebody else's grant can never be -- and
    # without this a tenant whose grants omit `role` could never flip at
    # all. A SYNTHESIZED grant never counts: under legacy_open every
    # principal has one, so counting it would let a grantless tenant
    # flip to strict and lock itself out completely.
    counted_caller = False
    if caller_scope is not None:
        for grant in caller_scope.grants:
            if grant.synthesized or grant.scope_type != SCOPE_TENANT:
                continue
            if ADMIN_PERMISSION in grant.permissions or "*" in grant.permissions:
                admins += 1
                counted_caller = True
                break

    for row in grant_rows:
        if row.scope_type != SCOPE_TENANT:
            continue
        if counted_caller and (
            getattr(row, "principal_ref", None) == caller_scope.principal_ref
        ):
            # Already counted through the caller's own resolved scope.
            continue
        if not is_active(row, now=now):
            continue
        permissions = effective_permissions(
            role_permissions_for(row), getattr(row, "permission_subset", None)
        )
        if ADMIN_PERMISSION in permissions or "*" in permissions:
            admins += 1

    if admins:
        return PreflightResult(ok=True, admin_count=admins)
    return PreflightResult(
        ok=False,
        admin_count=0,
        reason=(
            "refusing to enable strict scope enforcement: no active, unexpired "
            f"principal holds a tenant-scope grant containing {ADMIN_PERMISSION!r}. "
            "Grant one first, or the tenant would have no administrator able to "
            "grant scope afterwards. Nothing has been changed."
        ),
    )
