"""Support ticket endpoints."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/tenants/{tenant_id}/tickets", tags=["support"])


@router.post("/")
async def create_ticket(tenant_id: str) -> dict:
    return {"stub": "create_ticket", "tenant_id": tenant_id}


@router.get("/")
async def list_tickets(tenant_id: str) -> dict:
    return {"stub": "list_tickets", "tenant_id": tenant_id}


@router.get("/{ticket_id}")
async def get_ticket(tenant_id: str, ticket_id: str) -> dict:
    return {"stub": "get_ticket", "tenant_id": tenant_id, "ticket_id": ticket_id}


@router.post("/{ticket_id}/reply")
async def reply_to_ticket(tenant_id: str, ticket_id: str) -> dict:
    return {"stub": "reply_to_ticket", "tenant_id": tenant_id, "ticket_id": ticket_id}


@router.post("/{ticket_id}/close")
async def close_ticket(tenant_id: str, ticket_id: str) -> dict:
    return {"stub": "close_ticket", "tenant_id": tenant_id, "ticket_id": ticket_id}


# ── admin support ──────────────────────────────────────────────────

admin_router = APIRouter(prefix="/api/admin/tickets", tags=["support-admin"])


@admin_router.get("/")
async def admin_list_tickets() -> dict:
    return {"stub": "admin_list_tickets"}


@admin_router.patch("/{ticket_id}")
async def admin_update_ticket(ticket_id: str) -> dict:
    return {"stub": "admin_update_ticket", "ticket_id": ticket_id}
