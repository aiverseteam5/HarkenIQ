"""Tenant lifecycle operations.

Orchestrates tenant creation (DB + Keycloak realm + owner user + license),
suspension, reactivation, and impersonation. Each method is a complete
business operation — API handlers call one method and commit.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_console.db.models import utcnow
from harkeniq_console.db.repos import (
    AuditRepo,
    TenantRepo,
    UserRepo,
)

logger = logging.getLogger("harkeniq.console.tenant")


class TenantError(Exception):
    def __init__(self, message: str, code: str = "error") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class TenantCreateRequest:
    name: str
    slug: str
    billing_country: str
    currency: str = "USD"
    plan: str = "approve"
    node_commit: int = 0
    admin_email: str = ""


def _already_exists(exc: Exception) -> bool:
    """Is this Keycloak error "the thing is already there"?

    Provisioning is a reconciliation, so an object that already exists is
    success, not failure -- but only that. Every other error still fails
    the tenant closed.
    """
    if getattr(exc, "status_code", None) == 409:
        return True
    return "already exists" in str(exc).lower()


class TenantService:
    def __init__(
        self,
        session: AsyncSession,
        keycloak_admin=None,
        licensing_module=None,
    ) -> None:
        self.session = session
        self.keycloak = keycloak_admin
        self.licensing = licensing_module
        self.tenants = TenantRepo(session)
        self.users = UserRepo(session)
        self.audit = AuditRepo(session)

    async def provision_realm(self, tenant, actor: str = "system") -> str:
        """Create this tenant's identity boundary in Keycloak. E1.4.

        realm -> the five tenant roles -> the console client -> the
        recorded binding. Idempotent for a tenant that already has one, so
        it is safe to call on an existing tenant that predates E1.4.

        Raises TenantError on failure, and audits the failure: an identity
        boundary that could not be built is an operational event somebody
        has to see, not a warning in a log nobody reads.
        """
        if self.keycloak is None:
            raise TenantError(
                "no Keycloak admin client is configured", "keycloak_unconfigured"
            )

        # Reconcile against Keycloak's ACTUAL state, not against the
        # recorded binding. Migration 0004 backfills a binding for every
        # tenant created before E1.4 -- and those realms do not exist yet,
        # so trusting the record would report "already provisioned" for a
        # realm nobody can authenticate against. The database records the
        # binding; Keycloak is the truth about whether it exists.
        realm_name = tenant.keycloak_realm or tenant.slug
        try:
            try:
                await self.keycloak.create_realm(realm_name)
            except Exception as exc:
                if not _already_exists(exc):
                    raise
                logger.info(
                    "realm %r already exists; reconciling its roles and client",
                    realm_name,
                )
            # ALWAYS reconcile the roles, whether the realm was just
            # created or already existed. A realm that exists with no
            # roles is what a part-way failure leaves behind, and it is
            # indistinguishable from a healthy one unless this runs.
            await self.keycloak.ensure_realm_roles(realm_name)
            try:
                await self.keycloak.create_client(
                    realm_name, "harkeniq-console",
                    ["http://localhost:8100/*", "http://localhost:5173/*"],
                )
            except Exception as exc:
                if not _already_exists(exc):
                    raise
        except Exception as exc:
            # Name the exception TYPE. An httpx timeout stringifies to the
            # empty string, so "failed: " with nothing after it was an
            # undiagnosable error -- which is its own defect.
            reason = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            logger.error(
                "identity provisioning failed for tenant %s: %s",
                tenant.slug, reason,
            )
            await self.audit.append(
                actor_id=None,
                actor_email=actor,
                action="tenant.realm_provision_failed",
                subject_type="tenant",
                subject_id=tenant.id,
                tenant_id=tenant.id,
                detail={"slug": tenant.slug, "error": reason},
            )
            raise TenantError(
                f"Keycloak realm creation failed: {reason}", "keycloak_error"
            )

        if tenant.keycloak_realm != realm_name:
            await self.tenants.update(tenant, keycloak_realm=realm_name)
        await self.audit.append(
            actor_id=None,
            actor_email=actor,
            action="tenant.realm_provisioned",
            subject_type="tenant",
            subject_id=tenant.id,
            tenant_id=tenant.id,
            detail={"slug": tenant.slug, "keycloak_realm": realm_name},
        )
        return realm_name

    async def create_tenant(
        self, req: TenantCreateRequest, created_by: str = "system",
    ) -> dict:
        """Create tenant with Keycloak realm and owner user."""
        # Validate slug uniqueness
        existing = await self.tenants.get_by_slug(req.slug)
        if existing:
            raise TenantError(f"slug '{req.slug}' already exists", "conflict")

        # E1.4: a tenant without its identity boundary is not a tenant.
        #
        # This used to be `if self.keycloak:` and every production call
        # site passed None, so the whole branch was skipped and the API
        # returned 200 with keycloak_realm=null -- a tenant with no realm,
        # no roles, no client and no owner, that nobody could ever sign in
        # to, reported as created successfully. Refuse instead.
        if self.keycloak is None:
            raise TenantError(
                "cannot create a tenant without a Keycloak admin client: "
                "the tenant's realm, roles and owner are its identity "
                "boundary, and a tenant nobody can authenticate into is "
                "not a tenant",
                "keycloak_unconfigured",
            )

        # Create tenant row
        tenant = await self.tenants.create(
            name=req.name,
            slug=req.slug,
            billing_country=req.billing_country,
            currency=req.currency,
        )

        try:
            realm_name = await self.provision_realm(tenant, actor=created_by)
        except TenantError:
            # Fail CLOSED: the tenant row goes with the realm it could not
            # get. A half-provisioned tenant that reports success is the
            # exact failure this slice exists to remove.
            await self.session.rollback()
            raise

        # A23-5: a tenant without an administrator is not a tenant either.
        #
        # `admin_email` used to be optional and owner minting used to warn
        # and continue, so two ordinary paths produced an ACTIVE tenant
        # with no authoritative owner subject. That was survivable while
        # `legacy_open` synthesized tenant-wide reach for a never-granted
        # human. Under strict birth (A23.11) it is a tenant nobody can
        # ever administer: A23.6 made the first grant a two-person act and
        # A23-4 removed the synthesis that used to supply the second
        # person, so there is no principal left who could recover it.
        #
        # No new lifecycle state (A23.14 D3) -- `tenants.status` keeps its
        # two values. This extends the fail-closed path the realm already
        # uses, one clause above.
        if not req.admin_email:
            await self.session.rollback()
            raise TenantError(
                "cannot create a tenant without an owner: a tenant is born "
                "in strict enforcement, and strict enforcement with no "
                "administrator is a tenant nobody can ever administer. "
                "Supply admin_email.",
                "owner_required",
            )

        try:
            keycloak_user_id = await self.keycloak.create_user(
                realm_name, req.admin_email,
            )
            await self.keycloak.assign_realm_role(
                realm_name, keycloak_user_id, "tenant_owner",
            )
        except Exception as exc:  # noqa: BLE001 -- any failure is fail-closed
            # Fail CLOSED, like the realm above. An owner row carrying no
            # Keycloak subject cannot be granted to -- Central Command
            # seeds the first grant on a SUBJECT, and an email is not an
            # identity -- so a tenant with one is unadministrable.
            logger.warning("Keycloak owner creation failed: %s", exc)
            await self.session.rollback()
            raise TenantError(
                f"could not create the tenant owner in realm "
                f"'{realm_name}': {type(exc).__name__}: {exc}",
                "owner_provision_failed",
            )

        owner = await self.users.create(
            tenant_id=tenant.id,
            email=req.admin_email,
            role="tenant_owner",
            keycloak_user_id=keycloak_user_id,
            status="invited",
            invited_by=created_by,
        )

        # Audit
        await self.audit.append(
            actor_id=None,
            actor_email=created_by,
            action="tenant.create",
            subject_type="tenant",
            subject_id=tenant.id,
            tenant_id=tenant.id,
            detail={
                "name": req.name,
                "slug": req.slug,
                "plan": req.plan,
                "node_commit": req.node_commit,
                "admin_email": req.admin_email,
                # E1.4: the tenant<->realm relationship, in the record.
                "keycloak_realm": realm_name,
            },
        )

        return {
            "id": tenant.id,
            "slug": tenant.slug,
            "name": tenant.name,
            "status": tenant.status,
            "keycloak_realm": realm_name,
            "created_at": tenant.created_at.isoformat(),
            "owner_email": req.admin_email,
            "owner_id": owner.id if owner else None,
        }

    async def suspend_tenant(
        self, tenant_id: str, reason: str, actor_email: str,
    ) -> dict:
        tenant = await self.tenants.get_by_id(tenant_id)
        if not tenant:
            raise TenantError("tenant not found", "not_found")
        if tenant.status == "suspended":
            raise TenantError("tenant already suspended", "conflict")

        await self.tenants.update(
            tenant,
            status="suspended",
            suspended_at=utcnow(),
            suspended_reason=reason,
        )

        await self.audit.append(
            actor_id=None,
            actor_email=actor_email,
            action="tenant.suspend",
            subject_type="tenant",
            subject_id=tenant_id,
            tenant_id=tenant_id,
            detail={"reason": reason},
        )

        return {"id": tenant.id, "status": "suspended"}

    async def reactivate_tenant(
        self, tenant_id: str, actor_email: str,
    ) -> dict:
        tenant = await self.tenants.get_by_id(tenant_id)
        if not tenant:
            raise TenantError("tenant not found", "not_found")
        if tenant.status != "suspended":
            raise TenantError("tenant is not suspended", "conflict")

        await self.tenants.update(
            tenant,
            status="active",
            suspended_at=None,
            suspended_reason=None,
        )

        await self.audit.append(
            actor_id=None,
            actor_email=actor_email,
            action="tenant.reactivate",
            subject_type="tenant",
            subject_id=tenant_id,
            tenant_id=tenant_id,
        )

        return {"id": tenant.id, "status": "active"}

    async def get_tenant_detail(self, tenant_id: str) -> Optional[dict]:
        tenant = await self.tenants.get_by_id(tenant_id)
        if not tenant:
            return None
        user_count = await self.users.count_by_tenant(tenant_id)
        return {
            "id": tenant.id,
            "slug": tenant.slug,
            "name": tenant.name,
            "status": tenant.status,
            "billing_country": tenant.billing_country,
            "currency": tenant.currency,
            "keycloak_realm": tenant.keycloak_realm,
            "created_at": tenant.created_at.isoformat(),
            "updated_at": tenant.updated_at.isoformat(),
            "suspended_at": tenant.suspended_at.isoformat() if tenant.suspended_at else None,
            "suspended_reason": tenant.suspended_reason,
            "user_count": user_count,
        }
