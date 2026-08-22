"""Phase 4 billing repository tests: PriceBook, UsageEvent, Invoice,
InvoiceLine, CreditNote, Payment, PaymentProviderCustomer, DelinquencyLog.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from harkeniq_console.db.repos import (
    CreditNoteRepo,
    DelinquencyLogRepo,
    InvoiceLineRepo,
    InvoiceRepo,
    PaymentProviderCustomerRepo,
    PaymentRepo,
    PriceBookRepo,
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
async def second_tenant(session):
    return await TenantRepo(session).create(
        slug="globex", name="Globex Corp", billing_country="IN", currency="INR",
    )


# ── PriceBookRepo ────────────────────────────────────────────────────


class TestPriceBookRepo:
    async def test_create(self, session):
        repo = PriceBookRepo(session)
        pb = await repo.create(
            plan="approve", currency="USD", billing_interval="annual",
            node_price_cents=2400, version=1,
        )
        assert pb.id
        assert pb.plan == "approve"
        assert pb.node_price_cents == 2400

    async def test_get_price_exact(self, session):
        repo = PriceBookRepo(session)
        await repo.create(
            plan="approve", currency="USD", billing_interval="annual",
            node_price_cents=2400, version=1,
        )
        found = await repo.get_price("approve", "USD", "annual", 1)
        assert found is not None
        assert found.node_price_cents == 2400

    async def test_get_price_wrong_currency(self, session):
        repo = PriceBookRepo(session)
        await repo.create(
            plan="approve", currency="USD", billing_interval="annual",
            node_price_cents=2400, version=1,
        )
        assert await repo.get_price("approve", "EUR", "annual", 1) is None

    async def test_get_price_wrong_interval(self, session):
        repo = PriceBookRepo(session)
        await repo.create(
            plan="approve", currency="USD", billing_interval="annual",
            node_price_cents=2400, version=1,
        )
        assert await repo.get_price("approve", "USD", "monthly", 1) is None

    async def test_get_price_wrong_version(self, session):
        repo = PriceBookRepo(session)
        await repo.create(
            plan="approve", currency="USD", billing_interval="annual",
            node_price_cents=2400, version=1,
        )
        assert await repo.get_price("approve", "USD", "annual", 2) is None

    async def test_list_all(self, session):
        repo = PriceBookRepo(session)
        await repo.create(plan="approve", currency="USD", billing_interval="monthly", node_price_cents=3000, version=1)
        await repo.create(plan="approve", currency="USD", billing_interval="annual", node_price_cents=2400, version=1)
        await repo.create(plan="enterprise", currency="USD", billing_interval="monthly", node_price_cents=5000, version=1)
        items = await repo.list_all()
        assert len(items) == 3

    async def test_list_all_with_version(self, session):
        repo = PriceBookRepo(session)
        await repo.create(plan="approve", currency="USD", billing_interval="annual", node_price_cents=2400, version=1)
        await repo.create(plan="approve", currency="USD", billing_interval="annual", node_price_cents=2600, version=2)
        items = await repo.list_all(version=1)
        assert len(items) == 1
        assert items[0].node_price_cents == 2400

    async def test_site_base_fee_nullable(self, session):
        repo = PriceBookRepo(session)
        pb = await repo.create(
            plan="approve", currency="USD", billing_interval="annual",
            node_price_cents=2400, version=1,
        )
        assert pb.site_base_fee_cents is None

    async def test_site_base_fee_set(self, session):
        repo = PriceBookRepo(session)
        pb = await repo.create(
            plan="enterprise", currency="USD", billing_interval="annual",
            node_price_cents=4000, site_base_fee_cents=20000, version=1,
        )
        assert pb.site_base_fee_cents == 20000


# ── UsageEventRepo ───────────────────────────────────────────────────


class TestUsageEventRepo:
    async def test_record(self, session, tenant):
        repo = UsageEventRepo(session)
        ev = await repo.record(
            tenant_id=tenant.id, site_name="dc-east",
            date=date(2026, 7, 1), node_count=100,
        )
        assert ev.id
        assert ev.node_count == 100
        assert ev.source == "cc_report"

    async def test_record_with_source(self, session, tenant):
        repo = UsageEventRepo(session)
        ev = await repo.record(
            tenant_id=tenant.id, site_name="dc-east",
            date=date(2026, 7, 1), node_count=50,
            source="signed_upload",
        )
        assert ev.source == "signed_upload"

    async def test_record_batch(self, session, tenant):
        repo = UsageEventRepo(session)
        events = [
            {"tenant_id": tenant.id, "site_name": "dc-east", "date": date(2026, 7, d), "node_count": 100 + d}
            for d in range(1, 6)
        ]
        count = await repo.record_batch(events)
        assert count == 5

    async def test_high_water(self, session, tenant):
        repo = UsageEventRepo(session)
        for d in range(1, 11):
            await repo.record(
                tenant_id=tenant.id, site_name="dc-east",
                date=date(2026, 7, d), node_count=90 + d,
            )
        hw = await repo.get_high_water(tenant.id, date(2026, 7, 1), date(2026, 7, 31))
        assert hw == 100  # 90 + 10

    async def test_high_water_no_data(self, session, tenant):
        repo = UsageEventRepo(session)
        hw = await repo.get_high_water(tenant.id, date(2026, 7, 1), date(2026, 7, 31))
        assert hw == 0

    async def test_high_water_date_range(self, session, tenant):
        repo = UsageEventRepo(session)
        await repo.record(tenant_id=tenant.id, site_name="dc-east", date=date(2026, 6, 15), node_count=200)
        await repo.record(tenant_id=tenant.id, site_name="dc-east", date=date(2026, 7, 5), node_count=80)
        hw = await repo.get_high_water(tenant.id, date(2026, 7, 1), date(2026, 7, 31))
        assert hw == 80  # June event excluded

    async def test_daily_counts(self, session, tenant):
        repo = UsageEventRepo(session)
        for d in range(1, 4):
            await repo.record(
                tenant_id=tenant.id, site_name="dc-east",
                date=date(2026, 7, d), node_count=100,
            )
        daily = await repo.get_daily_counts(tenant.id, date(2026, 7, 1), date(2026, 7, 31))
        assert len(daily) == 3

    async def test_daily_counts_empty(self, session, tenant):
        repo = UsageEventRepo(session)
        daily = await repo.get_daily_counts(tenant.id, date(2026, 7, 1), date(2026, 7, 31))
        assert len(daily) == 0

    async def test_per_site_summary(self, session, tenant):
        repo = UsageEventRepo(session)
        for d in range(1, 6):
            await repo.record(tenant_id=tenant.id, site_name="dc-east", date=date(2026, 7, d), node_count=100)
            await repo.record(tenant_id=tenant.id, site_name="dc-west", date=date(2026, 7, d), node_count=50)
        summary = await repo.get_per_site_summary(tenant.id, date(2026, 7, 1), date(2026, 7, 31))
        assert len(summary) == 2
        east = next(s for s in summary if s["site_name"] == "dc-east")
        assert east["peak_nodes"] == 100
        assert east["days"] == 5

    async def test_per_site_summary_varying(self, session, tenant):
        repo = UsageEventRepo(session)
        await repo.record(tenant_id=tenant.id, site_name="dc-east", date=date(2026, 7, 1), node_count=80)
        await repo.record(tenant_id=tenant.id, site_name="dc-east", date=date(2026, 7, 2), node_count=120)
        summary = await repo.get_per_site_summary(tenant.id, date(2026, 7, 1), date(2026, 7, 31))
        east = summary[0]
        assert east["peak_nodes"] == 120
        assert east["avg_nodes"] == 100.0

    async def test_tenant_isolation(self, session, tenant, second_tenant):
        repo = UsageEventRepo(session)
        await repo.record(tenant_id=tenant.id, site_name="dc-east", date=date(2026, 7, 1), node_count=200)
        await repo.record(tenant_id=second_tenant.id, site_name="dc-mumbai", date=date(2026, 7, 1), node_count=50)
        hw = await repo.get_high_water(tenant.id, date(2026, 7, 1), date(2026, 7, 31))
        assert hw == 200
        hw2 = await repo.get_high_water(second_tenant.id, date(2026, 7, 1), date(2026, 7, 31))
        assert hw2 == 50


# ── InvoiceRepo ──────────────────────────────────────────────────────


class TestInvoiceRepo:
    async def test_create(self, session, tenant):
        repo = InvoiceRepo(session)
        inv = await repo.create(
            tenant_id=tenant.id, invoice_number="INV-acme-202601-abc",
            type="commit", status="issued", currency="USD",
            subtotal_cents=72000_00, total_cents=72000_00,
            period_start=date(2026, 1, 1), period_end=date(2026, 12, 31),
            issued_at=utcnow(), due_at=utcnow() + timedelta(days=30),
        )
        assert inv.id
        assert inv.type == "commit"

    async def test_get_by_id(self, session, tenant):
        repo = InvoiceRepo(session)
        inv = await repo.create(
            tenant_id=tenant.id, invoice_number="INV-001",
            type="commit", currency="USD", subtotal_cents=1000, total_cents=1000,
            period_start=date(2026, 1, 1), period_end=date(2026, 12, 31),
        )
        found = await repo.get_by_id(inv.id)
        assert found is not None
        assert found.invoice_number == "INV-001"

    async def test_get_by_id_not_found(self, session):
        assert await InvoiceRepo(session).get_by_id("nope") is None

    async def test_get_by_number(self, session, tenant):
        repo = InvoiceRepo(session)
        await repo.create(
            tenant_id=tenant.id, invoice_number="INV-UNIQUE",
            type="commit", currency="USD", subtotal_cents=1000, total_cents=1000,
            period_start=date(2026, 1, 1), period_end=date(2026, 12, 31),
        )
        found = await repo.get_by_number("INV-UNIQUE")
        assert found is not None

    async def test_list_by_tenant(self, session, tenant):
        repo = InvoiceRepo(session)
        for i in range(3):
            await repo.create(
                tenant_id=tenant.id, invoice_number=f"INV-{i}",
                type="commit", currency="USD", subtotal_cents=1000, total_cents=1000,
                period_start=date(2026, 1, 1), period_end=date(2026, 12, 31),
            )
        items, total = await repo.list_by_tenant(tenant.id)
        assert total == 3
        assert len(items) == 3

    async def test_list_by_tenant_status_filter(self, session, tenant):
        repo = InvoiceRepo(session)
        await repo.create(
            tenant_id=tenant.id, invoice_number="INV-A", type="commit",
            status="issued", currency="USD", subtotal_cents=1000, total_cents=1000,
            period_start=date(2026, 1, 1), period_end=date(2026, 12, 31),
        )
        await repo.create(
            tenant_id=tenant.id, invoice_number="INV-B", type="commit",
            status="paid", currency="USD", subtotal_cents=1000, total_cents=1000,
            period_start=date(2026, 1, 1), period_end=date(2026, 12, 31),
        )
        items, total = await repo.list_by_tenant(tenant.id, status="paid")
        assert total == 1
        assert items[0].status == "paid"

    async def test_list_overdue(self, session, tenant):
        repo = InvoiceRepo(session)
        await repo.create(
            tenant_id=tenant.id, invoice_number="INV-OVERDUE",
            type="commit", status="issued", currency="USD",
            subtotal_cents=5000, total_cents=5000,
            period_start=date(2026, 1, 1), period_end=date(2026, 12, 31),
            due_at=utcnow() - timedelta(days=5),
            issued_at=utcnow() - timedelta(days=35),
        )
        overdue = await repo.list_overdue()
        assert len(overdue) == 1

    async def test_list_overdue_excludes_paid(self, session, tenant):
        repo = InvoiceRepo(session)
        await repo.create(
            tenant_id=tenant.id, invoice_number="INV-PAID",
            type="commit", status="paid", currency="USD",
            subtotal_cents=5000, total_cents=5000,
            period_start=date(2026, 1, 1), period_end=date(2026, 12, 31),
            due_at=utcnow() - timedelta(days=5),
        )
        overdue = await repo.list_overdue()
        assert len(overdue) == 0

    async def test_update_status(self, session, tenant):
        repo = InvoiceRepo(session)
        inv = await repo.create(
            tenant_id=tenant.id, invoice_number="INV-UPD",
            type="commit", status="issued", currency="USD",
            subtotal_cents=1000, total_cents=1000,
            period_start=date(2026, 1, 1), period_end=date(2026, 12, 31),
        )
        updated = await repo.update(inv, status="paid", paid_at=utcnow())
        assert updated.status == "paid"
        assert updated.paid_at is not None

    async def test_count_by_tenant(self, session, tenant):
        repo = InvoiceRepo(session)
        await repo.create(
            tenant_id=tenant.id, invoice_number="INV-C1",
            type="commit", currency="USD", subtotal_cents=1000, total_cents=1000,
            period_start=date(2026, 1, 1), period_end=date(2026, 12, 31),
        )
        assert await repo.count_by_tenant(tenant.id) == 1

    async def test_revenue_by_plan(self, session, tenant):
        repo = InvoiceRepo(session)
        await repo.create(
            tenant_id=tenant.id, invoice_number="INV-R1",
            type="commit", status="paid", currency="USD",
            subtotal_cents=72000_00, total_cents=72000_00,
            period_start=date(2026, 1, 1), period_end=date(2026, 12, 31),
        )
        rev = await repo.get_revenue_by_plan()
        assert len(rev) == 1
        assert rev[0]["type"] == "commit"
        assert rev[0]["total_cents"] == 72000_00


# ── InvoiceLineRepo ─────────────────────────────────────────────────


class TestInvoiceLineRepo:
    async def test_create(self, session, tenant):
        inv = await InvoiceRepo(session).create(
            tenant_id=tenant.id, invoice_number="INV-L1",
            type="commit", currency="USD", subtotal_cents=1000, total_cents=1000,
            period_start=date(2026, 1, 1), period_end=date(2026, 12, 31),
        )
        line = await InvoiceLineRepo(session).create(
            invoice_id=inv.id, description="Test line",
            quantity=10, unit_price_cents=100, amount_cents=1000,
            line_type="commit",
        )
        assert line.id
        assert line.line_type == "commit"

    async def test_list_by_invoice(self, session, tenant):
        inv = await InvoiceRepo(session).create(
            tenant_id=tenant.id, invoice_number="INV-L2",
            type="commit", currency="USD", subtotal_cents=3000, total_cents=3000,
            period_start=date(2026, 1, 1), period_end=date(2026, 12, 31),
        )
        repo = InvoiceLineRepo(session)
        await repo.create(invoice_id=inv.id, description="Line 1", quantity=10, unit_price_cents=100, amount_cents=1000, line_type="commit")
        await repo.create(invoice_id=inv.id, description="Line 2", quantity=5, unit_price_cents=200, amount_cents=1000, line_type="overage")
        lines = await repo.list_by_invoice(inv.id)
        assert len(lines) == 2

    async def test_list_by_invoice_empty(self, session, tenant):
        inv = await InvoiceRepo(session).create(
            tenant_id=tenant.id, invoice_number="INV-L3",
            type="commit", currency="USD", subtotal_cents=0, total_cents=0,
            period_start=date(2026, 1, 1), period_end=date(2026, 12, 31),
        )
        lines = await InvoiceLineRepo(session).list_by_invoice(inv.id)
        assert len(lines) == 0


# ── CreditNoteRepo ──────────────────────────────────────────────────


class TestCreditNoteRepo:
    async def test_create(self, session, tenant):
        inv = await InvoiceRepo(session).create(
            tenant_id=tenant.id, invoice_number="INV-CN1",
            type="commit", currency="USD", subtotal_cents=5000, total_cents=5000,
            period_start=date(2026, 1, 1), period_end=date(2026, 12, 31),
        )
        cn = await CreditNoteRepo(session).create(
            invoice_id=inv.id, tenant_id=tenant.id,
            amount_cents=1000, currency="USD", reason="Correction",
            issued_by="admin",
        )
        assert cn.id
        assert cn.amount_cents == 1000

    async def test_list_by_invoice(self, session, tenant):
        inv = await InvoiceRepo(session).create(
            tenant_id=tenant.id, invoice_number="INV-CN2",
            type="commit", currency="USD", subtotal_cents=5000, total_cents=5000,
            period_start=date(2026, 1, 1), period_end=date(2026, 12, 31),
        )
        repo = CreditNoteRepo(session)
        await repo.create(invoice_id=inv.id, tenant_id=tenant.id, amount_cents=500, currency="USD", reason="R1")
        await repo.create(invoice_id=inv.id, tenant_id=tenant.id, amount_cents=300, currency="USD", reason="R2")
        cns = await repo.list_by_invoice(inv.id)
        assert len(cns) == 2

    async def test_list_by_tenant(self, session, tenant):
        inv = await InvoiceRepo(session).create(
            tenant_id=tenant.id, invoice_number="INV-CN3",
            type="commit", currency="USD", subtotal_cents=5000, total_cents=5000,
            period_start=date(2026, 1, 1), period_end=date(2026, 12, 31),
        )
        await CreditNoteRepo(session).create(
            invoice_id=inv.id, tenant_id=tenant.id,
            amount_cents=200, currency="USD", reason="Test",
        )
        cns = await CreditNoteRepo(session).list_by_tenant(tenant.id)
        assert len(cns) == 1


# ── PaymentRepo ──────────────────────────────────────────────────────


class TestPaymentRepo:
    async def test_create(self, session, tenant):
        pay = await PaymentRepo(session).create(
            tenant_id=tenant.id, provider="stripe",
            amount_cents=5000, currency="USD", status="pending",
        )
        assert pay.id
        assert pay.status == "pending"

    async def test_get_by_id(self, session, tenant):
        repo = PaymentRepo(session)
        pay = await repo.create(
            tenant_id=tenant.id, provider="stripe",
            amount_cents=5000, currency="USD",
        )
        found = await repo.get_by_id(pay.id)
        assert found is not None

    async def test_get_by_provider_event_id(self, session, tenant):
        repo = PaymentRepo(session)
        await repo.create(
            tenant_id=tenant.id, provider="stripe",
            amount_cents=5000, currency="USD",
            provider_event_id="evt_123",
        )
        found = await repo.get_by_provider_event_id("evt_123")
        assert found is not None

    async def test_get_by_provider_event_id_not_found(self, session):
        assert await PaymentRepo(session).get_by_provider_event_id("nope") is None

    async def test_list_by_tenant(self, session, tenant):
        repo = PaymentRepo(session)
        for i in range(3):
            await repo.create(
                tenant_id=tenant.id, provider="stripe",
                amount_cents=1000 * (i + 1), currency="USD",
            )
        items, total = await repo.list_by_tenant(tenant.id)
        assert total == 3

    async def test_list_by_invoice(self, session, tenant):
        inv = await InvoiceRepo(session).create(
            tenant_id=tenant.id, invoice_number="INV-P1",
            type="commit", currency="USD", subtotal_cents=5000, total_cents=5000,
            period_start=date(2026, 1, 1), period_end=date(2026, 12, 31),
        )
        repo = PaymentRepo(session)
        await repo.create(
            tenant_id=tenant.id, invoice_id=inv.id,
            provider="stripe", amount_cents=5000, currency="USD",
        )
        payments = await repo.list_by_invoice(inv.id)
        assert len(payments) == 1

    async def test_update(self, session, tenant):
        repo = PaymentRepo(session)
        pay = await repo.create(
            tenant_id=tenant.id, provider="stripe",
            amount_cents=5000, currency="USD", status="pending",
        )
        updated = await repo.update(pay, status="completed", completed_at=utcnow())
        assert updated.status == "completed"
        assert updated.completed_at is not None


# ── PaymentProviderCustomerRepo ──────────────────────────────────────


class TestPaymentProviderCustomerRepo:
    async def test_create(self, session, tenant):
        ppc = await PaymentProviderCustomerRepo(session).create(
            tenant_id=tenant.id, provider="stripe",
            provider_customer_id="cus_abc123",
        )
        assert ppc.id
        assert ppc.provider_customer_id == "cus_abc123"

    async def test_get(self, session, tenant):
        repo = PaymentProviderCustomerRepo(session)
        await repo.create(
            tenant_id=tenant.id, provider="stripe",
            provider_customer_id="cus_xyz",
        )
        found = await repo.get(tenant.id, "stripe")
        assert found is not None
        assert found.provider_customer_id == "cus_xyz"

    async def test_get_not_found(self, session, tenant):
        assert await PaymentProviderCustomerRepo(session).get(tenant.id, "stripe") is None

    async def test_get_wrong_provider(self, session, tenant):
        repo = PaymentProviderCustomerRepo(session)
        await repo.create(
            tenant_id=tenant.id, provider="stripe",
            provider_customer_id="cus_abc",
        )
        assert await repo.get(tenant.id, "razorpay") is None


# ── DelinquencyLogRepo ───────────────────────────────────────────────


class TestDelinquencyLogRepo:
    async def test_append(self, session, tenant):
        repo = DelinquencyLogRepo(session)
        entry = await repo.append(
            tenant_id=tenant.id, from_state="current",
            to_state="overdue", reason="test",
        )
        assert entry.id
        assert entry.to_state == "overdue"

    async def test_list_by_tenant_ordered(self, session, tenant):
        repo = DelinquencyLogRepo(session)
        await repo.append(tenant_id=tenant.id, from_state="current", to_state="overdue", reason="first")
        await repo.append(tenant_id=tenant.id, from_state="overdue", to_state="restricted", reason="second")
        entries = await repo.list_by_tenant(tenant.id)
        assert len(entries) == 2
        assert entries[0].to_state == "restricted"  # most recent first

    async def test_list_by_tenant_empty(self, session, tenant):
        entries = await DelinquencyLogRepo(session).list_by_tenant(tenant.id)
        assert len(entries) == 0

    async def test_tenant_isolation(self, session, tenant, second_tenant):
        repo = DelinquencyLogRepo(session)
        await repo.append(tenant_id=tenant.id, from_state="current", to_state="overdue", reason="t1")
        await repo.append(tenant_id=second_tenant.id, from_state="current", to_state="restricted", reason="t2")
        t1_entries = await repo.list_by_tenant(tenant.id)
        assert len(t1_entries) == 1
        assert t1_entries[0].to_state == "overdue"
