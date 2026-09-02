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

#: A5 (A22.13). A scope resolved to answer WHERE, never WHETHER.
#:
#: `load_agent_scope` used to resolve with ``role_permissions=["*"]`` and
#: say why: an agent's authority is its bindings, and it does not call the
#: HTTP API. A3 removed that premise -- an agent now holds a credential --
#: and a scope resolved that way answers ``permits("action.approve")`` with
#: True. It was latent only because every call site read `.site_ids`.
#:
#: The wildcard is gone rather than patched at the call sites. This marker
#: preserves the grant arithmetic EXACTLY (a grant survives resolution
#: whenever it survived under ``"*"``, so no agent loses reach) while
#: matching no real permission and not being ``"*"``, so a permission
#: question fails closed even if the louder guard below is bypassed.
SCOPE_ONLY_MARKER = "__scope_only__"

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
    #: A23.9. True when the grant's TARGET no longer exists (the org unit
    #: was deleted, the site is gone). The row is retained and it stays
    #: in the resolved list -- it is evidence that this principal was
    #: administered -- but it covers nothing and carries nothing. It is
    #: never dropped, because a dropped grant is an EMPTY list, and an
    #: empty list is what `legacy_open` synthesizes tenant-wide reach on.
    inert: bool = False
    #: Why: "org_unit_missing" | "site_missing".
    inert_reason: str = ""

    def covers_site(self, site_id: str, site_unit_path: str) -> bool:
        if self.inert:
            return False
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
        if self.inert:
            return False
        if self.scope_type == SCOPE_TENANT:
            return True
        if self.scope_type == SCOPE_ORG_UNIT:
            return bool(unit_path) and is_descendant(unit_path, self.org_unit_path)
        return False

    def covers_device(
        self, agent_id: str, site_id: str, site_unit_path: str, device_class: str
    ) -> bool:
        if self.inert:
            return False
        if self.scope_type == SCOPE_DEVICE:
            return bool(agent_id) and self.scope_ref == agent_id
        if self.scope_type == SCOPE_DEVICE_CLASS:
            return bool(device_class) and self.scope_ref.lower() == device_class.lower()
        return self.covers_site(site_id, site_unit_path)

    def covers_tenant(self) -> bool:
        """Only a tenant grant reaches a tenant-wide object."""
        return self.scope_type == SCOPE_TENANT and not self.inert


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
    #: A22.13: resolved for expansion only. `.site_ids` and the coverage
    #: helpers are meaningful; `permits()` is not, and says so loudly
    #: rather than answering a question this scope was never built for.
    scope_only: bool = False

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

        A scope resolved for EXPANSION ONLY refuses the question (A22.13).
        Answering False would be fail-closed and silent; a caller that
        asks a where-scope a whether-question has a bug, and it should
        surface as one rather than as a mysterious refusal.
        """
        if self.scope_only:
            raise ScopeError(
                "this scope was resolved to answer WHERE, not WHETHER; ask "
                "the principal's own authorization scope for a permission"
            )
        for grant in self.grants:
            if grant.inert:
                continue
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
            permission in g.permissions or "*" in g.permissions
            for g in self.grants if not g.inert
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
        """No EFFECTIVE grant. An inert grant reaches nothing, so a scope
        holding only inert grants is empty for every operational
        purpose -- and `administered` is how a caller tells the two
        apart."""
        return not any(not g.inert for g in self.grants)

    @property
    def administered(self) -> bool:
        """Was this principal ever deliberately granted anything that
        still stands as a row? True for inert grants too. False for a
        scope that holds only the synthesized `legacy_open` grant."""
        return any(not g.synthesized for g in self.grants)

    @property
    def inert_grants(self) -> tuple[Grant, ...]:
        return tuple(g for g in self.grants if g.inert)

    @property
    def effective_grants(self) -> tuple[Grant, ...]:
        return tuple(g for g in self.grants if not g.inert)

    def _unit_path_for(self, site_id: str, given: str) -> str:
        return given or self.site_unit_paths.get(site_id, "")

    # -- delegation ----------------------------------------------------

    def can_delegate(self, other: "ResolvedScope") -> bool:
        """Is every grant in `other` inside this scope?

        "Delegated administration cannot exceed the delegator's
        authority", as arithmetic rather than as review.
        """
        for grant in other.grants:
            if grant.inert:
                # A grant to a vanished target cannot be delegated: there
                # is nothing there to hand over.
                return False
            if not self._covers_grant(grant):
                return False
        return True

    def _covers_grant(self, grant: Grant) -> bool:
        for mine in self.grants:
            if mine.inert:
                continue
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
    if SCOPE_ONLY_MARKER in role:
        # Same survival rule as "*", carrying no permission out of it.
        return frozenset({SCOPE_ONLY_MARKER})
    if "*" in role:
        return frozenset(requested)
    return frozenset(role & requested)


def grant_permissions(
    row: Any,
    role_permissions: Iterable[str],
    role_ceiling: Optional[Iterable[str]] = None,
) -> frozenset[str]:
    """What ONE grant row carries: ``token role ∩ recorded role ∩ subset``.

    A23-3. The row records the role the grantor named (`role`), and
    until now the resolver never read it: the token's role was the only
    input, so a tenant owner narrowed to one site could hand out a
    "tenant_owner" grant there and the recipient's own token decided
    what that meant. The recorded role is now a CEILING the grantor
    asserted, applied with the same arithmetic as the subset -- it can
    only narrow. `role_ceiling=None` (no role recorded, or a name this
    Central Command does not know) leaves the row exactly as it was, so
    every grant made without a role behaves identically after upgrade.

    The two survival markers pass through: ``"*"`` in the token (a
    platform identity in the insecure demo) and `SCOPE_ONLY_MARKER` (an
    agent, whose authority is its bindings, not permissions).
    """
    permissions = effective_permissions(
        role_permissions, getattr(row, "permission_subset", None)
    )
    if role_ceiling is None or not permissions:
        return permissions
    ceiling = {p for p in role_ceiling if p}
    if SCOPE_ONLY_MARKER in permissions or "*" in ceiling:
        return permissions
    if "*" in permissions:
        return frozenset(ceiling)
    return frozenset(permissions & ceiling)


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
    role_ceiling_for: Optional[Any] = None,
) -> ResolvedScope:
    """Build a :class:`ResolvedScope` from rows. Pure.

    `grant_rows` are ``cc_scope_grants`` records; `org_units` and
    `sites` provide the tree and the site->unit attachment the org_unit
    scope type needs. Nothing here reads a request, a session or a tree
    response.

    `role_ceiling_for(row)` returns the permission list of the role the
    row RECORDS, or None when it recorded none (A23-3, see
    :func:`grant_permissions`). The loader supplies it from the role
    table; the resolver stays ignorant of role names.
    """
    role_permissions = list(role_permissions)
    unit_by_id = {u.id: u for u in org_units}
    site_list = list(sites)
    known_site_ids = {s.id for s in site_list}
    site_unit_paths: dict[str, str] = {}
    for site in site_list:
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
        permissions = grant_permissions(
            row,
            role_permissions,
            role_ceiling_for(row) if role_ceiling_for is not None else None,
        )
        if not permissions:
            continue
        path = ""
        inert_reason = ""
        ref = row.scope_ref or ""
        if scope_type == SCOPE_ORG_UNIT:
            unit = unit_by_id.get(ref)
            if unit is None:
                # A23.9: the unit is gone. The grant is RETAINED as
                # inert -- reach none, reason stated -- rather than
                # dropped, because dropping it produces the empty list
                # `legacy_open` synthesizes tenant-wide reach on. A
                # vanished target never widens.
                inert_reason = "org_unit_missing"
            else:
                path = unit.path
        elif scope_type == SCOPE_SITE and ref not in known_site_ids:
            # Same rule for a site that is no longer in the tenant's
            # current site set: `covers_site` must not say yes to an id
            # that names nothing.
            inert_reason = "site_missing"
        grants.append(
            Grant(
                scope_type=scope_type,
                scope_ref=ref,
                permissions=permissions,
                org_unit_path=path,
                inert=bool(inert_reason),
                inert_reason=inert_reason,
            )
        )

    if not grants and enforcement == ENFORCEMENT_LEGACY_OPEN:
        # Upgrade behaviour: a principal with no grants keeps exactly the
        # tenant-wide reach they have today. Central Command cannot
        # enumerate a realm's principals to backfill grants, so it must
        # not pretend the absence of a grant is a decision.
        #
        # `grants` includes INERT grants, deliberately: a principal whose
        # only grant points at a vanished target is administered, not
        # ungranted, and gets nothing here (A23.9). Revoked and expired
        # rows are filtered above and still reach this branch -- that is
        # A23-4's never-granted-vs-previously-granted distinction, and
        # the spec assigns it there.
        grants.append(
            Grant(
                scope_type=SCOPE_TENANT,
                scope_ref="",
                permissions=frozenset(role_permissions),
                synthesized=True,
            )
        )

    return _project(
        scope_only=SCOPE_ONLY_MARKER in set(role_permissions),
        tenant_id=tenant_id,
        principal_type=principal_type,
        principal_ref=principal_ref,
        enforcement=enforcement,
        grants=grants,
        unit_by_id=unit_by_id,
        sites=site_list,
        site_unit_paths=site_unit_paths,
    )


def _project(
    *,
    scope_only: bool = False,
    tenant_id: str,
    principal_type: str,
    principal_ref: str,
    enforcement: str,
    grants: Sequence[Grant],
    unit_by_id: Mapping[str, Any],
    sites: Sequence[Any],
    site_unit_paths: Mapping[str, str],
) -> ResolvedScope:
    all_grants = list(grants)
    # Projections are built from EFFECTIVE grants only. An inert grant
    # stays in `.grants` as evidence and contributes no site, path,
    # class or device to any read filter.
    grants = [g for g in all_grants if not g.inert]
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
        grants=tuple(all_grants),
        tenant_wide=tenant_wide,
        site_ids=frozenset(site_ids),
        org_unit_paths=frozenset(org_paths),
        device_classes=frozenset(device_classes),
        device_ids=frozenset(device_ids),
        contextual_unit_ids=frozenset(contextual),
        site_unit_paths=dict(site_unit_paths),
        unit_paths={u.id: u.path for u in unit_by_id.values()},
        scope_only=scope_only,
    )


def expand_rules_to_site_ids(
    rules: Iterable[Any], org_units: Iterable[Any], sites: Iterable[Any]
) -> frozenset[str]:
    """The sites a set of scope RULES reaches, through the org tree.

    A23.3. This is what `resolve_scope`'s `resolved_site_ids` argument
    was always meant to carry: the org-unit rules of the SUBJECT (an
    agent, a campaign) flattened onto sites. The campaign preflight used
    to pass the CALLER's resolved reach there instead, and the union
    inside `resolve_scope` turned a one-site campaign into the caller's
    whole estate. Pure, and it knows nothing about who is asking.
    """
    unit_by_id = {u.id: u for u in org_units}
    site_list = list(sites)
    out: set[str] = set()
    for rule in rules:
        scope_type = getattr(rule, "scope_type", "")
        ref = getattr(rule, "scope_ref", "") or ""
        if scope_type == SCOPE_SITE and ref:
            out.add(ref)
        elif scope_type == SCOPE_ORG_UNIT:
            unit = unit_by_id.get(ref)
            if unit is None:
                continue  # a vanished unit reaches nothing
            for site in site_list:
                parent = unit_by_id.get(getattr(site, "org_unit_id", None) or "")
                if parent is not None and is_descendant(parent.path, unit.path):
                    out.add(site.id)
        elif scope_type == SCOPE_TENANT:
            out.update(s.id for s in site_list)
    return frozenset(out)


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


def _row_in_realm(row: Any, realm: str) -> bool:
    """A grant authorizes only under the realm this CC serves (E1.4).

    A stale-realm grant authorizes nobody, so it must not count as an
    administrator either: counting it would let the real last
    administrator be revoked because a row that resolves to nobody
    "still exists".
    """
    if not realm:
        return True
    return (getattr(row, "realm", "") or "") in (realm, "")


def count_tenant_admins(
    grant_rows: Iterable[Any],
    role_permissions_for: Any,
    *,
    caller_scope: Optional[ResolvedScope] = None,
    now: Optional[datetime] = None,
    realm: str = "",
    exclude_ids: Iterable[str] = (),
    replacement: Any = None,
    permanent_only: bool = False,
    caller_role_permissions: Optional[Iterable[str]] = None,
    role_ceiling_for: Optional[Any] = None,
) -> int:
    """How many distinct principals hold tenant-scope ``role.manage``?

    THE one counting function (A23.8). Revoke, overwrite-through-create,
    reassign and the strict flip all ask this and nothing else, so the
    four cannot disagree about who is an administrator.

    Counts a principal when they hold an **active, unexpired,
    tenant-scope, user** grant **under this realm** whose effective
    permissions (`role_permissions_for(row)` ∩ subset) carry
    ``role.manage`` or ``"*"``.

    Two adjustments let a caller ask "and AFTER this mutation?":

    * `exclude_ids` -- grant ids the mutation removes (a revoke, or the
      row an overwrite replaces).
    * `replacement` -- the row-shaped object the mutation writes in its
      place, counted by the same rule.
    * `permanent_only` -- count only grants with NO expiry. "Setting an
      expiry on the last administrator" is refused by A23.8, and a
      lockout scheduled by the clock is still a configured lockout, so
      the guard also refuses a mutation that leaves no UNEXPIRING
      administrator where one existed.

    The caller's own resolved scope counts them for their own REAL tenant
    row (not a synthesized one, and not one in `exclude_ids`): their
    token is authoritative about their role, which a stored `role` on
    somebody else's grant can never be -- and without this a tenant whose
    grants omit `role` could never flip at all.

    When `caller_role_permissions` (the token's role) is supplied, the
    caller's contribution is recomputed from the ROW in `grant_rows`
    rather than read off `caller_scope`: the scope was resolved before
    the tenant lock, and a concurrent administrator may have narrowed
    the caller's own subset in between. The locked rows are the truth
    at commit; the scope is only trusted for the caller's identity.
    """
    excluded = set(exclude_ids)
    admins: set[str] = set()

    def _qualifies(row: Any) -> bool:
        if getattr(row, "principal_type", PRINCIPAL_USER) != PRINCIPAL_USER:
            return False
        if getattr(row, "scope_type", "") != SCOPE_TENANT:
            return False
        if not is_active(row, now=now) or not _row_in_realm(row, realm):
            return False
        if permanent_only and getattr(row, "expires_at", None) is not None:
            return False
        permissions = effective_permissions(
            role_permissions_for(row), getattr(row, "permission_subset", None)
        )
        return ADMIN_PERMISSION in permissions or "*" in permissions

    rows = list(grant_rows)
    for row in rows:
        if getattr(row, "id", None) in excluded:
            continue
        if _qualifies(row):
            admins.add(getattr(row, "principal_ref", "") or "")

    if replacement is not None and _qualifies(replacement):
        admins.add(getattr(replacement, "principal_ref", "") or "")

    if caller_scope is not None and caller_scope.principal_ref not in admins:
        own = [
            r for r in rows
            if (getattr(r, "principal_ref", None) == caller_scope.principal_ref
                and getattr(r, "principal_type", PRINCIPAL_USER) == PRINCIPAL_USER
                and getattr(r, "scope_type", "") == SCOPE_TENANT)
        ]
        standing = [
            r for r in own
            if getattr(r, "id", None) not in excluded
            and is_active(r, now=now) and _row_in_realm(r, realm)
            and not (permanent_only and getattr(r, "expires_at", None) is not None)
        ]
        if own and len(standing) == len(own):
            if caller_role_permissions is not None:
                # Truth at commit: the locked row, the token's role, the
                # recorded ceiling and the subset -- the resolver's own
                # arithmetic, applied to what is in the database NOW.
                token = list(caller_role_permissions)
                holds = any(
                    ADMIN_PERMISSION in perms or "*" in perms
                    for perms in (
                        grant_permissions(
                            r, token,
                            role_ceiling_for(r) if role_ceiling_for is not None else None,
                        )
                        for r in standing
                    )
                )
            else:
                holds = any(
                    not g.synthesized and not g.inert and g.scope_type == SCOPE_TENANT
                    and (ADMIN_PERMISSION in g.permissions or "*" in g.permissions)
                    for g in caller_scope.grants
                )
            if holds:
                admins.add(caller_scope.principal_ref)

    return len(admins)


def preflight_strict(
    grant_rows: Iterable[Any],
    role_permissions_for: Any,
    *,
    caller_scope: Optional[ResolvedScope] = None,
    now: Optional[datetime] = None,
    realm: str = "",
    caller_role_permissions: Optional[Iterable[str]] = None,
    role_ceiling_for: Optional[Any] = None,
) -> PreflightResult:
    """May this tenant be switched to strict without locking itself out?

    Ratified L1: the flip is allowed only if at least one **active,
    unexpired** principal holds a **tenant-scope** grant whose effective
    permissions contain ``role.manage``. A23.8: answered by the ONE
    counting function the grant mutations also ask.

    Deliberately NOT in the resolver. A resolver that knew about
    administrators would carry a special case into every future caller;
    this is a question the endpoint asks once, at the moment of the flip.
    """
    admins = count_tenant_admins(
        grant_rows, role_permissions_for,
        caller_scope=caller_scope, now=now, realm=realm,
        caller_role_permissions=caller_role_permissions,
        role_ceiling_for=role_ceiling_for,
    )
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
