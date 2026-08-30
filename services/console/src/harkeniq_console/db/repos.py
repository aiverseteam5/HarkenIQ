"""Thin data-access helpers. Each repo wraps one AsyncSession.

Commit responsibility stays with the caller (one commit per API
request) so multi-table updates remain atomic.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Optional, Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_console.db.models import (
    ApiKey,
    ConsoleAuditLog,
    CreditNote,
    CustomRole,
    DelinquencyLog,
    FeatureFlag,
    ImpersonationLog,
    Invoice,
    InvoiceLine,
    License,
    MarketplaceInstall,
    MarketplaceSkill,
    Payment,
    PaymentProviderCustomer,
    PlatformSetting,
    PriceBook,
    Subscription,
    SupportAccessLog,
    TenantService,
    SupportTicket,
    Tenant,
    TenantSite,
    TicketMessage,
    TicketStateChange,
    UsageEvent,
    User,
    UserCustomRole,
    utcnow,
)


class TenantRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, tenant_id: str) -> Optional[Tenant]:
        return await self.session.get(Tenant, tenant_id)

    async def get_by_realm(self, realm: str) -> Optional[Tenant]:
        """Resolve a Keycloak realm to its tenant. E1.4.

        The authoritative identity lookup. Authorization used to resolve
        by slug, which agreed with the realm only by naming convention;
        this reads the recorded binding, which migration 0004 populated
        for every tenant and a unique index keeps unambiguous.
        """
        if not realm:
            return None
        return (
            await self.session.execute(
                select(Tenant).where(Tenant.keycloak_realm == realm)
            )
        ).scalar_one_or_none()

    async def get_by_slug(self, slug: str) -> Optional[Tenant]:
        return (
            await self.session.execute(
                select(Tenant).where(Tenant.slug == slug)
            )
        ).scalar_one_or_none()

    async def list_filtered(
        self,
        search: Optional[str] = None,
        status: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[Sequence[Tenant], int]:
        stmt = select(Tenant)
        count_stmt = select(func.count()).select_from(Tenant)
        if status:
            stmt = stmt.where(Tenant.status == status)
            count_stmt = count_stmt.where(Tenant.status == status)
        if search:
            pattern = f"%{search}%"
            flt = or_(Tenant.name.ilike(pattern), Tenant.slug.ilike(pattern))
            stmt = stmt.where(flt)
            count_stmt = count_stmt.where(flt)
        total = (await self.session.execute(count_stmt)).scalar_one()
        stmt = stmt.order_by(Tenant.created_at).offset((page - 1) * page_size).limit(page_size)
        items = (await self.session.execute(stmt)).scalars().all()
        return items, total

    async def create(self, **kwargs) -> Tenant:
        tenant = Tenant(**kwargs)
        self.session.add(tenant)
        await self.session.flush()
        return tenant

    async def update(self, tenant: Tenant, **kwargs) -> Tenant:
        for key, value in kwargs.items():
            if hasattr(tenant, key):
                setattr(tenant, key, value)
        tenant.updated_at = utcnow()
        await self.session.flush()
        return tenant

    async def count(self) -> int:
        return (
            await self.session.execute(select(func.count()).select_from(Tenant))
        ).scalar_one()


class UserRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, user_id: str) -> Optional[User]:
        return await self.session.get(User, user_id)

    async def get_by_keycloak_id(self, keycloak_user_id: str) -> Optional[User]:
        """Resolve the local user row from the JWT subject.

        UserContext.user_id is the Keycloak subject; custom-role grants are
        keyed by the local users.id, so the two must be joined explicitly.
        """
        return (
            await self.session.execute(
                select(User).where(User.keycloak_user_id == keycloak_user_id)
            )
        ).scalar_one_or_none()

    async def get_by_email(self, tenant_id: str, email: str) -> Optional[User]:
        return (
            await self.session.execute(
                select(User).where(User.tenant_id == tenant_id, User.email == email)
            )
        ).scalar_one_or_none()

    async def list_by_tenant(
        self,
        tenant_id: str,
        search: Optional[str] = None,
        role: Optional[str] = None,
        status: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[Sequence[User], int]:
        stmt = select(User).where(User.tenant_id == tenant_id)
        count_stmt = select(func.count()).select_from(User).where(User.tenant_id == tenant_id)
        if role:
            stmt = stmt.where(User.role == role)
            count_stmt = count_stmt.where(User.role == role)
        if status:
            stmt = stmt.where(User.status == status)
            count_stmt = count_stmt.where(User.status == status)
        if search:
            pattern = f"%{search}%"
            flt = or_(User.email.ilike(pattern), User.display_name.ilike(pattern))
            stmt = stmt.where(flt)
            count_stmt = count_stmt.where(flt)
        total = (await self.session.execute(count_stmt)).scalar_one()
        stmt = stmt.order_by(User.created_at).offset((page - 1) * page_size).limit(page_size)
        items = (await self.session.execute(stmt)).scalars().all()
        return items, total

    async def create(self, **kwargs) -> User:
        user = User(**kwargs)
        self.session.add(user)
        await self.session.flush()
        return user

    async def update(self, user: User, **kwargs) -> User:
        for key, value in kwargs.items():
            if hasattr(user, key):
                setattr(user, key, value)
        await self.session.flush()
        return user

    async def count_by_tenant(self, tenant_id: str) -> int:
        return (
            await self.session.execute(
                select(func.count()).select_from(User).where(User.tenant_id == tenant_id)
            )
        ).scalar_one()


class CustomRoleRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, role_id: str) -> Optional[CustomRole]:
        return await self.session.get(CustomRole, role_id)

    async def list_by_tenant(self, tenant_id: str) -> Sequence[CustomRole]:
        return (
            await self.session.execute(
                select(CustomRole).where(CustomRole.tenant_id == tenant_id)
            )
        ).scalars().all()

    async def create(self, **kwargs) -> CustomRole:
        role = CustomRole(**kwargs)
        self.session.add(role)
        await self.session.flush()
        return role

    async def update(self, role: CustomRole, **kwargs) -> CustomRole:
        for key, value in kwargs.items():
            if hasattr(role, key):
                setattr(role, key, value)
        await self.session.flush()
        return role

    async def delete(self, role: CustomRole) -> None:
        await self.session.delete(role)
        await self.session.flush()

    async def get_user_custom_roles(self, user_id: str) -> Sequence[CustomRole]:
        return (
            await self.session.execute(
                select(CustomRole)
                .join(UserCustomRole, UserCustomRole.custom_role_id == CustomRole.id)
                .where(UserCustomRole.user_id == user_id)
            )
        ).scalars().all()


#: Serializes hash-chain appends within this process (R4-2 P12); the
#: UNIQUE constraint on console_audit_log.seq is the cross-process backstop.
_audit_chain_lock = asyncio.Lock()


class AuditRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _chain_ts(ts) -> str:
        """Timezone-stable timestamp string for hashing.

        sqlite returns naive datetimes for values written tz-aware, so
        normalize to naive UTC before formatting -- the payload string
        must be identical at write time and at verify time.
        """
        from datetime import timezone as _tz
        if ts.tzinfo is not None:
            ts = ts.astimezone(_tz.utc).replace(tzinfo=None)
        return ts.isoformat()

    @staticmethod
    def _chain_payload(row: ConsoleAuditLog) -> dict:
        return {
            "ts": AuditRepo._chain_ts(row.ts),
            "actor_id": row.actor_id,
            "actor_email": row.actor_email,
            "action": row.action,
            "subject_type": row.subject_type,
            "subject_id": row.subject_id,
            "tenant_id": row.tenant_id,
            "detail": row.detail,
        }

    async def append(
        self,
        actor_id: Optional[str],
        actor_email: str,
        action: str,
        subject_type: str = "",
        subject_id: str = "",
        tenant_id: Optional[str] = None,
        detail: Optional[dict] = None,
    ) -> ConsoleAuditLog:
        from harkeniq.audit.chain import next_link, pg_advisory_chain_lock

        row = ConsoleAuditLog(
            ts=utcnow(),
            actor_id=actor_id,
            actor_email=actor_email,
            action=action,
            subject_type=subject_type,
            subject_id=subject_id,
            tenant_id=tenant_id,
            detail=detail,
        )
        async with _audit_chain_lock:
            # R5-2: cross-replica serialization on PostgreSQL (held
            # through the caller's commit); no-op on sqlite.
            await pg_advisory_chain_lock(self.session, "console.console_audit_log")
            tail = (
                await self.session.execute(
                    select(ConsoleAuditLog.seq, ConsoleAuditLog.entry_hash)
                    .where(ConsoleAuditLog.seq.isnot(None))
                    .order_by(ConsoleAuditLog.seq.desc())
                    .limit(1)
                )
            ).first()
            row.seq, row.prev_hash, row.entry_hash = next_link(
                tail[0] if tail else 0,
                tail[1] if tail else None,
                self._chain_payload(row),
            )
            self.session.add(row)
            await self.session.flush()
        return row

    async def verify_chain(self):
        """Verify the audit hash chain (R4-2 P12); returns ChainVerification."""
        from harkeniq.audit.chain import verify_chain

        rows = (
            await self.session.execute(
                select(ConsoleAuditLog)
                .where(ConsoleAuditLog.seq.isnot(None))
                .order_by(ConsoleAuditLog.seq)
            )
        ).scalars().all()
        return verify_chain(
            (r.seq, r.prev_hash, r.entry_hash, self._chain_payload(r))
            for r in rows
        )

    async def list_filtered(
        self,
        tenant_id: Optional[str] = None,
        actor: Optional[str] = None,
        action: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        search: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[Sequence[ConsoleAuditLog], int]:
        stmt = select(ConsoleAuditLog)
        count_stmt = select(func.count()).select_from(ConsoleAuditLog)
        if tenant_id:
            stmt = stmt.where(ConsoleAuditLog.tenant_id == tenant_id)
            count_stmt = count_stmt.where(ConsoleAuditLog.tenant_id == tenant_id)
        if actor:
            pattern = f"%{actor}%"
            flt = ConsoleAuditLog.actor_email.ilike(pattern)
            stmt = stmt.where(flt)
            count_stmt = count_stmt.where(flt)
        if action:
            stmt = stmt.where(ConsoleAuditLog.action == action)
            count_stmt = count_stmt.where(ConsoleAuditLog.action == action)
        if date_from:
            stmt = stmt.where(ConsoleAuditLog.ts >= date_from)
            count_stmt = count_stmt.where(ConsoleAuditLog.ts >= date_from)
        if date_to:
            stmt = stmt.where(ConsoleAuditLog.ts <= date_to)
            count_stmt = count_stmt.where(ConsoleAuditLog.ts <= date_to)
        if search:
            pattern = f"%{search}%"
            flt = or_(
                ConsoleAuditLog.action.ilike(pattern),
                ConsoleAuditLog.subject_type.ilike(pattern),
            )
            stmt = stmt.where(flt)
            count_stmt = count_stmt.where(flt)
        total = (await self.session.execute(count_stmt)).scalar_one()
        stmt = stmt.order_by(ConsoleAuditLog.ts.desc()).offset((page - 1) * page_size).limit(page_size)
        items = (await self.session.execute(stmt)).scalars().all()
        return items, total


class SettingsRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, key: str) -> Optional[PlatformSetting]:
        return await self.session.get(PlatformSetting, key)

    async def set(self, key: str, value, updated_by: Optional[str] = None) -> PlatformSetting:
        setting = await self.get(key)
        if setting is None:
            setting = PlatformSetting(key=key, value=value, updated_by=updated_by)
            self.session.add(setting)
        else:
            setting.value = value
            setting.updated_by = updated_by
            setting.updated_at = utcnow()
        await self.session.flush()
        return setting


class LicenseRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, license_id: str) -> Optional[License]:
        return await self.session.get(License, license_id)

    async def get_by_fingerprint(self, fingerprint: str) -> Optional[License]:
        return (
            await self.session.execute(
                select(License).where(License.fingerprint == fingerprint)
            )
        ).scalar_one_or_none()

    async def list_by_tenant(
        self,
        tenant_id: str,
        status: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[Sequence[License], int]:
        stmt = select(License).where(License.tenant_id == tenant_id)
        count_stmt = select(func.count()).select_from(License).where(License.tenant_id == tenant_id)
        if status:
            stmt = stmt.where(License.status == status)
            count_stmt = count_stmt.where(License.status == status)
        total = (await self.session.execute(count_stmt)).scalar_one()
        stmt = stmt.order_by(License.issued_at.desc()).offset((page - 1) * page_size).limit(page_size)
        items = (await self.session.execute(stmt)).scalars().all()
        return items, total

    async def create(self, **kwargs) -> License:
        lic = License(**kwargs)
        self.session.add(lic)
        await self.session.flush()
        return lic

    async def update(self, license: License, **kwargs) -> License:
        for key, value in kwargs.items():
            if hasattr(license, key):
                setattr(license, key, value)
        await self.session.flush()
        return license

    async def revoke(self, license: License, revoked_by: str, reason: str) -> License:
        license.status = "revoked"
        license.revoked_by = revoked_by
        license.revoked_at = utcnow()
        license.revoke_reason = reason
        await self.session.flush()
        return license


class SubscriptionRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_tenant(self, tenant_id: str) -> Optional[Subscription]:
        return (
            await self.session.execute(
                select(Subscription).where(Subscription.tenant_id == tenant_id)
            )
        ).scalar_one_or_none()

    async def create(self, **kwargs) -> Subscription:
        sub = Subscription(**kwargs)
        self.session.add(sub)
        await self.session.flush()
        return sub

    async def update(self, subscription: Subscription, **kwargs) -> Subscription:
        for key, value in kwargs.items():
            if hasattr(subscription, key):
                setattr(subscription, key, value)
        subscription.updated_at = utcnow()
        await self.session.flush()
        return subscription


class TenantSiteRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, site_id: str) -> Optional[TenantSite]:
        return await self.session.get(TenantSite, site_id)

    async def list_by_tenant(self, tenant_id: str) -> Sequence[TenantSite]:
        return (
            await self.session.execute(
                select(TenantSite).where(TenantSite.tenant_id == tenant_id)
            )
        ).scalars().all()

    async def create(self, **kwargs) -> TenantSite:
        site = TenantSite(**kwargs)
        self.session.add(site)
        await self.session.flush()
        return site

    async def update(self, site: TenantSite, **kwargs) -> TenantSite:
        for key, value in kwargs.items():
            if hasattr(site, key):
                setattr(site, key, value)
        await self.session.flush()
        return site


class FeatureFlagRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_by_tenant(self, tenant_id: str) -> Sequence[FeatureFlag]:
        return (
            await self.session.execute(
                select(FeatureFlag).where(
                    or_(FeatureFlag.tenant_id == tenant_id, FeatureFlag.tenant_id.is_(None))
                )
            )
        ).scalars().all()

    async def get(self, tenant_id: str, feature_name: str) -> Optional[FeatureFlag]:
        return (
            await self.session.execute(
                select(FeatureFlag).where(
                    FeatureFlag.tenant_id == tenant_id,
                    FeatureFlag.feature_name == feature_name,
                )
            )
        ).scalar_one_or_none()

    async def set_flag(
        self,
        tenant_id: str,
        feature_name: str,
        enabled: bool,
        updated_by: Optional[str] = None,
    ) -> FeatureFlag:
        flag = await self.get(tenant_id, feature_name)
        if flag is None:
            flag = FeatureFlag(
                tenant_id=tenant_id,
                feature_name=feature_name,
                enabled=enabled,
                updated_by=updated_by,
            )
            self.session.add(flag)
        else:
            flag.enabled = enabled
            flag.updated_by = updated_by
            flag.updated_at = utcnow()
        await self.session.flush()
        return flag

    async def list_globals(self) -> Sequence[FeatureFlag]:
        return (
            await self.session.execute(
                select(FeatureFlag).where(FeatureFlag.tenant_id.is_(None))
            )
        ).scalars().all()


# ── Phase 4: billing repos ───────────────────────────────────────────


class PriceBookRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_price(
        self,
        plan: str,
        currency: str,
        billing_interval: str,
        version: int = 1,
    ) -> Optional[PriceBook]:
        return (
            await self.session.execute(
                select(PriceBook).where(
                    PriceBook.plan == plan,
                    PriceBook.currency == currency,
                    PriceBook.billing_interval == billing_interval,
                    PriceBook.version == version,
                )
            )
        ).scalar_one_or_none()

    async def list_all(self, version: Optional[int] = None) -> Sequence[PriceBook]:
        stmt = select(PriceBook)
        if version is not None:
            stmt = stmt.where(PriceBook.version == version)
        return (await self.session.execute(stmt.order_by(PriceBook.plan))).scalars().all()

    async def create(self, **kwargs) -> PriceBook:
        row = PriceBook(**kwargs)
        self.session.add(row)
        await self.session.flush()
        return row


class UsageEventRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record(self, **kwargs) -> UsageEvent:
        row = UsageEvent(**kwargs)
        self.session.add(row)
        await self.session.flush()
        return row

    async def record_batch(self, events: list[dict]) -> int:
        rows = [UsageEvent(**e) for e in events]
        self.session.add_all(rows)
        await self.session.flush()
        return len(rows)

    async def get_high_water(
        self,
        tenant_id: str,
        period_start: datetime,
        period_end: datetime,
    ) -> int:
        result = await self.session.execute(
            select(func.max(UsageEvent.node_count)).where(
                UsageEvent.tenant_id == tenant_id,
                UsageEvent.date >= period_start,
                UsageEvent.date <= period_end,
            )
        )
        return result.scalar_one() or 0

    async def get_daily_counts(
        self,
        tenant_id: str,
        period_start: datetime,
        period_end: datetime,
    ) -> Sequence[UsageEvent]:
        return (
            await self.session.execute(
                select(UsageEvent)
                .where(
                    UsageEvent.tenant_id == tenant_id,
                    UsageEvent.date >= period_start,
                    UsageEvent.date <= period_end,
                )
                .order_by(UsageEvent.date)
            )
        ).scalars().all()

    async def get_per_site_summary(
        self,
        tenant_id: str,
        period_start: datetime,
        period_end: datetime,
    ) -> list[dict]:
        result = await self.session.execute(
            select(
                UsageEvent.site_name,
                func.avg(UsageEvent.node_count).label("avg_nodes"),
                func.max(UsageEvent.node_count).label("peak_nodes"),
                func.count().label("days"),
            )
            .where(
                UsageEvent.tenant_id == tenant_id,
                UsageEvent.date >= period_start,
                UsageEvent.date <= period_end,
            )
            .group_by(UsageEvent.site_name)
        )
        return [
            {
                "site_name": r.site_name,
                "avg_nodes": float(r.avg_nodes or 0),
                "peak_nodes": r.peak_nodes or 0,
                "days": r.days,
            }
            for r in result.all()
        ]


class InvoiceRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, invoice_id: str) -> Optional[Invoice]:
        return await self.session.get(Invoice, invoice_id)

    async def get_by_number(self, invoice_number: str) -> Optional[Invoice]:
        return (
            await self.session.execute(
                select(Invoice).where(Invoice.invoice_number == invoice_number)
            )
        ).scalar_one_or_none()

    async def list_by_tenant(
        self,
        tenant_id: str,
        status: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[Sequence[Invoice], int]:
        stmt = select(Invoice).where(Invoice.tenant_id == tenant_id)
        count_stmt = select(func.count()).select_from(Invoice).where(
            Invoice.tenant_id == tenant_id
        )
        if status:
            stmt = stmt.where(Invoice.status == status)
            count_stmt = count_stmt.where(Invoice.status == status)
        total = (await self.session.execute(count_stmt)).scalar_one()
        stmt = (
            stmt.order_by(Invoice.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        items = (await self.session.execute(stmt)).scalars().all()
        return items, total

    async def list_overdue(self) -> Sequence[Invoice]:
        return (
            await self.session.execute(
                select(Invoice).where(
                    Invoice.status == "issued",
                    Invoice.due_at < utcnow(),
                )
            )
        ).scalars().all()

    async def create(self, **kwargs) -> Invoice:
        inv = Invoice(**kwargs)
        self.session.add(inv)
        await self.session.flush()
        return inv

    async def update(self, invoice: Invoice, **kwargs) -> Invoice:
        for key, value in kwargs.items():
            if hasattr(invoice, key):
                setattr(invoice, key, value)
        await self.session.flush()
        return invoice

    async def count_by_tenant(self, tenant_id: str) -> int:
        return (
            await self.session.execute(
                select(func.count())
                .select_from(Invoice)
                .where(Invoice.tenant_id == tenant_id)
            )
        ).scalar_one()

    async def get_revenue_by_plan(self) -> list[dict]:
        """Aggregate paid revenue grouped by invoice type."""
        result = await self.session.execute(
            select(
                Invoice.type,
                Invoice.currency,
                func.sum(Invoice.total_cents).label("total"),
                func.count().label("count"),
            )
            .where(Invoice.status == "paid")
            .group_by(Invoice.type, Invoice.currency)
        )
        return [
            {"type": r.type, "currency": r.currency, "total_cents": r.total or 0, "count": r.count}
            for r in result.all()
        ]


class InvoiceLineRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, **kwargs) -> InvoiceLine:
        line = InvoiceLine(**kwargs)
        self.session.add(line)
        await self.session.flush()
        return line

    async def list_by_invoice(self, invoice_id: str) -> Sequence[InvoiceLine]:
        return (
            await self.session.execute(
                select(InvoiceLine).where(InvoiceLine.invoice_id == invoice_id)
            )
        ).scalars().all()


class CreditNoteRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, **kwargs) -> CreditNote:
        cn = CreditNote(**kwargs)
        self.session.add(cn)
        await self.session.flush()
        return cn

    async def list_by_invoice(self, invoice_id: str) -> Sequence[CreditNote]:
        return (
            await self.session.execute(
                select(CreditNote).where(CreditNote.invoice_id == invoice_id)
            )
        ).scalars().all()

    async def list_by_tenant(self, tenant_id: str) -> Sequence[CreditNote]:
        return (
            await self.session.execute(
                select(CreditNote)
                .where(CreditNote.tenant_id == tenant_id)
                .order_by(CreditNote.issued_at.desc())
            )
        ).scalars().all()


class PaymentRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, payment_id: str) -> Optional[Payment]:
        return await self.session.get(Payment, payment_id)

    async def get_by_provider_event_id(self, provider_event_id: str) -> Optional[Payment]:
        return (
            await self.session.execute(
                select(Payment).where(Payment.provider_event_id == provider_event_id)
            )
        ).scalar_one_or_none()

    async def list_by_tenant(
        self,
        tenant_id: str,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[Sequence[Payment], int]:
        stmt = select(Payment).where(Payment.tenant_id == tenant_id)
        count_stmt = select(func.count()).select_from(Payment).where(
            Payment.tenant_id == tenant_id
        )
        total = (await self.session.execute(count_stmt)).scalar_one()
        stmt = (
            stmt.order_by(Payment.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        items = (await self.session.execute(stmt)).scalars().all()
        return items, total

    async def list_by_invoice(self, invoice_id: str) -> Sequence[Payment]:
        return (
            await self.session.execute(
                select(Payment).where(Payment.invoice_id == invoice_id)
            )
        ).scalars().all()

    async def create(self, **kwargs) -> Payment:
        pay = Payment(**kwargs)
        self.session.add(pay)
        await self.session.flush()
        return pay

    async def update(self, payment: Payment, **kwargs) -> Payment:
        for key, value in kwargs.items():
            if hasattr(payment, key):
                setattr(payment, key, value)
        await self.session.flush()
        return payment


class PaymentProviderCustomerRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, tenant_id: str, provider: str) -> Optional[PaymentProviderCustomer]:
        return (
            await self.session.execute(
                select(PaymentProviderCustomer).where(
                    PaymentProviderCustomer.tenant_id == tenant_id,
                    PaymentProviderCustomer.provider == provider,
                )
            )
        ).scalar_one_or_none()

    async def create(self, **kwargs) -> PaymentProviderCustomer:
        row = PaymentProviderCustomer(**kwargs)
        self.session.add(row)
        await self.session.flush()
        return row


class DelinquencyLogRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def append(self, **kwargs) -> DelinquencyLog:
        row = DelinquencyLog(**kwargs)
        self.session.add(row)
        await self.session.flush()
        return row

    async def list_by_tenant(self, tenant_id: str) -> Sequence[DelinquencyLog]:
        return (
            await self.session.execute(
                select(DelinquencyLog)
                .where(DelinquencyLog.tenant_id == tenant_id)
                .order_by(DelinquencyLog.created_at.desc())
            )
        ).scalars().all()


# ── Phase 5: support repos ───────────────────────────────────────────


class SupportTicketRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, ticket_id: str) -> Optional[SupportTicket]:
        return await self.session.get(SupportTicket, ticket_id)

    async def next_ticket_number(self, tenant_id: str) -> int:
        result = await self.session.execute(
            select(func.max(SupportTicket.ticket_number)).where(
                SupportTicket.tenant_id == tenant_id,
            )
        )
        current = result.scalar_one() or 0
        return current + 1

    async def list_by_tenant(
        self,
        tenant_id: str,
        status: Optional[str] = None,
        severity: Optional[str] = None,
        search: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[Sequence[SupportTicket], int]:
        stmt = select(SupportTicket).where(SupportTicket.tenant_id == tenant_id)
        count_stmt = select(func.count()).select_from(SupportTicket).where(
            SupportTicket.tenant_id == tenant_id,
        )
        if status:
            stmt = stmt.where(SupportTicket.status == status)
            count_stmt = count_stmt.where(SupportTicket.status == status)
        if severity:
            stmt = stmt.where(SupportTicket.severity == severity)
            count_stmt = count_stmt.where(SupportTicket.severity == severity)
        if search:
            pattern = f"%{search}%"
            flt = SupportTicket.subject.ilike(pattern)
            stmt = stmt.where(flt)
            count_stmt = count_stmt.where(flt)
        total = (await self.session.execute(count_stmt)).scalar_one()
        stmt = (
            stmt.order_by(SupportTicket.updated_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        items = (await self.session.execute(stmt)).scalars().all()
        return items, total

    async def list_all(
        self,
        status: Optional[str] = None,
        severity: Optional[str] = None,
        assigned_to: Optional[str] = None,
        search: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[Sequence[SupportTicket], int]:
        stmt = select(SupportTicket)
        count_stmt = select(func.count()).select_from(SupportTicket)
        if status:
            stmt = stmt.where(SupportTicket.status == status)
            count_stmt = count_stmt.where(SupportTicket.status == status)
        if severity:
            stmt = stmt.where(SupportTicket.severity == severity)
            count_stmt = count_stmt.where(SupportTicket.severity == severity)
        if assigned_to:
            stmt = stmt.where(SupportTicket.assigned_to == assigned_to)
            count_stmt = count_stmt.where(SupportTicket.assigned_to == assigned_to)
        if search:
            pattern = f"%{search}%"
            flt = SupportTicket.subject.ilike(pattern)
            stmt = stmt.where(flt)
            count_stmt = count_stmt.where(flt)
        total = (await self.session.execute(count_stmt)).scalar_one()
        stmt = (
            stmt.order_by(SupportTicket.updated_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        items = (await self.session.execute(stmt)).scalars().all()
        return items, total

    async def create(self, **kwargs) -> SupportTicket:
        ticket = SupportTicket(**kwargs)
        self.session.add(ticket)
        await self.session.flush()
        return ticket

    async def update(self, ticket: SupportTicket, **kwargs) -> SupportTicket:
        for key, value in kwargs.items():
            if hasattr(ticket, key):
                setattr(ticket, key, value)
        ticket.updated_at = utcnow()
        await self.session.flush()
        return ticket

    async def count_open(self, tenant_id: Optional[str] = None) -> int:
        stmt = select(func.count()).select_from(SupportTicket).where(
            SupportTicket.status.in_(["open", "acknowledged", "in_progress"]),
        )
        if tenant_id:
            stmt = stmt.where(SupportTicket.tenant_id == tenant_id)
        return (await self.session.execute(stmt)).scalar_one()


class TicketMessageRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, **kwargs) -> TicketMessage:
        msg = TicketMessage(**kwargs)
        self.session.add(msg)
        await self.session.flush()
        return msg

    async def list_by_ticket(
        self,
        ticket_id: str,
        include_internal: bool = False,
    ) -> Sequence[TicketMessage]:
        stmt = select(TicketMessage).where(TicketMessage.ticket_id == ticket_id)
        if not include_internal:
            stmt = stmt.where(TicketMessage.is_internal == False)  # noqa: E712
        return (
            await self.session.execute(stmt.order_by(TicketMessage.created_at))
        ).scalars().all()


class TicketStateChangeRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def append(self, **kwargs) -> TicketStateChange:
        row = TicketStateChange(**kwargs)
        self.session.add(row)
        await self.session.flush()
        return row

    async def list_by_ticket(self, ticket_id: str) -> Sequence[TicketStateChange]:
        return (
            await self.session.execute(
                select(TicketStateChange)
                .where(TicketStateChange.ticket_id == ticket_id)
                .order_by(TicketStateChange.changed_at)
            )
        ).scalars().all()


#: One grant duration, one definition — the endpoint docstring, DEMO.md
#: and the UI derive from expires_at rather than restating "24".
SUPPORT_ACCESS_TTL_HOURS = 24


class SupportAccessLogRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, entry_id: str) -> Optional[SupportAccessLog]:
        return await self.session.get(SupportAccessLog, entry_id)

    async def get_active(
        self, tenant_id: str, user_id: Optional[str] = None,
    ) -> Optional[SupportAccessLog]:
        """The live grant for *tenant_id* — bound to its requester.

        Red-team finding: a tenant-scoped lookup meant ONE approval
        admitted EVERY platform_support engineer for 24h. A grant is for
        the person who requested it (Vinod's model: request -> approved ->
        specific tenant, per person), so callers gating access pass their
        user_id and only that requester's grant admits them. user_id=None
        keeps the tenant-wide view for status/duplicate checks.
        """
        now = utcnow()
        stmt = select(SupportAccessLog).where(
            SupportAccessLog.tenant_id == tenant_id,
            # Approval is the gate. Without this clause a merely
            # REQUESTED row would satisfy tenant_scope and asking
            # for access would be the same as being granted it.
            SupportAccessLog.status == "approved",
            SupportAccessLog.expires_at.is_not(None),
            SupportAccessLog.expires_at > now,
            SupportAccessLog.revoked_at.is_(None),
        )
        if user_id is not None:
            stmt = stmt.where(SupportAccessLog.requested_by == user_id)
        stmt = stmt.order_by(SupportAccessLog.approved_at.desc())
        return (await self.session.execute(stmt)).scalars().first()

    async def get_pending(
        self, tenant_id: str, requested_by: Optional[str] = None,
    ) -> Optional[SupportAccessLog]:
        """An outstanding request, so asking twice does not queue twice.

        Per-requester when *requested_by* is given: each engineer requests
        for themselves, so engineer A's pending request must not block
        engineer B's. A partial unique index enforces the invariant the
        old read-then-insert check only hoped for.
        """
        stmt = select(SupportAccessLog).where(
            SupportAccessLog.tenant_id == tenant_id,
            SupportAccessLog.status == "requested",
        )
        if requested_by is not None:
            stmt = stmt.where(SupportAccessLog.requested_by == requested_by)
        stmt = stmt.order_by(SupportAccessLog.requested_at.asc())
        return (await self.session.execute(stmt)).scalars().first()

    async def list_pending(self) -> Sequence[SupportAccessLog]:
        """The approver's queue, across every tenant."""
        return (
            await self.session.execute(
                select(SupportAccessLog)
                .where(SupportAccessLog.status == "requested")
                .order_by(SupportAccessLog.requested_at.asc())
            )
        ).scalars().all()

    async def denial_history(
        self, tenant_id: str, requested_by: str,
    ) -> tuple[int, Optional[SupportAccessLog]]:
        """(count, most recent) of this engineer's denials for this tenant.

        A14 (OQ-25): denial is non-final — re-request is allowed — but the
        approver must SEE the history at decision time. The audit chain
        stays the durable record; this is the decision-time read.
        """
        rows = (
            await self.session.execute(
                select(SupportAccessLog)
                .where(
                    SupportAccessLog.tenant_id == tenant_id,
                    SupportAccessLog.requested_by == requested_by,
                    SupportAccessLog.status == "denied",
                )
                .order_by(SupportAccessLog.denied_at.desc())
            )
        ).scalars().all()
        return len(rows), (rows[0] if rows else None)

    async def request(
        self, *, tenant_id: str, requested_by: str, reason: str = "",
    ) -> SupportAccessLog:
        """Raise a request. It grants nothing until someone approves it."""
        row = SupportAccessLog(
            tenant_id=tenant_id,
            status="requested",
            requested_by=requested_by,
            reason=reason or None,
            # No clock until approval — expires_at is set when granted.
            enabled_by="",
            expires_at=None,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def approve(
        self, entry: SupportAccessLog, approved_by: str,
        ttl_hours: int = SUPPORT_ACCESS_TTL_HOURS,
    ) -> SupportAccessLog:
        now = utcnow()
        entry.status = "approved"
        entry.approved_by = approved_by
        entry.approved_at = now
        entry.enabled_by = approved_by
        entry.enabled_at = now
        # The clock starts at approval, not at request: a request that sat
        # in the queue overnight must not arrive already half spent.
        entry.expires_at = now + timedelta(hours=ttl_hours)
        await self.session.flush()
        return entry

    async def deny(self, entry: SupportAccessLog, denied_by: str) -> SupportAccessLog:
        entry.status = "denied"
        entry.denied_by = denied_by
        entry.denied_at = utcnow()
        await self.session.flush()
        return entry

    async def revoke(self, entry: SupportAccessLog, revoked_by: str) -> SupportAccessLog:
        entry.status = "revoked"
        entry.revoked_at = utcnow()
        entry.revoked_by = revoked_by
        await self.session.flush()
        return entry

    async def list_by_tenant(self, tenant_id: str) -> Sequence[SupportAccessLog]:
        return (
            await self.session.execute(
                select(SupportAccessLog)
                .where(SupportAccessLog.tenant_id == tenant_id)
                .order_by(SupportAccessLog.enabled_at.desc())
            )
        ).scalars().all()


# ── Phase 7: API keys + impersonation repos ──────────────────────────


class ApiKeyRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, key_id: str) -> Optional[ApiKey]:
        return await self.session.get(ApiKey, key_id)

    async def get_by_hash(self, key_hash: str) -> Optional[ApiKey]:
        return (
            await self.session.execute(
                select(ApiKey).where(ApiKey.key_hash == key_hash)
            )
        ).scalar_one_or_none()

    async def list_by_tenant(
        self,
        tenant_id: str,
        status: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[Sequence[ApiKey], int]:
        stmt = select(ApiKey).where(ApiKey.tenant_id == tenant_id)
        count_stmt = select(func.count()).select_from(ApiKey).where(
            ApiKey.tenant_id == tenant_id,
        )
        if status:
            stmt = stmt.where(ApiKey.status == status)
            count_stmt = count_stmt.where(ApiKey.status == status)
        total = (await self.session.execute(count_stmt)).scalar_one()
        stmt = (
            stmt.order_by(ApiKey.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        items = (await self.session.execute(stmt)).scalars().all()
        return items, total

    async def create(self, **kwargs) -> ApiKey:
        key = ApiKey(**kwargs)
        self.session.add(key)
        await self.session.flush()
        return key

    async def update(self, key: ApiKey, **kwargs) -> ApiKey:
        for k, v in kwargs.items():
            if hasattr(key, k):
                setattr(key, k, v)
        await self.session.flush()
        return key

    async def revoke(self, key: ApiKey) -> ApiKey:
        key.status = "revoked"
        key.revoked_at = utcnow()
        await self.session.flush()
        return key


class ImpersonationLogRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, **kwargs) -> ImpersonationLog:
        row = ImpersonationLog(**kwargs)
        self.session.add(row)
        await self.session.flush()
        return row

    async def get_by_id(self, log_id: str) -> Optional[ImpersonationLog]:
        return await self.session.get(ImpersonationLog, log_id)

    async def end_session(self, entry: ImpersonationLog) -> ImpersonationLog:
        entry.ended_at = utcnow()
        await self.session.flush()
        return entry

    async def list_filtered(
        self,
        admin_user_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[Sequence[ImpersonationLog], int]:
        stmt = select(ImpersonationLog)
        count_stmt = select(func.count()).select_from(ImpersonationLog)
        if admin_user_id:
            stmt = stmt.where(ImpersonationLog.admin_user_id == admin_user_id)
            count_stmt = count_stmt.where(ImpersonationLog.admin_user_id == admin_user_id)
        if tenant_id:
            stmt = stmt.where(ImpersonationLog.tenant_id == tenant_id)
            count_stmt = count_stmt.where(ImpersonationLog.tenant_id == tenant_id)
        if date_from:
            stmt = stmt.where(ImpersonationLog.started_at >= date_from)
            count_stmt = count_stmt.where(ImpersonationLog.started_at >= date_from)
        if date_to:
            stmt = stmt.where(ImpersonationLog.started_at <= date_to)
            count_stmt = count_stmt.where(ImpersonationLog.started_at <= date_to)
        total = (await self.session.execute(count_stmt)).scalar_one()
        stmt = (
            stmt.order_by(ImpersonationLog.started_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        items = (await self.session.execute(stmt)).scalars().all()
        return items, total


class MarketplaceRepo:
    """Skill marketplace persistence (R4-3 P17)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, entry_id: str) -> Optional[MarketplaceSkill]:
        return await self.session.get(MarketplaceSkill, entry_id)

    async def get_by_name_version(
        self, skill_name: str, version: int
    ) -> Optional[MarketplaceSkill]:
        return (
            await self.session.execute(
                select(MarketplaceSkill).where(
                    MarketplaceSkill.skill_name == skill_name,
                    MarketplaceSkill.version == version,
                )
            )
        ).scalar_one_or_none()

    async def submit(
        self,
        skill_name: str,
        version: int,
        yaml_content: str,
        author_email: str = "",
        tenant_id: Optional[str] = None,
        description: str = "",
        target: str = "",
        tier: str = "community",
        validation_report: Optional[dict] = None,
    ) -> MarketplaceSkill:
        entry = MarketplaceSkill(
            skill_name=skill_name,
            version=version,
            yaml_content=yaml_content,
            author_email=author_email,
            tenant_id=tenant_id,
            description=description[:512],
            target=target,
            tier=tier,
            validation_report=validation_report,
        )
        self.session.add(entry)
        await self.session.flush()
        return entry

    async def list_filtered(
        self,
        tier: Optional[str] = None,
        review_status: Optional[str] = None,
        published: Optional[bool] = None,
        target: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[Sequence[MarketplaceSkill], int]:
        stmt = select(MarketplaceSkill)
        count_stmt = select(func.count()).select_from(MarketplaceSkill)
        if tier:
            stmt = stmt.where(MarketplaceSkill.tier == tier)
            count_stmt = count_stmt.where(MarketplaceSkill.tier == tier)
        if review_status:
            stmt = stmt.where(MarketplaceSkill.review_status == review_status)
            count_stmt = count_stmt.where(
                MarketplaceSkill.review_status == review_status
            )
        if published is not None:
            stmt = stmt.where(MarketplaceSkill.published == published)
            count_stmt = count_stmt.where(MarketplaceSkill.published == published)
        if target:
            stmt = stmt.where(MarketplaceSkill.target == target)
            count_stmt = count_stmt.where(MarketplaceSkill.target == target)
        total = (await self.session.execute(count_stmt)).scalar_one()
        stmt = (
            stmt.order_by(MarketplaceSkill.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        items = (await self.session.execute(stmt)).scalars().all()
        return items, total

    async def review(
        self,
        entry: MarketplaceSkill,
        approve: bool,
        reviewer_email: str,
        reason: str = "",
    ) -> MarketplaceSkill:
        entry.review_status = "approved" if approve else "rejected"
        entry.published = approve
        entry.reviewed_by = reviewer_email
        entry.reviewed_at = utcnow()
        entry.rejection_reason = "" if approve else reason[:512]
        entry.updated_at = utcnow()
        await self.session.flush()
        return entry

    async def record_stats(
        self,
        entry: MarketplaceSkill,
        successes: int = 0,
        failures: int = 0,
        devices: int = 0,
    ) -> MarketplaceSkill:
        entry.success_count += max(0, successes)
        entry.failure_count += max(0, failures)
        entry.total_executions += max(0, successes) + max(0, failures)
        entry.device_count = max(entry.device_count, devices)
        entry.updated_at = utcnow()
        await self.session.flush()
        return entry

    async def record_install(self, entry: MarketplaceSkill) -> MarketplaceSkill:
        entry.install_count += 1
        entry.updated_at = utcnow()
        await self.session.flush()
        return entry

    async def promote(self, entry: MarketplaceSkill) -> MarketplaceSkill:
        entry.tier = "verified"
        entry.promoted_at = utcnow()
        entry.updated_at = utcnow()
        await self.session.flush()
        return entry


class MarketplaceInstallRepo:
    """Install-event persistence (R5-2)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record(
        self, tenant_id: str, skill_entry_id: str, installed_by: str = ""
    ) -> MarketplaceInstall:
        install = MarketplaceInstall(
            tenant_id=tenant_id, skill_entry_id=skill_entry_id,
            installed_by=installed_by,
        )
        self.session.add(install)
        await self.session.flush()
        return install

    async def list_for_tenant(
        self, tenant_id: str, since: Optional[datetime] = None,
        limit: int = 500,
    ) -> Sequence[MarketplaceInstall]:
        stmt = (
            select(MarketplaceInstall)
            .where(MarketplaceInstall.tenant_id == tenant_id)
            .order_by(MarketplaceInstall.installed_at)
            .limit(limit)
        )
        if since is not None:
            stmt = stmt.where(MarketplaceInstall.installed_at > since)
        return (await self.session.execute(stmt)).scalars().all()


class TenantServiceRepo:
    """Tenant → service placement. Resolution is deliberately fail-closed."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def resolve(self, tenant_id: str, kind: str) -> Optional[TenantService]:
        """The tenant's active placement for *kind*, or None.

        Returning None is the whole point: callers must refuse the request
        rather than reach for a global default. A shared fallback endpoint
        would serve one tenant's data under another tenant's URL.
        """
        return (
            await self.session.execute(
                select(TenantService).where(
                    TenantService.tenant_id == tenant_id,
                    TenantService.service_kind == kind,
                    TenantService.status == "active",
                )
            )
        ).scalar_one_or_none()

    async def get_by_id(self, service_id: str) -> Optional[TenantService]:
        return await self.session.get(TenantService, service_id)

    async def list_by_tenant(self, tenant_id: str) -> Sequence[TenantService]:
        return (
            await self.session.execute(
                select(TenantService)
                .where(TenantService.tenant_id == tenant_id)
                .order_by(TenantService.service_kind, TenantService.registered_at.desc())
            )
        ).scalars().all()

    async def find_active_by_endpoint(self, endpoint_url: str) -> Optional[TenantService]:
        """The active placement bound to *endpoint_url*, any tenant, any kind.

        Backs the one-tenant-one-CC invariant: the API refuses to bind an
        endpoint that is already another tenant's stack.
        """
        return (
            await self.session.execute(
                select(TenantService).where(
                    TenantService.endpoint_url == endpoint_url,
                    TenantService.status == "active",
                )
            )
        ).scalars().first()

    async def register(
        self, *, tenant_id: str, service_kind: str, endpoint_url: str,
        registered_by: str = "",
    ) -> TenantService:
        """Register a placement, disabling any active one for the same kind.

        Re-registering is how a tenant gets moved to a new stack, so the
        previous row is retired rather than deleted — the history of where
        a tenant's data was served from is worth keeping.
        """
        existing = await self.resolve(tenant_id, service_kind)
        if existing is not None:
            await self.disable(existing)
        row = TenantService(
            tenant_id=tenant_id,
            service_kind=service_kind,
            endpoint_url=endpoint_url,
            registered_by=registered_by,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def disable(self, row: TenantService) -> TenantService:
        row.status = "disabled"
        row.updated_at = utcnow()
        await self.session.flush()
        return row

