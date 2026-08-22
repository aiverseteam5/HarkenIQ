"""Billing engine — invoice generation, credit notes, delinquency state machine.

Every method takes an AsyncSession as the first argument. Commit
responsibility stays with the caller (API layer).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_console.db.models import new_id, utcnow
from harkeniq_console.db.repos import (
    AuditRepo,
    CreditNoteRepo,
    DelinquencyLogRepo,
    InvoiceLineRepo,
    InvoiceRepo,
    PriceBookRepo,
    SubscriptionRepo,
    TenantRepo,
    UsageEventRepo,
)

logger = logging.getLogger("harkeniq.console.billing.engine")

_FREQUENCY_MONTHS = {"annual": 12, "quarterly": 3}


class BillingEngine:
    """Stateless billing engine — all state travels through the session."""

    # ------------------------------------------------------------------
    # commit invoice
    # ------------------------------------------------------------------

    async def generate_commit_invoice(
        self,
        session: AsyncSession,
        tenant_id: str,
    ) -> dict:
        tenant = await TenantRepo(session).get_by_id(tenant_id)
        if tenant is None:
            raise ValueError(f"tenant {tenant_id} not found")

        sub = await SubscriptionRepo(session).get_by_tenant(tenant_id)
        if sub is None:
            raise ValueError(f"no subscription for tenant {tenant_id}")

        months = _FREQUENCY_MONTHS.get(sub.billing_frequency, 12)

        price = await PriceBookRepo(session).get_price(
            plan=sub.plan,
            currency=tenant.currency,
            billing_interval=sub.billing_frequency,
            version=sub.price_book_version,
        )
        if price is None:
            raise ValueError(
                f"no price_book entry for {sub.plan}/{tenant.currency}"
                f"/{sub.billing_frequency}/v{sub.price_book_version}"
            )

        line_qty = sub.node_commit * months
        amount = sub.node_commit * price.node_price_cents * months
        period_start = sub.billing_cycle_start
        period_end = _add_months(period_start, months)

        inv_number = (
            f"INV-{tenant.slug}-{period_start.strftime('%Y%m')}-{new_id()[:8]}"
        )

        inv = await InvoiceRepo(session).create(
            tenant_id=tenant_id,
            invoice_number=inv_number,
            type="commit",
            status="issued",
            currency=tenant.currency,
            subtotal_cents=amount,
            tax_cents=0,
            total_cents=amount,
            issued_at=utcnow(),
            due_at=utcnow() + timedelta(days=30),
            period_start=period_start,
            period_end=period_end,
        )

        await InvoiceLineRepo(session).create(
            invoice_id=inv.id,
            description=(
                f"{sub.plan.title()} plan — {sub.node_commit} nodes "
                f"x {months} months"
            ),
            quantity=line_qty,
            unit_price_cents=price.node_price_cents,
            amount_cents=amount,
            line_type="commit",
        )

        await AuditRepo(session).append(
            actor_id=None,
            actor_email="system",
            action="invoice.generated",
            subject_type="invoice",
            subject_id=inv.id,
            tenant_id=tenant_id,
            detail={"type": "commit", "total_cents": amount},
        )

        logger.info(
            "Generated commit invoice %s for tenant %s: %d %s",
            inv_number, tenant.slug, amount, tenant.currency,
        )
        return _invoice_dict(inv)

    # ------------------------------------------------------------------
    # overage invoice
    # ------------------------------------------------------------------

    async def generate_overage_invoice(
        self,
        session: AsyncSession,
        tenant_id: str,
        period_start: date,
        period_end: date,
    ) -> dict | None:
        tenant = await TenantRepo(session).get_by_id(tenant_id)
        if tenant is None:
            raise ValueError(f"tenant {tenant_id} not found")

        sub = await SubscriptionRepo(session).get_by_tenant(tenant_id)
        if sub is None:
            raise ValueError(f"no subscription for tenant {tenant_id}")

        high_water = await UsageEventRepo(session).get_high_water(
            tenant_id, period_start, period_end,
        )
        overage = max(0, high_water - sub.node_commit)
        if overage == 0:
            return None

        price = await PriceBookRepo(session).get_price(
            plan=sub.plan,
            currency=tenant.currency,
            billing_interval=sub.billing_frequency,
            version=sub.price_book_version,
        )
        if price is None:
            raise ValueError(f"no price_book for overage calculation")

        amount = overage * price.node_price_cents
        inv_number = (
            f"INV-{tenant.slug}-{period_start.strftime('%Y%m')}"
            f"-OVR-{new_id()[:8]}"
        )

        inv = await InvoiceRepo(session).create(
            tenant_id=tenant_id,
            invoice_number=inv_number,
            type="overage",
            status="issued",
            currency=tenant.currency,
            subtotal_cents=amount,
            tax_cents=0,
            total_cents=amount,
            issued_at=utcnow(),
            due_at=utcnow() + timedelta(days=30),
            period_start=period_start,
            period_end=period_end,
        )

        await InvoiceLineRepo(session).create(
            invoice_id=inv.id,
            description=(
                f"Overage — {overage} nodes above {sub.node_commit} commit "
                f"(high-water {high_water})"
            ),
            quantity=overage,
            unit_price_cents=price.node_price_cents,
            amount_cents=amount,
            line_type="overage",
        )

        await AuditRepo(session).append(
            actor_id=None,
            actor_email="system",
            action="invoice.generated",
            subject_type="invoice",
            subject_id=inv.id,
            tenant_id=tenant_id,
            detail={
                "type": "overage",
                "high_water": high_water,
                "overage": overage,
                "total_cents": amount,
            },
        )

        logger.info(
            "Generated overage invoice %s: %d nodes over commit, %d %s",
            inv_number, overage, amount, tenant.currency,
        )
        return _invoice_dict(inv)

    # ------------------------------------------------------------------
    # credit note
    # ------------------------------------------------------------------

    async def apply_credit_note(
        self,
        session: AsyncSession,
        invoice_id: str,
        amount_cents: int,
        reason: str,
        issued_by: str,
    ) -> dict:
        inv = await InvoiceRepo(session).get_by_id(invoice_id)
        if inv is None:
            raise ValueError(f"invoice {invoice_id} not found")
        if amount_cents <= 0:
            raise ValueError("credit note amount must be positive")
        if amount_cents > inv.total_cents:
            raise ValueError("credit note exceeds invoice total")

        cn = await CreditNoteRepo(session).create(
            invoice_id=invoice_id,
            tenant_id=inv.tenant_id,
            amount_cents=amount_cents,
            currency=inv.currency,
            reason=reason,
            issued_by=issued_by,
        )

        await AuditRepo(session).append(
            actor_id=issued_by,
            actor_email=issued_by,
            action="credit_note.issued",
            subject_type="credit_note",
            subject_id=cn.id,
            tenant_id=inv.tenant_id,
            detail={
                "invoice_id": invoice_id,
                "amount_cents": amount_cents,
                "reason": reason,
            },
        )

        return {
            "id": cn.id,
            "invoice_id": cn.invoice_id,
            "amount_cents": cn.amount_cents,
            "currency": cn.currency,
            "reason": cn.reason,
            "issued_at": cn.issued_at.isoformat(),
        }

    # ------------------------------------------------------------------
    # delinquency state machine
    # ------------------------------------------------------------------

    async def check_delinquency(
        self,
        session: AsyncSession,
        tenant_id: str,
    ) -> dict:
        tenant = await TenantRepo(session).get_by_id(tenant_id)
        if tenant is None:
            raise ValueError(f"tenant {tenant_id} not found")

        overdue_invoices = await InvoiceRepo(session).list_overdue()
        tenant_overdue = [i for i in overdue_invoices if i.tenant_id == tenant_id]

        current_status = tenant.delinquency_status
        now = utcnow()

        if not tenant_overdue:
            if current_status != "current":
                await self._transition(
                    session, tenant, "current", "all invoices paid",
                )
            return {
                "status": "current",
                "days_overdue": 0,
                "overdue_amount_cents": 0,
            }

        earliest_due = min(i.due_at for i in tenant_overdue if i.due_at)
        # ensure both are tz-aware or both naive for subtraction
        if earliest_due.tzinfo is None and now.tzinfo is not None:
            earliest_due = earliest_due.replace(tzinfo=timezone.utc)
        elif earliest_due.tzinfo is not None and now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        days_overdue = max(0, (now - earliest_due).days)
        overdue_amount = sum(i.total_cents for i in tenant_overdue)

        new_status = current_status
        if days_overdue >= 14 and current_status not in ("restricted", "suspended"):
            new_status = "restricted"
        elif days_overdue > 0 and current_status == "current":
            new_status = "overdue"

        if new_status != current_status:
            await self._transition(
                session,
                tenant,
                new_status,
                f"{days_overdue} days overdue, {overdue_amount} cents outstanding",
            )

        return {
            "status": new_status,
            "days_overdue": days_overdue,
            "overdue_amount_cents": overdue_amount,
        }

    async def _transition(
        self,
        session: AsyncSession,
        tenant,
        to_state: str,
        reason: str,
    ) -> None:
        from_state = tenant.delinquency_status
        await TenantRepo(session).update(tenant, delinquency_status=to_state)
        await DelinquencyLogRepo(session).append(
            tenant_id=tenant.id,
            from_state=from_state,
            to_state=to_state,
            reason=reason,
        )
        await AuditRepo(session).append(
            actor_id=None,
            actor_email="system",
            action="delinquency.transition",
            subject_type="tenant",
            subject_id=tenant.id,
            tenant_id=tenant.id,
            detail={
                "from": from_state,
                "to": to_state,
                "reason": reason,
            },
        )
        logger.info(
            "Delinquency transition: tenant %s %s -> %s (%s)",
            tenant.slug, from_state, to_state, reason,
        )

    # ------------------------------------------------------------------
    # payment + restore
    # ------------------------------------------------------------------

    async def record_payment_and_restore(
        self,
        session: AsyncSession,
        tenant_id: str,
        invoice_id: str,
        payment_data: dict,
    ) -> dict:
        inv = await InvoiceRepo(session).get_by_id(invoice_id)
        if inv is None:
            raise ValueError(f"invoice {invoice_id} not found")

        await InvoiceRepo(session).update(
            inv, status="paid", paid_at=utcnow(),
            payment_provider=payment_data.get("provider"),
            provider_payment_id=payment_data.get("provider_payment_id"),
        )

        await AuditRepo(session).append(
            actor_id=None,
            actor_email="system",
            action="invoice.paid",
            subject_type="invoice",
            subject_id=invoice_id,
            tenant_id=tenant_id,
            detail=payment_data,
        )

        await self.check_delinquency(session, tenant_id)

        return _invoice_dict(inv)

    # ------------------------------------------------------------------
    # batch true-ups (admin)
    # ------------------------------------------------------------------

    async def generate_all_trueups(self, session: AsyncSession) -> list[dict]:
        """Generate overage invoices for ALL active subscriptions for the previous month."""
        from harkeniq_console.db.models import Subscription
        from sqlalchemy import select

        now = utcnow()
        first_of_month = now.replace(day=1)
        period_end = first_of_month - timedelta(days=1)
        period_start = period_end.replace(day=1)

        subs = (
            await session.execute(
                select(Subscription).where(Subscription.status == "active")
            )
        ).scalars().all()

        generated = []
        for sub in subs:
            try:
                inv = await self.generate_overage_invoice(
                    session,
                    sub.tenant_id,
                    period_start.date() if hasattr(period_start, "date") else period_start,
                    period_end.date() if hasattr(period_end, "date") else period_end,
                )
                if inv is not None:
                    generated.append(inv)
            except Exception:
                logger.exception(
                    "Failed to generate true-up for tenant %s", sub.tenant_id,
                )
        return generated


# ------------------------------------------------------------------
# serializer
# ------------------------------------------------------------------


def _invoice_dict(inv) -> dict:
    return {
        "id": inv.id,
        "tenant_id": inv.tenant_id,
        "invoice_number": inv.invoice_number,
        "type": inv.type,
        "status": inv.status,
        "currency": inv.currency,
        "subtotal_cents": inv.subtotal_cents,
        "tax_cents": inv.tax_cents,
        "total_cents": inv.total_cents,
        "issued_at": inv.issued_at.isoformat() if inv.issued_at else None,
        "due_at": inv.due_at.isoformat() if inv.due_at else None,
        "paid_at": inv.paid_at.isoformat() if inv.paid_at else None,
        "period_start": str(inv.period_start),
        "period_end": str(inv.period_end),
        "created_at": inv.created_at.isoformat() if inv.created_at else None,
    }


def _add_months(d: date, months: int) -> date:
    """Add *months* to a date, clamping to end-of-month."""
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    import calendar
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)
