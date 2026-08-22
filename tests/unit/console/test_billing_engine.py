"""Phase 4 billing engine and metering tests."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from harkeniq_console.billing.engine import BillingEngine
from harkeniq_console.billing.metering import MeteringService
from harkeniq_console.db.repos import (
    AuditRepo,
    DelinquencyLogRepo,
    InvoiceLineRepo,
    InvoiceRepo,
    PriceBookRepo,
    SubscriptionRepo,
    TenantRepo,
    UsageEventRepo,
)


def utcnow():
    return datetime.now(timezone.utc)


@pytest.fixture
async def tenant(session):
    return await TenantRepo(session).create(
        slug="acme", name="Acme Corp", billing_country="US", currency="USD",
    )


@pytest.fixture
async def subscription(session, tenant):
    return await SubscriptionRepo(session).create(
        tenant_id=tenant.id, plan="approve", node_commit=100,
        billing_cycle_start=date(2026, 1, 1), billing_frequency="annual",
        price_book_version=1,
    )


@pytest.fixture
async def quarterly_subscription(session, tenant):
    return await SubscriptionRepo(session).create(
        tenant_id=tenant.id, plan="approve", node_commit=100,
        billing_cycle_start=date(2026, 1, 1), billing_frequency="quarterly",
        price_book_version=1,
    )


@pytest.fixture
async def price_annual(session):
    return await PriceBookRepo(session).create(
        plan="approve", currency="USD", billing_interval="annual",
        node_price_cents=2400, version=1,
    )


@pytest.fixture
async def price_quarterly(session):
    return await PriceBookRepo(session).create(
        plan="approve", currency="USD", billing_interval="quarterly",
        node_price_cents=2700, version=1,
    )


@pytest.fixture
async def usage_data(session, tenant):
    repo = UsageEventRepo(session)
    for day in range(1, 16):
        await repo.record(
            tenant_id=tenant.id, site_name="dc-east",
            date=date(2026, 7, day), node_count=90 + day,
        )
    for day in range(1, 16):
        await repo.record(
            tenant_id=tenant.id, site_name="dc-west",
            date=date(2026, 7, day), node_count=50,
        )


# ── BillingEngine ────────────────────────────────────────────────────


class TestGenerateCommitInvoice:
    async def test_annual_amount(self, session, tenant, subscription, price_annual):
        engine = BillingEngine()
        inv = await engine.generate_commit_invoice(session, tenant.id)
        # 100 nodes * 2400 cents * 12 months = 2,880,000 cents
        assert inv["total_cents"] == 100 * 2400 * 12

    async def test_quarterly_amount(self, session, tenant, price_quarterly):
        # Need a quarterly subscription — can't use both fixtures since both
        # create a subscription for the same tenant. Create inline.
        await SubscriptionRepo(session).create(
            tenant_id=tenant.id, plan="approve", node_commit=100,
            billing_cycle_start=date(2026, 1, 1), billing_frequency="quarterly",
            price_book_version=1,
        )
        engine = BillingEngine()
        inv = await engine.generate_commit_invoice(session, tenant.id)
        # 100 nodes * 2700 cents * 3 months = 810,000 cents
        assert inv["total_cents"] == 100 * 2700 * 3

    async def test_invoice_number_format(self, session, tenant, subscription, price_annual):
        inv = await BillingEngine().generate_commit_invoice(session, tenant.id)
        assert inv["invoice_number"].startswith("INV-acme-202601-")

    async def test_status_issued(self, session, tenant, subscription, price_annual):
        inv = await BillingEngine().generate_commit_invoice(session, tenant.id)
        assert inv["status"] == "issued"

    async def test_type_commit(self, session, tenant, subscription, price_annual):
        inv = await BillingEngine().generate_commit_invoice(session, tenant.id)
        assert inv["type"] == "commit"

    async def test_period(self, session, tenant, subscription, price_annual):
        inv = await BillingEngine().generate_commit_invoice(session, tenant.id)
        assert inv["period_start"] == "2026-01-01"
        assert inv["period_end"] == "2027-01-01"

    async def test_line_item_created(self, session, tenant, subscription, price_annual):
        inv = await BillingEngine().generate_commit_invoice(session, tenant.id)
        lines = await InvoiceLineRepo(session).list_by_invoice(inv["id"])
        assert len(lines) == 1
        assert lines[0].line_type == "commit"
        assert lines[0].quantity == 100 * 12
        assert lines[0].unit_price_cents == 2400

    async def test_audit_log(self, session, tenant, subscription, price_annual):
        await BillingEngine().generate_commit_invoice(session, tenant.id)
        logs, _ = await AuditRepo(session).list_filtered(tenant_id=tenant.id, action="invoice.generated")
        assert len(logs) == 1

    async def test_missing_tenant(self, session):
        with pytest.raises(ValueError, match="tenant .* not found"):
            await BillingEngine().generate_commit_invoice(session, "nonexistent")

    async def test_missing_subscription(self, session, tenant):
        with pytest.raises(ValueError, match="no subscription"):
            await BillingEngine().generate_commit_invoice(session, tenant.id)

    async def test_missing_price_book(self, session, tenant, subscription):
        with pytest.raises(ValueError, match="no price_book"):
            await BillingEngine().generate_commit_invoice(session, tenant.id)


class TestGenerateOverageInvoice:
    async def test_generates_when_over_commit(self, session, tenant, subscription, price_annual, usage_data):
        engine = BillingEngine()
        inv = await engine.generate_overage_invoice(
            session, tenant.id, date(2026, 7, 1), date(2026, 7, 31),
        )
        assert inv is not None
        # high_water = 105 (90+15), commit = 100, overage = 5
        assert inv["type"] == "overage"
        assert inv["total_cents"] == 5 * 2400

    async def test_returns_none_no_overage(self, session, tenant, subscription, price_annual):
        repo = UsageEventRepo(session)
        for d in range(1, 5):
            await repo.record(
                tenant_id=tenant.id, site_name="dc-east",
                date=date(2026, 7, d), node_count=50,
            )
        inv = await BillingEngine().generate_overage_invoice(
            session, tenant.id, date(2026, 7, 1), date(2026, 7, 31),
        )
        assert inv is None

    async def test_returns_none_exact_commit(self, session, tenant, subscription, price_annual):
        await UsageEventRepo(session).record(
            tenant_id=tenant.id, site_name="dc-east",
            date=date(2026, 7, 1), node_count=100,
        )
        inv = await BillingEngine().generate_overage_invoice(
            session, tenant.id, date(2026, 7, 1), date(2026, 7, 31),
        )
        assert inv is None

    async def test_overage_line_type(self, session, tenant, subscription, price_annual, usage_data):
        inv = await BillingEngine().generate_overage_invoice(
            session, tenant.id, date(2026, 7, 1), date(2026, 7, 31),
        )
        lines = await InvoiceLineRepo(session).list_by_invoice(inv["id"])
        assert lines[0].line_type == "overage"

    async def test_missing_tenant(self, session):
        with pytest.raises(ValueError):
            await BillingEngine().generate_overage_invoice(
                session, "nope", date(2026, 7, 1), date(2026, 7, 31),
            )


class TestApplyCreditNote:
    async def test_creates_credit_note(self, session, tenant):
        inv = await InvoiceRepo(session).create(
            tenant_id=tenant.id, invoice_number="INV-CN",
            type="commit", currency="USD", subtotal_cents=5000, total_cents=5000,
            period_start=date(2026, 1, 1), period_end=date(2026, 12, 31),
        )
        cn = await BillingEngine().apply_credit_note(
            session, inv.id, 1000, "Correction", "admin",
        )
        assert cn["amount_cents"] == 1000
        assert cn["reason"] == "Correction"

    async def test_rejects_nonexistent_invoice(self, session):
        with pytest.raises(ValueError, match="not found"):
            await BillingEngine().apply_credit_note(session, "nope", 100, "r", "a")

    async def test_rejects_exceeding_amount(self, session, tenant):
        inv = await InvoiceRepo(session).create(
            tenant_id=tenant.id, invoice_number="INV-CN2",
            type="commit", currency="USD", subtotal_cents=1000, total_cents=1000,
            period_start=date(2026, 1, 1), period_end=date(2026, 12, 31),
        )
        with pytest.raises(ValueError, match="exceeds"):
            await BillingEngine().apply_credit_note(session, inv.id, 2000, "too much", "a")

    async def test_rejects_zero_amount(self, session, tenant):
        inv = await InvoiceRepo(session).create(
            tenant_id=tenant.id, invoice_number="INV-CN3",
            type="commit", currency="USD", subtotal_cents=1000, total_cents=1000,
            period_start=date(2026, 1, 1), period_end=date(2026, 12, 31),
        )
        with pytest.raises(ValueError, match="positive"):
            await BillingEngine().apply_credit_note(session, inv.id, 0, "zero", "a")


class TestDelinquencyStateMachine:
    async def test_current_when_no_overdue(self, session, tenant):
        result = await BillingEngine().check_delinquency(session, tenant.id)
        assert result["status"] == "current"
        assert result["days_overdue"] == 0

    async def test_transitions_to_overdue(self, session, tenant):
        await InvoiceRepo(session).create(
            tenant_id=tenant.id, invoice_number="INV-OD",
            type="commit", status="issued", currency="USD",
            subtotal_cents=5000, total_cents=5000,
            period_start=date(2026, 1, 1), period_end=date(2026, 12, 31),
            due_at=utcnow() - timedelta(days=3),
            issued_at=utcnow() - timedelta(days=33),
        )
        result = await BillingEngine().check_delinquency(session, tenant.id)
        assert result["status"] == "overdue"

    async def test_transitions_to_restricted(self, session, tenant):
        await InvoiceRepo(session).create(
            tenant_id=tenant.id, invoice_number="INV-R",
            type="commit", status="issued", currency="USD",
            subtotal_cents=5000, total_cents=5000,
            period_start=date(2026, 1, 1), period_end=date(2026, 12, 31),
            due_at=utcnow() - timedelta(days=20),
            issued_at=utcnow() - timedelta(days=50),
        )
        result = await BillingEngine().check_delinquency(session, tenant.id)
        assert result["status"] == "restricted"

    async def test_auto_restore(self, session, tenant):
        # Set tenant to overdue first
        await TenantRepo(session).update(tenant, delinquency_status="overdue")
        # No overdue invoices -> auto-restore
        result = await BillingEngine().check_delinquency(session, tenant.id)
        assert result["status"] == "current"

    async def test_no_redundant_transition(self, session, tenant):
        await InvoiceRepo(session).create(
            tenant_id=tenant.id, invoice_number="INV-NR",
            type="commit", status="issued", currency="USD",
            subtotal_cents=5000, total_cents=5000,
            period_start=date(2026, 1, 1), period_end=date(2026, 12, 31),
            due_at=utcnow() - timedelta(days=3),
        )
        await TenantRepo(session).update(tenant, delinquency_status="overdue")
        result = await BillingEngine().check_delinquency(session, tenant.id)
        assert result["status"] == "overdue"
        # Should not have logged a new transition
        logs = await DelinquencyLogRepo(session).list_by_tenant(tenant.id)
        assert len(logs) == 0

    async def test_delinquency_log_created(self, session, tenant):
        await InvoiceRepo(session).create(
            tenant_id=tenant.id, invoice_number="INV-DL",
            type="commit", status="issued", currency="USD",
            subtotal_cents=5000, total_cents=5000,
            period_start=date(2026, 1, 1), period_end=date(2026, 12, 31),
            due_at=utcnow() - timedelta(days=3),
        )
        await BillingEngine().check_delinquency(session, tenant.id)
        logs = await DelinquencyLogRepo(session).list_by_tenant(tenant.id)
        assert len(logs) == 1
        assert logs[0].from_state == "current"
        assert logs[0].to_state == "overdue"

    async def test_missing_tenant(self, session):
        with pytest.raises(ValueError):
            await BillingEngine().check_delinquency(session, "nope")


class TestRecordPaymentAndRestore:
    async def test_marks_invoice_paid(self, session, tenant):
        inv = await InvoiceRepo(session).create(
            tenant_id=tenant.id, invoice_number="INV-PAY",
            type="commit", status="issued", currency="USD",
            subtotal_cents=5000, total_cents=5000,
            period_start=date(2026, 1, 1), period_end=date(2026, 12, 31),
            due_at=utcnow() - timedelta(days=3),
        )
        result = await BillingEngine().record_payment_and_restore(
            session, tenant.id, inv.id,
            {"provider": "stripe", "provider_payment_id": "pi_123"},
        )
        assert result["status"] == "paid"
        assert result["paid_at"] is not None

    async def test_auto_restores_delinquency(self, session, tenant):
        await TenantRepo(session).update(tenant, delinquency_status="overdue")
        inv = await InvoiceRepo(session).create(
            tenant_id=tenant.id, invoice_number="INV-REST",
            type="commit", status="issued", currency="USD",
            subtotal_cents=5000, total_cents=5000,
            period_start=date(2026, 1, 1), period_end=date(2026, 12, 31),
            due_at=utcnow() - timedelta(days=3),
        )
        await BillingEngine().record_payment_and_restore(
            session, tenant.id, inv.id, {"provider": "manual"},
        )
        refreshed = await TenantRepo(session).get_by_id(tenant.id)
        assert refreshed.delinquency_status == "current"


class TestGenerateAllTrueups:
    async def test_generates_for_active_subs(self, session, tenant, subscription, price_annual):
        repo = UsageEventRepo(session)
        now = datetime.now(timezone.utc)
        first_of_month = now.replace(day=1)
        prev_end = first_of_month - timedelta(days=1)
        prev_start = prev_end.replace(day=1)
        # Record usage above commit in previous month
        for d in range(1, 5):
            await repo.record(
                tenant_id=tenant.id, site_name="dc-east",
                date=date(prev_start.year, prev_start.month, d), node_count=120,
            )
        generated = await BillingEngine().generate_all_trueups(session)
        assert len(generated) == 1
        assert generated[0]["type"] == "overage"

    async def test_skips_no_overage(self, session, tenant, subscription, price_annual):
        # No usage data -> no overage
        generated = await BillingEngine().generate_all_trueups(session)
        assert len(generated) == 0


# ── MeteringService ──────────────────────────────────────────────────


class TestMeteringRecordUsage:
    async def test_record_event(self, session, tenant):
        result = await MeteringService().record_usage_event(
            session, tenant.id, "dc-east", date(2026, 7, 1), 100,
        )
        assert result["node_count"] == 100

    async def test_ingest_batch(self, session, tenant):
        events = [
            {"site_name": "dc-east", "date": date(2026, 7, d), "node_count": 100}
            for d in range(1, 6)
        ]
        count = await MeteringService().ingest_usage_batch(session, tenant.id, events)
        assert count == 5


class TestMeteringUsageSummary:
    async def test_returns_high_water(self, session, tenant, usage_data):
        summary = await MeteringService().get_usage_summary(
            session, tenant.id, date(2026, 7, 1), date(2026, 7, 31),
        )
        assert summary["high_water"] == 105  # 90+15

    async def test_returns_daily_counts(self, session, tenant, usage_data):
        summary = await MeteringService().get_usage_summary(
            session, tenant.id, date(2026, 7, 1), date(2026, 7, 31),
        )
        assert len(summary["daily_counts"]) == 30  # 15 east + 15 west

    async def test_returns_per_site(self, session, tenant, usage_data):
        summary = await MeteringService().get_usage_summary(
            session, tenant.id, date(2026, 7, 1), date(2026, 7, 31),
        )
        assert len(summary["per_site"]) == 2

    async def test_empty_period(self, session, tenant):
        summary = await MeteringService().get_usage_summary(
            session, tenant.id, date(2026, 8, 1), date(2026, 8, 31),
        )
        assert summary["high_water"] == 0
        assert len(summary["daily_counts"]) == 0


class TestMeteringEstimate:
    async def test_estimate_with_overage(self, session, tenant, subscription, price_annual):
        repo = UsageEventRepo(session)
        now = datetime.now(timezone.utc)
        await repo.record(
            tenant_id=tenant.id, site_name="dc-east",
            date=now.date(), node_count=120,
        )
        est = await MeteringService().estimate_upcoming_trueup(session, tenant.id)
        assert est["high_water_so_far"] == 120
        assert est["committed"] == 100
        assert est["estimated_overage"] == 20
        assert est["estimated_amount_cents"] == 20 * 2400
        assert est["currency"] == "USD"

    async def test_estimate_no_overage(self, session, tenant, subscription, price_annual):
        repo = UsageEventRepo(session)
        now = datetime.now(timezone.utc)
        await repo.record(
            tenant_id=tenant.id, site_name="dc-east",
            date=now.date(), node_count=50,
        )
        est = await MeteringService().estimate_upcoming_trueup(session, tenant.id)
        assert est["estimated_overage"] == 0
        assert est["estimated_amount_cents"] == 0

    async def test_estimate_no_subscription(self, session, tenant):
        est = await MeteringService().estimate_upcoming_trueup(session, tenant.id)
        assert est["committed"] == 0
        assert est["estimated_overage"] == 0

    async def test_estimate_missing_tenant(self, session):
        with pytest.raises(ValueError):
            await MeteringService().estimate_upcoming_trueup(session, "nope")


class TestMeteringSignedUpload:
    async def test_valid_upload(self, session, tenant):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import (
            Encoding, NoEncryption, PrivateFormat, PublicFormat,
        )

        private_key = Ed25519PrivateKey.generate()
        public_key_pem = private_key.public_key().public_bytes(
            Encoding.PEM, PublicFormat.SubjectPublicKeyInfo,
        ).decode()

        payload = json.dumps({
            "tenant_id": tenant.id,
            "period": "2026-07",
            "events": [
                {"site_name": "dc-airgap", "date": "2026-07-01", "node_count": 80},
                {"site_name": "dc-airgap", "date": "2026-07-02", "node_count": 85},
            ],
        }).encode()

        signature = private_key.sign(payload)
        report = signature + payload

        result = await MeteringService().upload_signed_usage_report(
            session, tenant.id, report, public_key_pem,
        )
        assert result["events_recorded"] == 2
        assert result["period"] == "2026-07"

    async def test_invalid_signature(self, session, tenant):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import (
            Encoding, PublicFormat,
        )

        key1 = Ed25519PrivateKey.generate()
        key2 = Ed25519PrivateKey.generate()
        public_key_pem = key2.public_key().public_bytes(
            Encoding.PEM, PublicFormat.SubjectPublicKeyInfo,
        ).decode()

        payload = json.dumps({"tenant_id": tenant.id, "events": []}).encode()
        signature = key1.sign(payload)  # signed with wrong key

        with pytest.raises(ValueError, match="signature verification failed"):
            await MeteringService().upload_signed_usage_report(
                session, tenant.id, signature + payload, public_key_pem,
            )

    async def test_tenant_id_mismatch(self, session, tenant):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import (
            Encoding, PublicFormat,
        )

        key = Ed25519PrivateKey.generate()
        public_key_pem = key.public_key().public_bytes(
            Encoding.PEM, PublicFormat.SubjectPublicKeyInfo,
        ).decode()

        payload = json.dumps({"tenant_id": "wrong", "events": []}).encode()
        signature = key.sign(payload)

        with pytest.raises(ValueError, match="tenant_id mismatch"):
            await MeteringService().upload_signed_usage_report(
                session, tenant.id, signature + payload, public_key_pem,
            )

    async def test_short_report(self, session, tenant):
        with pytest.raises(ValueError, match="too short"):
            await MeteringService().upload_signed_usage_report(
                session, tenant.id, b"short", "key",
            )
