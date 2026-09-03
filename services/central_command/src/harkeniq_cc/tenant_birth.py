"""A23-5: a tenant is born with an administrator (spec A23.11, A23.14).

Strict birth makes a new tenant's posture `strict`, and strict
enforcement with nobody granted is a tenant nobody can administer.
A23.6 made the first tenant grant a two-person act -- an owner may not
self-grant -- and A23-4 removed the `legacy_open` synthesis that used to
supply the second person. So under A23-5 the first grant can no longer
come from a principal at all. It comes from provisioning.

WHY THIS IS NOT A BYPASS. The rules it does not call are rules that do
not apply to it: `refuse_self_grant` and `check_delegation` are
functions of a grantor PRINCIPAL, invoked from the HTTP admission
sequence, and provisioning has no principal. The platform already
authors grant rows this way -- migration 0011 writes
`granted_by='migration:0011'` rows with no admission check.

What bounds it is the PRECONDITION, not the caller. It writes only into
a tenant that has never held a grant row of any lifecycle state, so
there is no tenant state in which it produces a second grant, revives a
revoked administrator (A23.10) or widens anything. Once the row exists
it is an ORDINARY grant: `count_tenant_admins` counts it, A23.8 refuses
its removal as the last administrator, and its holder still cannot
self-grant.

WHERE THE SUBJECT COMES FROM. The Console. It mints the owner and is the
only place the Keycloak subject is recorded (`users.keycloak_user_id`),
so Central Command asks over the existing CC->Console internal channel --
the same direction and the same credential pair used for marketplace
pulls and agent identities (A20). No Console->CC trust direction is
created, and no Keycloak admin credential leaves the identity plane.

A MIGRATED TENANT IS NOT A NEWLY BORN ONE (A23.14 D5). A deployment that
existed before A23-5 carries a settings row pinned by migration 0021, and
that row is this routine's stop sign: it seeds nothing, invents no
administrator for a historical tenant, and leaves the `locked_out`
reading to report the condition.
"""

from __future__ import annotations

import logging
from typing import Optional

from harkeniq_cc.actor import actor_of
from harkeniq_cc.db.repos import AuditRepo, ScopeGrantRepo, TenantSettingsRepo
from harkeniq_cc.grant_integrity import lock_tenant_authorization
from harkeniq_cc.scope import ENFORCEMENT_STRICT

logger = logging.getLogger("harkeniq.cc.tenant_birth")

#: Attribution for the grant and the posture row provisioning authors.
#: `actor_of` already recognises `system:*` as a canonical actor, and the
#: A23-2 impact census already ignores it.
SEED_ACTOR = "system:tenant_birth"

#: The role the first administrator holds. `tenant_owner` is the tenant's
#: own top role -- never `platform_super_admin`, which is a platform
#: identity and is not a tenant's to hold (A12.1).
SEED_ROLE = "tenant_owner"


class BirthOutcome:
    """What one birth attempt did, and why. Reported, never raised."""

    def __init__(self, status: str, reason: str = "", principal_ref: str = "") -> None:
        self.status = status
        self.reason = reason
        self.principal_ref = principal_ref

    def __repr__(self) -> str:  # pragma: no cover - diagnostics
        return f"BirthOutcome({self.status!r}, {self.reason!r})"


async def _fetch_owner_subject(state, realm: str) -> tuple[Optional[str], str]:
    """The Console-recorded owner subject for this realm, or a reason.

    An owner whose `keycloak_user_id` is NULL is not returned by the
    Console at all: a grant keyed on an email is a guess, not an
    authorization, and A23-2 spent a slice removing exactly that
    conflation.
    """
    from harkeniq_cc import identity_client

    body, reason = await identity_client.get(
        state, f"/api/internal/tenants/by-realm/{realm}/owners",
    )
    if body is None:
        return None, reason
    owners = body.get("owners") or []
    if not owners:
        return None, (
            "the Console records no owner with a Keycloak subject for realm "
            f"'{realm}', so there is no identity to grant to"
        )
    # Deterministic: the Console returns owners in creation order and the
    # FIRST recorded owner is the tenant's founding administrator.
    return str(owners[0].get("keycloak_user_id") or ""), ""


async def seed_tenant_birth(state, session) -> BirthOutcome:
    """Give a newly born tenant its first administrator. Idempotent.

    Runs inside the caller's transaction and holds the tenant
    authorization lock, so the precondition it evaluates is still true
    at commit and a concurrent replica's attempt serializes behind it
    rather than writing a second grant.
    """
    tenant_id = getattr(state.config, "tenant_id", "") or ""
    realm = getattr(state.config, "keycloak_realm", "") or ""
    if not tenant_id:
        return BirthOutcome("skipped", "no tenant is configured")

    await lock_tenant_authorization(session, tenant_id)

    settings = TenantSettingsRepo(session)
    grants = ScopeGrantRepo(session)

    # Two stop signs, both re-read under the lock.
    #
    # A settings row means the tenant's posture was decided by somebody
    # -- migration 0021 pinning an existing deployment, or an operator.
    # Either way this is not a birth.
    if await settings.get(tenant_id) is not None:
        return BirthOutcome("already_born", "the tenant's posture is already recorded")

    # A grant row of ANY lifecycle state means the tenant has been
    # administered. A tenant whose only administrator was revoked is a
    # recovery case governed by A23.10, never a birth.
    if await grants.any_grant_exists(tenant_id):
        return BirthOutcome(
            "already_born", "the tenant has held an authorization grant"
        )

    if not realm:
        return BirthOutcome("unadministered", "no Keycloak realm is configured")

    subject, reason = await _fetch_owner_subject(state, realm)
    if not subject:
        # Refuse rather than invent. The tenant stays strict and
        # unadministered, and says so through `locked_out`.
        logger.warning("tenant birth could not resolve an owner: %s", reason)
        audit = AuditRepo(session)
        await audit.append(
            actor=SEED_ACTOR,
            action="scope.grant_refused",
            subject=tenant_id,
            tenant_id=tenant_id,
            detail={"reason": "owner_subject_unknown", "detail": reason},
            actor_ref=actor_of(SEED_ACTOR),
        )
        return BirthOutcome("unadministered", reason)

    row = await grants.seed_first_grant(
        tenant_id=tenant_id,
        principal_ref=subject,
        role=SEED_ROLE,
        realm=realm,
        granted_by=SEED_ACTOR,
        note="first administrator, seeded at tenant birth (A23.14 D4)",
    )
    if row is None:  # pragma: no cover - the lock makes this unreachable
        return BirthOutcome("already_born", "a grant appeared concurrently")

    # The posture row is written explicitly. A missing row already reads
    # strict after A23-5, so this is a pin and an attribution rather than
    # a behaviour change -- and it is what makes the tenant's own state
    # unambiguous on inspection.
    await settings.set_enforcement(tenant_id, ENFORCEMENT_STRICT, SEED_ACTOR)

    audit = AuditRepo(session)
    await audit.append(
        actor=SEED_ACTOR,
        action="scope.granted",
        subject=subject,
        tenant_id=tenant_id,
        detail={
            "seeded": True,
            "source": "tenant_birth",
            "principal_type": "user",
            "scope_type": "tenant",
            "scope_ref": "",
            "role": SEED_ROLE,
        },
        actor_ref=actor_of(SEED_ACTOR),
    )
    await audit.append(
        actor=SEED_ACTOR,
        action="scope.enforcement_changed",
        subject=tenant_id,
        tenant_id=tenant_id,
        detail={"mode": ENFORCEMENT_STRICT, "seeded": True},
        actor_ref=actor_of(SEED_ACTOR),
    )
    logger.info(
        "tenant %s born strict with its first administrator", tenant_id,
    )
    return BirthOutcome("seeded", principal_ref=subject)


async def tenant_birth_once(state) -> BirthOutcome:
    """One attempt, in its own transaction. Safe to call repeatedly.

    Called at Central Command startup, which is when a CC deployment
    first serves its tenant. It is not a polling loop: every call after
    the first returns `already_born` and writes nothing. It exists a
    second time on the reconciliation cadence purely as a recovery net
    for the case where the Console was unreachable at startup, which is
    the one failure that leaves a real tenant unadministered.
    """
    async with state.sessionmaker() as session:
        try:
            outcome = await seed_tenant_birth(state, session)
            await session.commit()
        except Exception as exc:  # noqa: BLE001 -- birth never breaks boot
            await session.rollback()
            logger.warning("tenant birth attempt failed: %s", exc)
            return BirthOutcome("error", str(exc))
    return outcome
