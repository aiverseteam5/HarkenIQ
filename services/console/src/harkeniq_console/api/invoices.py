"""Invoice and payment endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_console.api.deps import get_session, require_super_admin, tenant_scope
from harkeniq_console.auth import UserContext
from harkeniq_console.billing.engine import BillingEngine
from harkeniq_console.db.models import utcnow
from harkeniq_console.db.repos import (
    AuditRepo,
    CreditNoteRepo,
    InvoiceLineRepo,
    InvoiceRepo,
    PaymentRepo,
    PaymentProviderCustomerRepo,
    TenantRepo,
)
from harkeniq_console.billing.router import get_payment_provider

router = APIRouter(prefix="/api/tenants/{tenant_id}/invoices", tags=["invoices"])
payments_router = APIRouter(prefix="/api/tenants/{tenant_id}/payments", tags=["payments"])
admin_router = APIRouter(prefix="/api/admin/invoices", tags=["invoices-admin"])

_engine = BillingEngine()


# ── serializers ──────────────────────────────────────────────────────


def _inv_dict(inv) -> dict:
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
        "payment_provider": inv.payment_provider,
        "provider_payment_id": inv.provider_payment_id,
        "created_at": inv.created_at.isoformat() if inv.created_at else None,
    }


def _line_dict(line) -> dict:
    return {
        "id": line.id,
        "invoice_id": line.invoice_id,
        "description": line.description,
        "quantity": line.quantity,
        "unit_price_cents": line.unit_price_cents,
        "amount_cents": line.amount_cents,
        "line_type": line.line_type,
    }


def _cn_dict(cn) -> dict:
    return {
        "id": cn.id,
        "invoice_id": cn.invoice_id,
        "tenant_id": cn.tenant_id,
        "amount_cents": cn.amount_cents,
        "currency": cn.currency,
        "reason": cn.reason,
        "issued_by": cn.issued_by,
        "issued_at": cn.issued_at.isoformat() if cn.issued_at else None,
    }


def _pay_dict(p) -> dict:
    return {
        "id": p.id,
        "tenant_id": p.tenant_id,
        "invoice_id": p.invoice_id,
        "provider": p.provider,
        "amount_cents": p.amount_cents,
        "currency": p.currency,
        "status": p.status,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "completed_at": p.completed_at.isoformat() if p.completed_at else None,
    }


# ── tenant invoice endpoints ────────────────────────────────────────


@router.get("/")
async def list_invoices(
    tenant_id: str,
    status: str | None = None,
    page: int = 1,
    page_size: int = 50,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(tenant_scope),
) -> dict:
    items, total = await InvoiceRepo(session).list_by_tenant(
        tenant_id, status=status, page=page, page_size=page_size,
    )
    return {"items": [_inv_dict(i) for i in items], "total": total, "page": page}


@router.get("/{invoice_id}")
async def get_invoice(
    tenant_id: str,
    invoice_id: str,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(tenant_scope),
) -> dict:
    inv = await InvoiceRepo(session).get_by_id(invoice_id)
    if inv is None or inv.tenant_id != tenant_id:
        raise HTTPException(404, "invoice not found")
    lines = await InvoiceLineRepo(session).list_by_invoice(invoice_id)
    cns = await CreditNoteRepo(session).list_by_invoice(invoice_id)
    payments = await PaymentRepo(session).list_by_invoice(invoice_id)
    return {
        "invoice": _inv_dict(inv),
        "lines": [_line_dict(l) for l in lines],
        "credit_notes": [_cn_dict(c) for c in cns],
        "payments": [_pay_dict(p) for p in payments],
    }


@router.post("/{invoice_id}/pay")
async def pay_invoice(
    tenant_id: str,
    invoice_id: str,
    session: AsyncSession = Depends(get_session),
    state=Depends(lambda r: r.app.state.console),
    user: UserContext = Depends(tenant_scope),
) -> dict:
    inv = await InvoiceRepo(session).get_by_id(invoice_id)
    if inv is None or inv.tenant_id != tenant_id:
        raise HTTPException(404, "invoice not found")
    if inv.status != "issued":
        raise HTTPException(400, f"invoice status is {inv.status}, not issued")

    tenant = await TenantRepo(session).get_by_id(tenant_id)
    if tenant is None:
        raise HTTPException(404, "tenant not found")

    cfg = state.config
    provider = get_payment_provider(
        tenant.billing_country,
        razorpay_key_id=cfg.razorpay_key_id,
        razorpay_key_secret=cfg.razorpay_key_secret,
        stripe_secret_key=cfg.stripe_secret_key,
        stripe_webhook_secret=cfg.stripe_webhook_secret,
    )
    provider_name = "razorpay" if tenant.billing_country.upper() == "IN" else "stripe"

    # ensure customer
    ppc_repo = PaymentProviderCustomerRepo(session)
    ppc = await ppc_repo.get(tenant_id, provider_name)
    if ppc is None:
        cust_id = await provider.ensure_customer(
            tenant_id, tenant.name, tenant.name,
        )
        ppc = await ppc_repo.create(
            tenant_id=tenant_id,
            provider=provider_name,
            provider_customer_id=cust_id,
        )
    else:
        cust_id = ppc.provider_customer_id

    # create payment
    result = await provider.create_payment(
        cust_id, inv.total_cents, inv.currency, f"Invoice {inv.invoice_number}",
    )

    # record payment
    payment = await PaymentRepo(session).create(
        tenant_id=tenant_id,
        invoice_id=invoice_id,
        provider=provider_name,
        provider_payment_id=result.get("id"),
        provider_customer_id=cust_id,
        amount_cents=inv.total_cents,
        currency=inv.currency,
        status="pending",
    )

    await session.commit()
    return {"payment": _pay_dict(payment), "provider_result": result}


# ── tenant payments ─────────────────────────────────────────────────


@payments_router.get("/")
async def list_payments(
    tenant_id: str,
    page: int = 1,
    page_size: int = 50,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(tenant_scope),
) -> dict:
    items, total = await PaymentRepo(session).list_by_tenant(
        tenant_id, page=page, page_size=page_size,
    )
    return {"items": [_pay_dict(p) for p in items], "total": total, "page": page}


# ── admin endpoints ─────────────────────────────────────────────────


@admin_router.post("/generate-trueups")
async def generate_trueups(
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_super_admin),
) -> dict:
    generated = await _engine.generate_all_trueups(session)
    await session.commit()
    return {"generated": len(generated), "invoices": generated}


class CreditNoteRequest(BaseModel):
    amount_cents: int
    reason: str


@admin_router.post("/{invoice_id}/credit-note")
async def create_credit_note(
    invoice_id: str,
    body: CreditNoteRequest,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_super_admin),
) -> dict:
    try:
        cn = await _engine.apply_credit_note(
            session, invoice_id, body.amount_cents, body.reason, user.user_id,
        )
        await session.commit()
        return cn
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


class ManualPaymentRequest(BaseModel):
    amount_cents: int
    currency: str
    reference: str = ""


@admin_router.post("/{invoice_id}/manual-payment")
async def manual_payment(
    invoice_id: str,
    body: ManualPaymentRequest,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_super_admin),
) -> dict:
    inv = await InvoiceRepo(session).get_by_id(invoice_id)
    if inv is None:
        raise HTTPException(404, "invoice not found")
    if inv.status not in ("issued", "draft"):
        raise HTTPException(400, f"cannot pay invoice with status {inv.status}")

    payment = await PaymentRepo(session).create(
        tenant_id=inv.tenant_id,
        invoice_id=invoice_id,
        provider="manual",
        provider_payment_id=body.reference or None,
        amount_cents=body.amount_cents,
        currency=body.currency,
        status="completed",
        completed_at=utcnow(),
    )

    await _engine.record_payment_and_restore(
        session, inv.tenant_id, invoice_id,
        {"provider": "manual", "reference": body.reference},
    )
    await session.commit()

    await AuditRepo(session).append(
        actor_id=user.user_id,
        actor_email=user.email,
        action="payment.manual",
        subject_type="payment",
        subject_id=payment.id,
        tenant_id=inv.tenant_id,
        detail={
            "invoice_id": invoice_id,
            "amount_cents": body.amount_cents,
            "reference": body.reference,
        },
    )
    await session.commit()
    return _pay_dict(payment)
