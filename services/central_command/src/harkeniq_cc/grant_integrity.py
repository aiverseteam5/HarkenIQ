"""Grant lifecycle integrity: the rules a grant mutation must satisfy.

A23-3 (spec A23.6, A23.8, A23.9; design doc §26). Central Command had
one authorization resolver and no rules about how grants LEAVE. This
module is those rules, at the boundary every grant mutation crosses,
so that they hold for the scope-grants router, the org-units router and
the Operational Agent bindings path alike -- and for any caller added
later, because a rule that lives in one route protects one route.

The invariants, in the order a mutation meets them:

* **No self-grant.** A principal never creates, revives or reassigns a
  grant for themselves. Refused by IDENTITY -- the subject, or the
  caller's own email -- before any scope question is asked, so it cannot
  depend on a scope check happening to say no.
* **Delegation is reach AND authority.** The delegated set is
  ``ROLE_PERMISSIONS[role] ∩ subset``, and every member must be held by
  the grantor over the EXACT target through the grantor's own resolved
  scope (`permits`, per grant, never a flattened set). A narrowed
  grantor cannot regain a withheld permission by naming a broader role.
* **The last administrator cannot be configured away.** ONE counting
  function (:func:`harkeniq_cc.scope.count_tenant_admins`) answers
  revoke, overwrite, reassign and the strict flip, under a
  transaction-scoped lock so two concurrent mutations serialize.
* **A vanished target never widens.** An org unit is not deleted while
  an active grant references it; the resolver keeps a grant to a
  missing target as INERT (see `scope.py`), and this module is where a
  deletion learns that.

Nothing here writes a row. Each rule either returns or raises
:class:`GrantIntegrityError`, and the caller decides how to record the
refusal -- every security-sensitive refusal is audited by its router
through the one audit writer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Iterable, Optional, Sequence

from harkeniq_cc.auth import ROLE_PERMISSIONS
from harkeniq_cc.scope import (
    ADMIN_PERMISSION,
    PRINCIPAL_USER,
    SCOPE_DEVICE,
    SCOPE_DEVICE_CLASS,
    SCOPE_ORG_UNIT,
    SCOPE_SITE,
    SCOPE_TENANT,
    ResolvedScope,
    count_tenant_admins,
    effective_permissions,
    is_active,
)

#: The roles a tenant may delegate. `platform_super_admin` is a platform
#: identity (A12.1) and is never a grant's ceiling.
TENANT_ROLES: frozenset[str] = frozenset(
    r for r in ROLE_PERMISSIONS if r != "platform_super_admin"
)

#: The advisory-lock namespace for a tenant's administrator count.
_ADMIN_LOCK = "cc.scope_admins.{tenant_id}"


class GrantIntegrityError(Exception):
    """A grant-lifecycle rule refused the mutation.

    `status` is the HTTP status a router maps it to; `code` is a stable
    machine-readable reason; `reason` is the human sentence; `audit` is
    the audit action the refusal is recorded under (None when the
    refusal is a validation error, not a security event).
    """

    def __init__(
        self, status: int, code: str, reason: str, *, audit: Optional[str] = None,
        detail: Optional[dict] = None,
    ) -> None:
        super().__init__(reason)
        self.status = status
        self.code = code
        self.reason = reason
        self.audit = audit
        self.detail = dict(detail or {})


# ---------------------------------------------------------------------------
# What a row carries
# ---------------------------------------------------------------------------


def role_ceiling_for(row: Any) -> Optional[list[str]]:
    """The permission list of the role a grant row RECORDS, or None.

    The resolver's `role_ceiling_for` input (A23-3). A row that recorded
    no role, or a role name this Central Command does not know, gets no
    ceiling and resolves exactly as before -- upgrade changes nothing
    for them. A row naming a known role is capped by it.
    """
    role = (getattr(row, "role", "") or "").strip()
    if role in ROLE_PERMISSIONS:
        return list(ROLE_PERMISSIONS[role])
    return None


def token_permissions(user: Any) -> list[str]:
    """The caller's role permissions, exactly as `get_scope` resolves them.

    One rule for what a token says: the role table when the role is
    known, the token's own permission list otherwise (a machine
    principal carries its A20.3 ceiling there).
    """
    role = getattr(user, "role", "") or ""
    listed = list(getattr(user, "permissions", None) or [])
    return list(ROLE_PERMISSIONS.get(role, listed)) or listed


def role_permissions_for(row: Any) -> list[str]:
    """The permission basis the administrator count uses for a row.

    A grant with no role named cannot be shown to carry `role.manage`,
    so it counts for nothing here: counting it would let the last
    administrator be revoked on the strength of somebody who turns out
    not to be one.
    """
    return list(ROLE_PERMISSIONS.get((getattr(row, "role", "") or "").strip(), []))


def delegated_permissions(role: str, subset: Optional[Sequence[str]]) -> frozenset[str]:
    """``ROLE_PERMISSIONS[role] ∩ subset`` -- what a human grant delegates.

    A human grant must name a tenant role: without one the grant's
    ceiling would be whatever the recipient's own token says, which is
    the one thing delegation must never be bounded by.
    """
    role = (role or "").strip()
    if role not in TENANT_ROLES:
        raise GrantIntegrityError(
            400, "role_required",
            f"a grant must name the tenant role it narrows (one of "
            f"{', '.join(sorted(TENANT_ROLES))}); got {role!r}",
        )
    return effective_permissions(ROLE_PERMISSIONS[role], subset)


def grant_shape(
    *,
    principal_type: str,
    principal_ref: str,
    scope_type: str,
    scope_ref: str = "",
    role: str = "",
    permission_subset: Optional[Sequence[str]] = None,
    expires_at: Optional[datetime] = None,
    realm: str = "",
    grant_id: Optional[str] = None,
) -> SimpleNamespace:
    """A row-shaped object for "what if this were written".

    Passed as `replacement` to the counting function so an overwrite or
    a reassign is judged by the SAME rule as a stored row.
    """
    return SimpleNamespace(
        id=grant_id,
        principal_type=principal_type,
        principal_ref=principal_ref,
        scope_type=scope_type,
        scope_ref=scope_ref,
        role=role,
        permission_subset=list(permission_subset) if permission_subset is not None else None,
        expires_at=expires_at,
        revoked_at=None,
        realm=realm,
    )


# ---------------------------------------------------------------------------
# Self-grant
# ---------------------------------------------------------------------------


def refuse_self_grant(user: Any, principal_type: str, principal_ref: str) -> None:
    """A23.6: a principal may never administer their own authority.

    Matched on the stable subject and, defensively, on the caller's own
    email: a grant keyed by email authorizes nobody (subjects are the
    key), but a grant that exists is a grant somebody will later
    "fix", and the fix would be a self-grant with a delay.
    """
    if principal_type != PRINCIPAL_USER:
        return
    ref = (principal_ref or "").strip()
    own = {(getattr(user, "user_id", "") or "").strip()}
    email = (getattr(user, "email", "") or "").strip().lower()
    if email:
        own.add(email)
    if ref and (ref in own or ref.lower() in own):
        raise GrantIntegrityError(
            403, "self_grant",
            "self-grant is forbidden: a principal may not create, change or "
            "move a grant for themselves, whatever the scope or permissions "
            "requested; another administrator must do it (A23.6)",
            audit="scope.grant_refused",
            detail={"reason": "self_grant"},
        )


# ---------------------------------------------------------------------------
# Delegation
# ---------------------------------------------------------------------------


def target_kwargs(scope_type: str, scope_ref: str, unit_path: str = "") -> dict:
    """The `permits(...)` target for a grant's scope."""
    if scope_type == SCOPE_TENANT:
        return {"tenant_object": True}
    if scope_type == SCOPE_ORG_UNIT:
        return {"org_unit_path": unit_path}
    if scope_type == SCOPE_SITE:
        return {"site_id": scope_ref}
    # device and device_class span whatever they match, so only a
    # tenant-wide grantor may hand them out.
    return {"tenant_object": True}


def describe_target(scope_type: str, scope_ref: str) -> str:
    if scope_type == SCOPE_TENANT:
        return "a tenant-wide grant"
    return f"{scope_type} {scope_ref!r}"


def check_delegation(
    grantor: ResolvedScope,
    *,
    permissions: Iterable[str],
    target: dict,
    what: str,
) -> None:
    """Reach AND authority, per permission, on the exact target.

    The grantor must hold `role.manage` over the target (reach to
    administer it) AND every delegated permission over that same
    target (authority to hand it out). Both are asked of the grantor's
    own resolved scope through `permits`, which checks coverage and
    permission on ONE grant -- so a grantor holding `action.approve`
    over site A and `role.manage` over site B delegates neither at the
    other.
    """
    if not grantor.permits(ADMIN_PERMISSION, **target):
        raise GrantIntegrityError(
            403, "outside_scope",
            f"{what} is outside your authorized scope: you do not hold "
            f"{ADMIN_PERMISSION!r} over it",
            audit="scope.grant_refused",
            detail={"reason": "outside_scope"},
        )
    missing = sorted(
        p for p in set(permissions)
        if p != ADMIN_PERMISSION and not grantor.permits(p, **target)
    )
    if missing:
        raise GrantIntegrityError(
            403, "exceeds_grantor",
            f"cannot delegate {', '.join(missing)} over {what}: you do not "
            "effectively hold them there, and delegated authority is bounded "
            "by the grantor's own effective permissions on the exact target "
            "(A23.6) -- naming a broader role does not restore what your own "
            "grant withholds",
            audit="scope.grant_refused",
            detail={"reason": "exceeds_grantor", "missing": missing},
        )


# ---------------------------------------------------------------------------
# The last administrator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdminTransition:
    """Administrator counts around one proposed mutation.

    `before`/`after` count ACTIVE tenant administrators (an expiring one
    included while it lasts); `permanent_before`/`permanent_after` count
    those with no expiry at all. A mutation is refused when it takes
    EITHER count from >=1 to 0: removing the last administrator, or
    leaving only administrators the clock will remove.
    """

    before: int
    after: int
    permanent_before: int = 0
    permanent_after: int = 0

    @property
    def removes_last(self) -> bool:
        return (self.before >= 1 and self.after == 0) or (
            self.permanent_before >= 1 and self.permanent_after == 0
        )


async def lock_tenant_authorization(session: Any, tenant_id: str) -> bool:
    """Serialize a tenant's grant mutations for the rest of this transaction.

    A transaction-scoped PostgreSQL advisory lock (R5-2's helper, keyed
    on the tenant), held through the caller's commit. Two
    administrators revoking each other's tenant grant at once both used
    to count two and both used to succeed; the second now waits, then
    re-reads a committed count of one and is refused. No-op on sqlite,
    which is single-writer.
    """
    from harkeniq.audit.chain import pg_advisory_chain_lock

    return await pg_advisory_chain_lock(session, _ADMIN_LOCK.format(tenant_id=tenant_id))


def admin_transition(
    rows: Iterable[Any],
    *,
    caller_scope: Optional[ResolvedScope] = None,
    caller_role_permissions: Optional[Iterable[str]] = None,
    realm: str = "",
    exclude_ids: Iterable[str] = (),
    replacement: Any = None,
    now: Optional[datetime] = None,
) -> AdminTransition:
    """Administrator count before and after a proposed mutation. Pure."""
    rows = list(rows)
    token = list(caller_role_permissions) if caller_role_permissions is not None else None

    def _count(**adjust) -> int:
        return count_tenant_admins(
            rows, role_permissions_for, caller_scope=caller_scope, realm=realm,
            now=now, caller_role_permissions=token,
            role_ceiling_for=role_ceiling_for, **adjust,
        )

    return AdminTransition(
        before=_count(),
        after=_count(exclude_ids=exclude_ids, replacement=replacement),
        permanent_before=_count(permanent_only=True),
        permanent_after=_count(
            exclude_ids=exclude_ids, replacement=replacement, permanent_only=True,
        ),
    )


async def guard_last_admin(
    session: Any,
    *,
    tenant_id: str,
    realm: str,
    caller_scope: Optional[ResolvedScope],
    mutation: str,
    exclude_ids: Iterable[str] = (),
    replacement: Any = None,
    audit: str,
    caller_role_permissions: Optional[Iterable[str]] = None,
) -> AdminTransition:
    """Refuse a mutation that would leave the tenant with no administrator.

    Takes the tenant lock FIRST, then reads every grant, then counts --
    so the count a refusal or a pass stands on is the count at commit.
    A23.8: applies to revoke, to an overwrite through create, to
    reassignment and (with `before` ignored) to the strict flip.

    A tenant that already has zero administrators (a `legacy_open`
    tenant that never granted one) is not protected here: there is
    nothing to remove, and refusing every mutation would block the very
    grant that creates the first administrator.
    """
    from harkeniq_cc.db.repos import ScopeGrantRepo

    await lock_tenant_authorization(session, tenant_id)
    rows = await ScopeGrantRepo(session).list_all(tenant_id)
    transition = admin_transition(
        rows, caller_scope=caller_scope, realm=realm,
        caller_role_permissions=caller_role_permissions,
        exclude_ids=exclude_ids, replacement=replacement,
    )
    if transition.removes_last:
        raise GrantIntegrityError(
            409, "last_admin",
            f"refusing to {mutation}: it would remove the last active "
            f"tenant-scope grant carrying {ADMIN_PERMISSION!r}, leaving this "
            "tenant with no administrator able to grant scope. Grant "
            f"{ADMIN_PERMISSION!r} at tenant scope to another principal first. "
            "Nothing has been changed (A23.8).",
            audit=audit,
            detail={
                "reason": "last_admin",
                "admins_before": transition.before,
                "admins_after": transition.after,
                "permanent_admins_before": transition.permanent_before,
                "permanent_admins_after": transition.permanent_after,
            },
        )
    return transition


# ---------------------------------------------------------------------------
# Vanished targets
# ---------------------------------------------------------------------------


async def referencing_grants(
    session: Any, tenant_id: str, scope_type: str, scope_ref: str,
    *, now: Optional[datetime] = None,
) -> list[Any]:
    """Active grants -- user AND agent -- that name this target."""
    from harkeniq_cc.db.repos import ScopeGrantRepo

    rows = await ScopeGrantRepo(session).list_referencing(
        tenant_id, scope_type, scope_ref,
    )
    return [r for r in rows if is_active(r, now=now)]


async def refuse_unit_delete_under_grants(session: Any, tenant_id: str, unit: Any) -> None:
    """A23.9: an org unit is not deleted while active grants reference it.

    The alternative -- deleting it and letting those grants point at
    nothing -- is exactly the shape the resolver now keeps INERT, and
    inert is a recovery posture, not a design. The operator reassigns
    or revokes first, and the refusal names every grant so they can.
    """
    rows = await referencing_grants(session, tenant_id, SCOPE_ORG_UNIT, unit.id)
    if not rows:
        return
    principals = sorted({(r.principal_type, r.principal_ref) for r in rows})
    raise GrantIntegrityError(
        409, "unit_referenced_by_grants",
        f"org unit {unit.name!r} is referenced by {len(rows)} active scope "
        f"grant(s) held by {len(principals)} principal(s); reassign or revoke "
        "them before deleting it. Deleting a unit from under a grant is "
        "refused rather than silently detaching or widening anything (A23.9)",
        audit="org_unit.delete_refused",
        detail={
            "reason": "unit_referenced_by_grants",
            "grant_ids": [r.id for r in rows],
            "principals": [
                {"principal_type": t, "principal_ref": p} for t, p in principals
            ],
        },
    )


def target_status(row: Any, *, unit_ids: set[str], site_ids: set[str],
                  device_ids: Optional[set[str]] = None) -> str:
    """Is the grant's target still there? present | missing | n/a."""
    ref = row.scope_ref or ""
    if row.scope_type == SCOPE_ORG_UNIT:
        return "present" if ref in unit_ids else "missing"
    if row.scope_type == SCOPE_SITE:
        return "present" if ref in site_ids else "missing"
    if row.scope_type == SCOPE_DEVICE and device_ids is not None:
        return "present" if ref in device_ids else "missing"
    if row.scope_type in (SCOPE_TENANT, SCOPE_DEVICE_CLASS):
        return "n/a"
    return "unknown"
