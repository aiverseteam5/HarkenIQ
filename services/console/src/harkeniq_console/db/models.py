"""Console persistence models (SQLAlchemy 2.0, async).

JSON columns use JSONB on PostgreSQL and plain JSON elsewhere so the
same models run on Postgres (production) and aiosqlite (unit tests).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JSONVariant = JSON().with_variant(JSONB(), "postgresql")


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="active")
    billing_country: Mapped[str] = mapped_column(String(8), default="")
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    keycloak_realm: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    suspended_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    delinquency_status: Mapped[str] = mapped_column(String(32), default="current")


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str | None] = mapped_column(ForeignKey("tenants.id"), nullable=True)
    keycloak_user_id: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    email: Mapped[str] = mapped_column(String(320))
    display_name: Mapped[str] = mapped_column(String(255), default="")
    role: Mapped[str] = mapped_column(String(64))
    is_platform_user: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), default="invited")
    invited_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_users_tenant_id", "tenant_id"),
        Index("ix_users_email", "email"),
    )


class CustomRole(Base):
    __tablename__ = "custom_roles"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    name: Mapped[str] = mapped_column(String(128))
    permissions: Mapped[list | None] = mapped_column(JSONVariant, nullable=True)
    created_by: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("tenant_id", "name"),)


class UserCustomRole(Base):
    __tablename__ = "user_custom_roles"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    custom_role_id: Mapped[str] = mapped_column(ForeignKey("custom_roles.id"), primary_key=True)


class ConsoleAuditLog(Base):
    __tablename__ = "console_audit_log"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    actor_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    actor_email: Mapped[str] = mapped_column(String(320), default="")
    action: Mapped[str] = mapped_column(String(128))
    subject_type: Mapped[str] = mapped_column(String(64), default="")
    subject_id: Mapped[str] = mapped_column(String(64), default="")
    tenant_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)

    __table_args__ = (
        Index("ix_console_audit_ts", "ts"),
        Index("ix_console_audit_tenant_id", "tenant_id"),
    )


class PlatformSetting(Base):
    __tablename__ = "platform_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class License(Base):
    __tablename__ = "licenses"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    license_key: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True)
    plan: Mapped[str] = mapped_column(String(32))
    node_commit: Mapped[int] = mapped_column(Integer)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), default="active")
    issued_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_licenses_tenant_id", "tenant_id"),)


class LicenseKeypair(Base):
    __tablename__ = "license_keypairs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    public_key_pem: Mapped[str] = mapped_column(Text)
    private_key_pem_encrypted: Mapped[str] = mapped_column(Text)
    purpose: Mapped[str] = mapped_column(String(32), default="signing")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), unique=True)
    plan: Mapped[str] = mapped_column(String(32))
    node_commit: Mapped[int] = mapped_column(Integer)
    license_id: Mapped[str | None] = mapped_column(ForeignKey("licenses.id"), nullable=True)
    billing_cycle_start: Mapped[datetime] = mapped_column(Date)
    billing_frequency: Mapped[str] = mapped_column(String(16), default="annual")
    status: Mapped[str] = mapped_column(String(32), default="active")
    price_book_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TenantSite(Base):
    __tablename__ = "tenant_sites"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    site_name: Mapped[str] = mapped_column(String(255))
    cc_endpoint: Mapped[str] = mapped_column(String(512), default="")
    sm_endpoint: Mapped[str] = mapped_column(String(512), default="")
    status: Mapped[str] = mapped_column(String(32), default="active")
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("tenant_id", "site_name"),)


class FeatureFlag(Base):
    __tablename__ = "feature_flags"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str | None] = mapped_column(ForeignKey("tenants.id"), nullable=True)
    feature_name: Mapped[str] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("tenant_id", "feature_name"),)


# ── Phase 4: billing tables ──────────────────────────────────────────


class PriceBook(Base):
    __tablename__ = "price_book"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    plan: Mapped[str] = mapped_column(String(32))
    currency: Mapped[str] = mapped_column(String(3))
    billing_interval: Mapped[str] = mapped_column(String(16))
    node_price_cents: Mapped[int] = mapped_column(Integer)
    site_base_fee_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("plan", "currency", "billing_interval", "version"),
    )


class UsageEvent(Base):
    __tablename__ = "usage_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    site_name: Mapped[str] = mapped_column(String(255))
    date: Mapped[datetime] = mapped_column(Date)
    node_count: Mapped[int] = mapped_column(Integer)
    agent_versions: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="cc_report")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_usage_events_tenant_date", "tenant_id", "date"),)


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    invoice_number: Mapped[str] = mapped_column(String(64), unique=True)
    type: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="draft")
    currency: Mapped[str] = mapped_column(String(3))
    subtotal_cents: Mapped[int] = mapped_column(Integer, default=0)
    tax_cents: Mapped[int] = mapped_column(Integer, default=0)
    total_cents: Mapped[int] = mapped_column(Integer, default=0)
    issued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    period_start: Mapped[datetime] = mapped_column(Date)
    period_end: Mapped[datetime] = mapped_column(Date)
    payment_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider_payment_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_invoices_tenant_id", "tenant_id"),
        Index("ix_invoices_status", "status"),
    )


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id"))
    description: Mapped[str] = mapped_column(Text)
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price_cents: Mapped[int] = mapped_column(Integer)
    amount_cents: Mapped[int] = mapped_column(Integer)
    line_type: Mapped[str] = mapped_column(String(32))

    __table_args__ = (Index("ix_invoice_lines_invoice_id", "invoice_id"),)


class CreditNote(Base):
    __tablename__ = "credit_notes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id"))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    reason: Mapped[str] = mapped_column(Text)
    issued_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_credit_notes_invoice_id", "invoice_id"),)


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    invoice_id: Mapped[str | None] = mapped_column(ForeignKey("invoices.id"), nullable=True)
    provider: Mapped[str] = mapped_column(String(32))
    provider_payment_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    provider_customer_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refund_amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    provider_event_id: Mapped[str | None] = mapped_column(String(256), nullable=True, unique=True)

    __table_args__ = (
        Index("ix_payments_tenant_id", "tenant_id"),
        Index("ix_payments_invoice_id", "invoice_id"),
    )


class PaymentProviderCustomer(Base):
    __tablename__ = "payment_provider_customers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    provider: Mapped[str] = mapped_column(String(32))
    provider_customer_id: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("tenant_id", "provider"),)


class DelinquencyLog(Base):
    __tablename__ = "delinquency_log"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    from_state: Mapped[str] = mapped_column(String(32))
    to_state: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text, default="")
    actor_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_delinquency_log_tenant_id", "tenant_id"),)


# ── Phase 5: support + audit tables ──────────────────────────────────


class SupportTicket(Base):
    __tablename__ = "support_tickets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    ticket_number: Mapped[int] = mapped_column(Integer)
    subject: Mapped[str] = mapped_column(String(512))
    body: Mapped[str] = mapped_column(Text, default="")
    severity: Mapped[str] = mapped_column(String(8))  # S1 S2 S3 S4
    component: Mapped[str] = mapped_column(String(64), default="other")
    site_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="open")
    assigned_to: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_by: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sla_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_support_tickets_tenant_id", "tenant_id"),
        Index("ix_support_tickets_status", "status"),
        UniqueConstraint("tenant_id", "ticket_number"),
    )


class TicketMessage(Base):
    __tablename__ = "ticket_messages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    ticket_id: Mapped[str] = mapped_column(ForeignKey("support_tickets.id"))
    author_id: Mapped[str] = mapped_column(String(32))
    author_email: Mapped[str] = mapped_column(String(320), default="")
    body: Mapped[str] = mapped_column(Text)
    is_internal: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_ticket_messages_ticket_id", "ticket_id"),)


class TicketStateChange(Base):
    __tablename__ = "ticket_state_changes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    ticket_id: Mapped[str] = mapped_column(ForeignKey("support_tickets.id"))
    from_status: Mapped[str] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32))
    changed_by: Mapped[str] = mapped_column(String(32))
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_ticket_state_changes_ticket_id", "ticket_id"),)


class SupportAccessLog(Base):
    __tablename__ = "support_access_log"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    enabled_by: Mapped[str] = mapped_column(String(32))
    enabled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(String(32), nullable=True)

    __table_args__ = (Index("ix_support_access_log_tenant_id", "tenant_id"),)


# ── Phase 7: settings + API keys + impersonation ─────────────────────


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    name: Mapped[str] = mapped_column(String(255))
    key_hash: Mapped[str] = mapped_column(String(128))
    key_prefix: Mapped[str] = mapped_column(String(12))
    scope: Mapped[str] = mapped_column(String(32), default="read")
    status: Mapped[str] = mapped_column(String(32), default="active")
    created_by: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_api_keys_tenant_id", "tenant_id"),
        Index("ix_api_keys_key_hash", "key_hash"),
    )


class ImpersonationLog(Base):
    __tablename__ = "impersonation_log"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    admin_user_id: Mapped[str] = mapped_column(String(32))
    admin_email: Mapped[str] = mapped_column(String(320))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    actions_count: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        Index("ix_impersonation_log_tenant_id", "tenant_id"),
        Index("ix_impersonation_log_admin", "admin_user_id"),
    )
