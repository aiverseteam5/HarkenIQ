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

    async def create_tenant(
        self, req: TenantCreateRequest, created_by: str = "system",
    ) -> dict:
        """Create tenant with Keycloak realm and owner user."""
        # Validate slug uniqueness
        existing = await self.tenants.get_by_slug(req.slug)
        if existing:
            raise TenantError(f"slug '{req.slug}' already exists", "conflict")

        # Create tenant row
        tenant = await self.tenants.create(
            name=req.name,
            slug=req.slug,
            billing_country=req.billing_country,
            currency=req.currency,
        )

        # Provision Keycloak realm
        realm_name = None
        if self.keycloak:
            try:
                realm_name = await self.keycloak.create_realm(req.slug)
                await self.keycloak.create_client(
                    realm_name, "harkeniq-console",
                    [f"http://localhost:8100/*", f"http://localhost:5173/*"],
                )
            except Exception as exc:
                logger.error("Keycloak realm creation failed: %s", exc)
                raise TenantError(
                    f"Keycloak realm creation failed: {exc}", "keycloak_error",
                )

        if realm_name:
            await self.tenants.update(tenant, keycloak_realm=realm_name)

        # Create owner user
        owner = None
        if req.admin_email:
            keycloak_user_id = None
            if self.keycloak and realm_name:
                try:
                    keycloak_user_id = await self.keycloak.create_user(
                        realm_name, req.admin_email,
                    )
                    await self.keycloak.assign_realm_role(
                        realm_name, keycloak_user_id, "tenant_owner",
                    )
                except Exception as exc:
                    logger.warning("Keycloak user creation failed: %s", exc)

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
