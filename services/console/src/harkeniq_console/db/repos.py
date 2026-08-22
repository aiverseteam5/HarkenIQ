"""Thin data-access helpers. Each repo wraps one AsyncSession.

Commit responsibility stays with the caller (one commit per API
request) so multi-table updates remain atomic.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_console.db.models import (
    ConsoleAuditLog,
    CreditNote,
    CustomRole,
    DelinquencyLog,
    FeatureFlag,
    Invoice,
    InvoiceLine,
    License,
    Payment,
    PaymentProviderCustomer,
    PlatformSetting,
    PriceBook,
    Subscription,
    SupportAccessLog,
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


class AuditRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

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
        row = ConsoleAuditLog(
            actor_id=actor_id,
            actor_email=actor_email,
            action=action,
            subject_type=subject_type,
            subject_id=subject_id,
            tenant_id=tenant_id,
            detail=detail,
        )
        self.session.add(row)
        await self.session.flush()
        return row

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


class SupportAccessLogRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, **kwargs) -> SupportAccessLog:
        row = SupportAccessLog(**kwargs)
        self.session.add(row)
        await self.session.flush()
        return row

    async def get_active(self, tenant_id: str) -> Optional[SupportAccessLog]:
        now = utcnow()
        return (
            await self.session.execute(
                select(SupportAccessLog).where(
                    SupportAccessLog.tenant_id == tenant_id,
                    SupportAccessLog.expires_at > now,
                    SupportAccessLog.revoked_at.is_(None),
                )
            )
        ).scalar_one_or_none()

    async def revoke(self, entry: SupportAccessLog, revoked_by: str) -> SupportAccessLog:
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
