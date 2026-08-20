"""Invoice and payment endpoints."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/tenants/{tenant_id}/invoices", tags=["invoices"])


@router.get("/")
async def list_invoices(tenant_id: str) -> dict:
    return {"stub": "list_invoices", "tenant_id": tenant_id}


@router.get("/{invoice_id}")
async def get_invoice(tenant_id: str, invoice_id: str) -> dict:
    return {"stub": "get_invoice", "tenant_id": tenant_id, "invoice_id": invoice_id}


@router.post("/{invoice_id}/pay")
async def pay_invoice(tenant_id: str, invoice_id: str) -> dict:
    return {"stub": "pay_invoice", "tenant_id": tenant_id, "invoice_id": invoice_id}


@router.get("/payments")
async def list_payments(tenant_id: str) -> dict:
    return {"stub": "list_payments", "tenant_id": tenant_id}


# ── admin invoicing ────────────────────────────────────────────────

admin_router = APIRouter(prefix="/api/admin/invoices", tags=["invoices-admin"])


@admin_router.post("/generate-trueups")
async def generate_trueups() -> dict:
    return {"stub": "generate_trueups"}


@admin_router.post("/{invoice_id}/credit-note")
async def create_credit_note(invoice_id: str) -> dict:
    return {"stub": "create_credit_note", "invoice_id": invoice_id}
