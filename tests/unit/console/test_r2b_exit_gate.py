"""R2b exit gate: end-to-end billing lifecycle validation.

Tests the full lifecycle through the service layer without HTTP,
validating the business logic chain that the exit gate specifies.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import date, datetime, timedelta, timezone

import pytest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from harkeniq_console.billing.engine import BillingEngine
from harkeniq_console.billing.metering import MeteringService
from harkeniq_console.db.repos import (
    ApiKeyRepo,
    AuditRepo,
    CreditNoteRepo,
    DelinquencyLogRepo,
    FeatureFlagRepo,
    ImpersonationLogRepo,
    InvoiceLineRepo,
    InvoiceRepo,
    LicenseRepo,
    PaymentRepo,
    PriceBookRepo,
    SubscriptionRepo,
    SupportAccessLogRepo,
    SupportTicketRepo,
    TenantRepo,
    TicketMessageRepo,
    TicketStateChangeRepo,
    UsageEventRepo,
    UserRepo,
)


def utcnow():
    return datetime.now(timezone.utc)


@pytest.fixture
async def setup(session):
    """Seed full scenario: tenant, users, subscription, price book, usage."""
    # 1. Super admin creates tenant
    tenant = await TenantRepo(session).create(
        slug="acme-dc", name="Acme Data Centers",
        billing_country="US", currency="USD",
    )

    # 2. Create users with different roles
    owner = await UserRepo(session).create(
        tenant_id=tenant.id, email="owner@acme.com",
        display_name="Owner", role="tenant_owner",
    )
    operator = await UserRepo(session).create(
        tenant_id=tenant.id, email="ops@acme.com",
        display_name="Operator", role="operator",
    )
    viewer = await UserRepo(session).create(
        tenant_id=tenant.id, email="viewer@acme.com",
        display_name="Viewer", role="viewer",
    )
    auditor = await UserRepo(session).create(
        tenant_id=tenant.id, email="auditor@acme.com",
        display_name="Auditor", role="auditor",
    )

    # 3. Issue license
    license_ = await LicenseRepo(session).create(
        tenant_id=tenant.id, license_key="signed-key",
        fingerprint="fp-acme-dc", plan="approve", node_commit=200,
        valid_from=utcnow(), valid_until=utcnow() + timedelta(days=365),
        issued_by=owner.id,
    )

    # 4. Create subscription
    sub = await SubscriptionRepo(session).create(
        tenant_id=tenant.id, plan="approve", node_commit=200,
        billing_cycle_start=date(2026, 1, 1), billing_frequency="annual",
        price_book_version=1, license_id=license_.id,
    )

    # 5. Seed price book
    await PriceBookRepo(session).create(
        plan="approve", currency="USD", billing_interval="annual",
        node_price_cents=2400, version=1,
    )
    await PriceBookRepo(session).create(
        plan="approve", currency="USD", billing_interval="monthly",
        node_price_cents=3000, version=1,
    )

    # 6. Seed usage data (July 2026: peak at 220 nodes)
    usage_repo = UsageEventRepo(session)
    for day in range(1, 31):
        count = 180 + (day if day <= 20 else 40 - day)  # peaks at day 20: 200
        await usage_repo.record(
            tenant_id=tenant.id, site_name="dc-east",
            date=date(2026, 7, day), node_count=count,
        )
        await usage_repo.record(
            tenant_id=tenant.id, site_name="dc-west",
            date=date(2026, 7, day), node_count=20,  # total peak: 220
        )

    return {
        "tenant": tenant,
        "owner": owner,
        "operator": operator,
        "viewer": viewer,
        "auditor": auditor,
        "license": license_,
        "subscription": sub,
    }


# ── Exit gate tests ──────────────────────────────────────────────────


class TestR2bExitGate:
    """Validates the R2b exit gate criteria from the build plan."""

    async def test_01_tenant_created_with_subscription(self, session, setup):
        """Super admin creates tenant, subscription exists."""
        tenant = setup["tenant"]
        sub = await SubscriptionRepo(session).get_by_tenant(tenant.id)
        assert sub is not None
        assert sub.plan == "approve"
        assert sub.node_commit == 200

    async def test_02_license_issued(self, session, setup):
        """Tenant owner issues Ed25519 license key."""
        lic = setup["license"]
        assert lic.status == "active"
        assert lic.plan == "approve"
        assert lic.node_commit == 200

    async def test_03_usage_metered_high_water(self, session, setup):
        """Usage metered daily, high-water mark correctly calculated."""
        tenant = setup["tenant"]
        hw = await UsageEventRepo(session).get_high_water(
            tenant.id, date(2026, 7, 1), date(2026, 7, 31),
        )
        # dc-east peaks at 200 (day 20), dc-west constant at 20
        # But high_water is per-event max, not sum. So max single event = 200
        assert hw == 200

    async def test_04_commit_invoice_correct_math(self, session, setup):
        """Monthly invoice generated with correct commit math."""
        engine = BillingEngine()
        inv = await engine.generate_commit_invoice(session, setup["tenant"].id)
        # 200 nodes * 2400 cents * 12 months = 5,760,000 cents
        assert inv["total_cents"] == 200 * 2400 * 12
        assert inv["type"] == "commit"

    async def test_05_overage_invoice_correct_math(self, session, setup):
        """Overage invoice: high_water(200) - commit(200) = 0 overage."""
        engine = BillingEngine()
        inv = await engine.generate_overage_invoice(
            session, setup["tenant"].id,
            date(2026, 7, 1), date(2026, 7, 31),
        )
        # high_water 200 == commit 200, so no overage
        assert inv is None

    async def test_06_overage_when_exceeded(self, session, setup):
        """Add spike usage, verify overage invoice generates."""
        tenant = setup["tenant"]
        # Add a spike day
        await UsageEventRepo(session).record(
            tenant_id=tenant.id, site_name="dc-east",
            date=date(2026, 7, 15), node_count=250,
        )
        engine = BillingEngine()
        inv = await engine.generate_overage_invoice(
            session, tenant.id, date(2026, 7, 1), date(2026, 7, 31),
        )
        assert inv is not None
        # overage = 250 - 200 = 50 nodes * 2400 = 120,000 cents
        assert inv["total_cents"] == 50 * 2400
        assert inv["type"] == "overage"

    async def test_07_payment_marks_invoice_paid(self, session, setup):
        """Payment via gateway marks invoice paid."""
        engine = BillingEngine()
        inv = await engine.generate_commit_invoice(session, setup["tenant"].id)
        result = await engine.record_payment_and_restore(
            session, setup["tenant"].id, inv["id"],
            {"provider": "stripe", "provider_payment_id": "pi_test_123"},
        )
        assert result["status"] == "paid"
        assert result["paid_at"] is not None

    async def test_08_credit_note(self, session, setup):
        """Credit note applied correctly."""
        engine = BillingEngine()
        inv = await engine.generate_commit_invoice(session, setup["tenant"].id)
        cn = await engine.apply_credit_note(
            session, inv["id"], 10000, "Promo discount", "admin",
        )
        assert cn["amount_cents"] == 10000

    async def test_09_delinquency_overdue(self, session, setup):
        """Delinquency: overdue when invoice past due."""
        tenant = setup["tenant"]
        await InvoiceRepo(session).create(
            tenant_id=tenant.id, invoice_number="INV-DEL",
            type="commit", status="issued", currency="USD",
            subtotal_cents=5000, total_cents=5000,
            period_start=date(2026, 1, 1), period_end=date(2026, 12, 31),
            due_at=utcnow() - timedelta(days=5),
            issued_at=utcnow() - timedelta(days=35),
        )
        result = await BillingEngine().check_delinquency(session, tenant.id)
        assert result["status"] == "overdue"

    async def test_10_delinquency_restricted(self, session, setup):
        """Delinquency: restricted when 14+ days overdue."""
        tenant = setup["tenant"]
        await InvoiceRepo(session).create(
            tenant_id=tenant.id, invoice_number="INV-REST",
            type="commit", status="issued", currency="USD",
            subtotal_cents=5000, total_cents=5000,
            period_start=date(2026, 1, 1), period_end=date(2026, 12, 31),
            due_at=utcnow() - timedelta(days=20),
            issued_at=utcnow() - timedelta(days=50),
        )
        result = await BillingEngine().check_delinquency(session, tenant.id)
        assert result["status"] == "restricted"

    async def test_11_delinquency_payment_restores(self, session, setup):
        """Delinquency: payment received -> auto-restore."""
        tenant = setup["tenant"]
        await TenantRepo(session).update(tenant, delinquency_status="overdue")
        inv = await InvoiceRepo(session).create(
            tenant_id=tenant.id, invoice_number="INV-PAY-REST",
            type="commit", status="issued", currency="USD",
            subtotal_cents=5000, total_cents=5000,
            period_start=date(2026, 1, 1), period_end=date(2026, 12, 31),
            due_at=utcnow() - timedelta(days=5),
        )
        await BillingEngine().record_payment_and_restore(
            session, tenant.id, inv.id, {"provider": "manual"},
        )
        refreshed = await TenantRepo(session).get_by_id(tenant.id)
        assert refreshed.delinquency_status == "current"

    async def test_12_support_ticket_lifecycle(self, session, setup):
        """Support ticket created, replied, closed."""
        tenant = setup["tenant"]
        ticket = await SupportTicketRepo(session).create(
            tenant_id=tenant.id, ticket_number=1,
            subject="Server unreachable", severity="S2",
            component="Agent", created_by=setup["owner"].id,
            sla_due_at=utcnow() + timedelta(hours=8),
        )
        assert ticket.status == "open"

        await TicketMessageRepo(session).create(
            ticket_id=ticket.id, author_id=setup["owner"].id,
            author_email="owner@acme.com", body="Help needed",
        )
        await TicketMessageRepo(session).create(
            ticket_id=ticket.id, author_id="support1",
            author_email="support@harkeniq.com", body="Investigating",
        )
        await TicketMessageRepo(session).create(
            ticket_id=ticket.id, author_id="support1",
            author_email="support@harkeniq.com",
            body="Internal: checking BMC logs", is_internal=True,
        )

        # Tenant sees 2 messages (internal hidden)
        tenant_msgs = await TicketMessageRepo(session).list_by_ticket(
            ticket.id, include_internal=False,
        )
        assert len(tenant_msgs) == 2

        # Support sees 3
        all_msgs = await TicketMessageRepo(session).list_by_ticket(
            ticket.id, include_internal=True,
        )
        assert len(all_msgs) == 3

        # Close
        await SupportTicketRepo(session).update(ticket, status="closed", closed_at=utcnow())
        assert ticket.status == "closed"

    async def test_13_support_mode_24h(self, session, setup):
        """Support mode: enable 24h access, auto-expire concept."""
        tenant = setup["tenant"]
        now = utcnow()
        entry = await SupportAccessLogRepo(session).create(
            tenant_id=tenant.id, enabled_by="support1",
            enabled_at=now, expires_at=now + timedelta(hours=24),
        )
        active = await SupportAccessLogRepo(session).get_active(tenant.id)
        assert active is not None

        # Revoke
        await SupportAccessLogRepo(session).revoke(entry, "support1")
        active = await SupportAccessLogRepo(session).get_active(tenant.id)
        assert active is None

    async def test_14_audit_trail_complete(self, session, setup):
        """Audit trail captures billing, support, feature flag actions."""
        tenant = setup["tenant"]
        audit = AuditRepo(session)

        await audit.append(actor_id="system", actor_email="system", action="invoice.generated",
                          subject_type="invoice", subject_id="inv1", tenant_id=tenant.id)
        await audit.append(actor_id=setup["owner"].id, actor_email="owner@acme.com",
                          action="ticket.created", subject_type="ticket", subject_id="t1", tenant_id=tenant.id)
        await audit.append(actor_id="admin", actor_email="admin@h.com",
                          action="feature_flag.toggled", subject_type="feature_flag", subject_id="f1", tenant_id=tenant.id)

        entries, total = await audit.list_filtered(tenant_id=tenant.id)
        assert total == 3
        actions = {e.action for e in entries}
        assert "invoice.generated" in actions
        assert "ticket.created" in actions
        assert "feature_flag.toggled" in actions

    async def test_15_air_gapped_signed_upload(self, session, setup):
        """Air-gapped: signed usage report upload works."""
        tenant = setup["tenant"]

        private_key = Ed25519PrivateKey.generate()
        public_key_pem = private_key.public_key().public_bytes(
            Encoding.PEM, PublicFormat.SubjectPublicKeyInfo,
        ).decode()

        payload = json.dumps({
            "tenant_id": tenant.id,
            "period": "2026-08",
            "events": [
                {"site_name": "dc-airgap", "date": "2026-08-01", "node_count": 150},
                {"site_name": "dc-airgap", "date": "2026-08-02", "node_count": 160},
            ],
        }).encode()
        signature = private_key.sign(payload)

        result = await MeteringService().upload_signed_usage_report(
            session, tenant.id, signature + payload, public_key_pem,
        )
        assert result["events_recorded"] == 2

    async def test_16_api_key_lifecycle(self, session, setup):
        """API key: generate, use, revoke."""
        tenant = setup["tenant"]
        raw = f"hiq_{secrets.token_hex(16)}"
        key_hash = hashlib.sha256(raw.encode()).hexdigest()

        repo = ApiKeyRepo(session)
        key = await repo.create(
            tenant_id=tenant.id, name="CI Pipeline",
            key_hash=key_hash, key_prefix=raw[:12],
            scope="write", created_by=setup["owner"].id,
        )
        assert key.status == "active"

        # Lookup by hash (simulates API auth)
        found = await repo.get_by_hash(key_hash)
        assert found is not None
        assert found.id == key.id

        # Revoke
        await repo.revoke(key)
        assert key.status == "revoked"

    async def test_17_feature_flags(self, session, setup):
        """Feature flags: set global, override per-tenant."""
        tenant = setup["tenant"]
        repo = FeatureFlagRepo(session)

        # Global default
        from harkeniq_console.db.models import FeatureFlag
        global_flag = FeatureFlag(feature_name="autonomy_mode", enabled=False)
        session.add(global_flag)
        await session.flush()

        # Tenant override
        await repo.set_flag(tenant.id, "autonomy_mode", True, updated_by="admin")
        flag = await repo.get(tenant.id, "autonomy_mode")
        assert flag.enabled is True  # overridden

    async def test_18_impersonation_logging(self, session, setup):
        """Impersonation: session logged with audit trail."""
        tenant = setup["tenant"]
        repo = ImpersonationLogRepo(session)
        entry = await repo.create(
            admin_user_id="superadmin", admin_email="admin@harkeniq.com",
            tenant_id=tenant.id,
        )
        assert entry.ended_at is None

        await repo.end_session(entry)
        assert entry.ended_at is not None

        items, total = await repo.list_filtered(tenant_id=tenant.id)
        assert total == 1

    async def test_19_usage_summary_for_chargeback(self, session, setup):
        """Usage summary returns correct data for chargeback dashboard."""
        tenant = setup["tenant"]
        summary = await MeteringService().get_usage_summary(
            session, tenant.id, date(2026, 7, 1), date(2026, 7, 31),
        )
        assert summary["high_water"] == 200
        assert len(summary["per_site"]) == 2
        east = next(s for s in summary["per_site"] if s["site_name"] == "dc-east")
        assert east["peak_nodes"] == 200

    async def test_20_trueup_estimate(self, session, setup):
        """True-up estimate reflects current month usage."""
        tenant = setup["tenant"]
        # Record usage for current month
        now = utcnow()
        await UsageEventRepo(session).record(
            tenant_id=tenant.id, site_name="dc-east",
            date=now.date(), node_count=230,
        )
        est = await MeteringService().estimate_upcoming_trueup(session, tenant.id)
        assert est["committed"] == 200
        assert est["high_water_so_far"] == 230
        assert est["estimated_overage"] == 30
        assert est["estimated_amount_cents"] == 30 * 2400
