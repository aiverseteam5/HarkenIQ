"""Support ticket endpoints (tenant + admin)."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_console.api.deps import (
    get_session,
    require_role,
    require_super_admin,
    require_tenant_permission,
    tenant_scope,
)
from harkeniq_console.auth import UserContext, get_current_user
from harkeniq_console.db.models import utcnow
from harkeniq_console.db.repos import (
    AuditRepo,
    SupportAccessLogRepo,
    SupportTicketRepo,
    TicketMessageRepo,
    TicketStateChangeRepo,
    TenantRepo,
)

router = APIRouter(prefix="/api/tenants/{tenant_id}/tickets", tags=["support"])
admin_router = APIRouter(prefix="/api/admin/tickets", tags=["support-admin"])

# SLA targets by severity (hours)
_SLA_HOURS = {"S1": 4, "S2": 8, "S3": 24, "S4": 72}
_VALID_STATUSES = {"open", "acknowledged", "in_progress", "waiting_on_tenant", "closed"}
_VALID_SEVERITIES = {"S1", "S2", "S3", "S4"}
_VALID_COMPONENTS = {"SM", "Agent", "Skill", "CC", "Billing", "Other"}


# ── serializers ──────────────────────────────────────────────────────


def _ticket_dict(t) -> dict:
    return {
        "id": t.id,
        "tenant_id": t.tenant_id,
        "ticket_number": t.ticket_number,
        "subject": t.subject,
        "body": t.body,
        "severity": t.severity,
        "component": t.component,
        "site_name": t.site_name,
        "status": t.status,
        "assigned_to": t.assigned_to,
        "created_by": t.created_by,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        "closed_at": t.closed_at.isoformat() if t.closed_at else None,
        "sla_due_at": t.sla_due_at.isoformat() if t.sla_due_at else None,
    }


def _message_dict(m) -> dict:
    return {
        "id": m.id,
        "ticket_id": m.ticket_id,
        "author_id": m.author_id,
        "author_email": m.author_email,
        "body": m.body,
        "is_internal": m.is_internal,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


def _access_dict(a) -> dict:
    return {
        "id": a.id,
        "tenant_id": a.tenant_id,
        "enabled_by": a.enabled_by,
        "enabled_at": a.enabled_at.isoformat() if a.enabled_at else None,
        "expires_at": a.expires_at.isoformat() if a.expires_at else None,
        "revoked_at": a.revoked_at.isoformat() if a.revoked_at else None,
        "revoked_by": a.revoked_by,
        "status": a.status,
        "requested_by": a.requested_by,
        "requested_at": a.requested_at.isoformat() if a.requested_at else None,
        "reason": a.reason,
        "approved_by": a.approved_by,
        "approved_at": a.approved_at.isoformat() if a.approved_at else None,
        "denied_by": a.denied_by,
        "denied_at": a.denied_at.isoformat() if a.denied_at else None,
    }


# ── request models ───────────────────────────────────────────────────


class CreateTicketRequest(BaseModel):
    subject: str
    body: str = ""
    severity: str = "S3"
    component: str = "Other"
    site_name: str | None = None


class ReplyRequest(BaseModel):
    body: str
    is_internal: bool = False


class UpdateTicketRequest(BaseModel):
    status: str | None = None
    assigned_to: str | None = None
    severity: str | None = None


# ── tenant endpoints ─────────────────────────────────────────────────


@router.post("/")
async def create_ticket(
    tenant_id: str,
    body: CreateTicketRequest,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_tenant_permission("support.create")),
) -> dict:
    if body.severity not in _VALID_SEVERITIES:
        raise HTTPException(400, f"severity must be one of {_VALID_SEVERITIES}")

    repo = SupportTicketRepo(session)
    ticket_number = await repo.next_ticket_number(tenant_id)

    now = utcnow()
    sla_hours = _SLA_HOURS.get(body.severity, 24)
    sla_due = now + timedelta(hours=sla_hours)

    ticket = await repo.create(
        tenant_id=tenant_id,
        ticket_number=ticket_number,
        subject=body.subject,
        body=body.body,
        severity=body.severity,
        component=body.component,
        site_name=body.site_name,
        created_by=user.user_id,
        sla_due_at=sla_due,
    )

    # initial message from ticket body
    if body.body:
        await TicketMessageRepo(session).create(
            ticket_id=ticket.id,
            author_id=user.user_id,
            author_email=user.email,
            body=body.body,
        )

    await AuditRepo(session).append(
        actor_id=user.user_id,
        actor_email=user.email,
        action="ticket.created",
        subject_type="ticket",
        subject_id=ticket.id,
        tenant_id=tenant_id,
        detail={"subject": body.subject, "severity": body.severity},
    )
    await session.commit()
    return _ticket_dict(ticket)


@router.get("/")
async def list_tickets(
    tenant_id: str,
    status: str | None = None,
    severity: str | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 50,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_tenant_permission("support.view")),
) -> dict:
    items, total = await SupportTicketRepo(session).list_by_tenant(
        tenant_id, status=status, severity=severity, search=search,
        page=page, page_size=page_size,
    )
    return {"items": [_ticket_dict(t) for t in items], "total": total, "page": page}


@router.get("/{ticket_id}")
async def get_ticket(
    tenant_id: str,
    ticket_id: str,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_tenant_permission("support.view")),
) -> dict:
    ticket = await SupportTicketRepo(session).get_by_id(ticket_id)
    if ticket is None or ticket.tenant_id != tenant_id:
        raise HTTPException(404, "ticket not found")

    # tenant users don't see internal notes
    include_internal = user.is_platform_user
    messages = await TicketMessageRepo(session).list_by_ticket(
        ticket_id, include_internal=include_internal,
    )
    state_changes = await TicketStateChangeRepo(session).list_by_ticket(ticket_id)

    return {
        "ticket": _ticket_dict(ticket),
        "messages": [_message_dict(m) for m in messages],
        "state_changes": [
            {
                "from_status": sc.from_status,
                "to_status": sc.to_status,
                "changed_by": sc.changed_by,
                "changed_at": sc.changed_at.isoformat() if sc.changed_at else None,
            }
            for sc in state_changes
        ],
    }


@router.post("/{ticket_id}/reply")
async def reply_to_ticket(
    tenant_id: str,
    ticket_id: str,
    body: ReplyRequest,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_tenant_permission("support.view")),
) -> dict:
    ticket = await SupportTicketRepo(session).get_by_id(ticket_id)
    if ticket is None or ticket.tenant_id != tenant_id:
        raise HTTPException(404, "ticket not found")
    if ticket.status == "closed":
        raise HTTPException(400, "cannot reply to a closed ticket")

    # only platform users can post internal notes
    is_internal = body.is_internal and user.is_platform_user

    msg = await TicketMessageRepo(session).create(
        ticket_id=ticket_id,
        author_id=user.user_id,
        author_email=user.email,
        body=body.body,
        is_internal=is_internal,
    )

    await SupportTicketRepo(session).update(ticket)  # bumps updated_at
    await session.commit()
    return _message_dict(msg)


@router.post("/{ticket_id}/close")
async def close_ticket(
    tenant_id: str,
    ticket_id: str,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_tenant_permission("support.view")),
) -> dict:
    ticket = await SupportTicketRepo(session).get_by_id(ticket_id)
    if ticket is None or ticket.tenant_id != tenant_id:
        raise HTTPException(404, "ticket not found")
    if ticket.status == "closed":
        raise HTTPException(400, "ticket already closed")

    old_status = ticket.status
    await SupportTicketRepo(session).update(ticket, status="closed", closed_at=utcnow())
    await TicketStateChangeRepo(session).append(
        ticket_id=ticket_id,
        from_status=old_status,
        to_status="closed",
        changed_by=user.user_id,
    )
    await AuditRepo(session).append(
        actor_id=user.user_id,
        actor_email=user.email,
        action="ticket.closed",
        subject_type="ticket",
        subject_id=ticket_id,
        tenant_id=tenant_id,
    )
    await session.commit()
    return _ticket_dict(ticket)


# ── admin endpoints ─────────────────────────────────────────────────


@admin_router.get("/")
async def admin_list_tickets(
    status: str | None = None,
    severity: str | None = None,
    assigned_to: str | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 50,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_role("platform_super_admin", "platform_support")),
) -> dict:
    items, total = await SupportTicketRepo(session).list_all(
        status=status, severity=severity, assigned_to=assigned_to,
        search=search, page=page, page_size=page_size,
    )
    return {"items": [_ticket_dict(t) for t in items], "total": total, "page": page}


@admin_router.patch("/{ticket_id}")
async def admin_update_ticket(
    ticket_id: str,
    body: UpdateTicketRequest,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_role("platform_super_admin", "platform_support")),
) -> dict:
    ticket = await SupportTicketRepo(session).get_by_id(ticket_id)
    if ticket is None:
        raise HTTPException(404, "ticket not found")

    updates = {}
    if body.status is not None:
        if body.status not in _VALID_STATUSES:
            raise HTTPException(400, f"invalid status: {body.status}")
        old_status = ticket.status
        updates["status"] = body.status
        if body.status == "closed":
            updates["closed_at"] = utcnow()
        await TicketStateChangeRepo(session).append(
            ticket_id=ticket_id,
            from_status=old_status,
            to_status=body.status,
            changed_by=user.user_id,
        )
    if body.assigned_to is not None:
        updates["assigned_to"] = body.assigned_to
    if body.severity is not None:
        if body.severity not in _VALID_SEVERITIES:
            raise HTTPException(400, f"invalid severity: {body.severity}")
        updates["severity"] = body.severity

    if updates:
        await SupportTicketRepo(session).update(ticket, **updates)

    await AuditRepo(session).append(
        actor_id=user.user_id,
        actor_email=user.email,
        action="ticket.updated",
        subject_type="ticket",
        subject_id=ticket_id,
        tenant_id=ticket.tenant_id,
        detail=updates,
    )
    await session.commit()
    return _ticket_dict(ticket)


@admin_router.post("/{ticket_id}/internal-note")
async def admin_internal_note(
    ticket_id: str,
    body: ReplyRequest,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_role("platform_super_admin", "platform_support")),
) -> dict:
    ticket = await SupportTicketRepo(session).get_by_id(ticket_id)
    if ticket is None:
        raise HTTPException(404, "ticket not found")

    msg = await TicketMessageRepo(session).create(
        ticket_id=ticket_id,
        author_id=user.user_id,
        author_email=user.email,
        body=body.body,
        is_internal=True,
    )
    await session.commit()
    return _message_dict(msg)


# ── support mode (24h access) ────────────────────────────────────────

support_mode_router = APIRouter(prefix="/api/admin/support-access", tags=["support-mode"])


class RequestAccessBody(BaseModel):
    reason: str = Field("", max_length=1000)


@support_mode_router.post("/{tenant_id}/request")
async def request_support_access(
    tenant_id: str,
    body: RequestAccessBody,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_role("platform_super_admin", "platform_support")),
) -> dict:
    """Ask to enter a tenant. Grants nothing on its own."""
    tenant = await TenantRepo(session).get_by_id(tenant_id)
    if tenant is None:
        raise HTTPException(404, "tenant not found")

    repo = SupportAccessLogRepo(session)
    # Both checks are per-requester: engineer A's grant or pending request
    # must not block (nor admit) engineer B.
    existing = await repo.get_active(tenant_id, user_id=user.user_id)
    if existing is not None:
        return {"status": "already_active", "access": _access_dict(existing)}
    pending = await repo.get_pending(tenant_id, requested_by=user.user_id)
    if pending is not None:
        return {"status": "already_requested", "access": _access_dict(pending)}

    entry = await repo.request(
        tenant_id=tenant_id, requested_by=user.user_id, reason=body.reason,
    )
    await AuditRepo(session).append(
        actor_id=user.user_id,
        actor_email=user.email,
        action="support_access.requested",
        subject_type="tenant",
        subject_id=tenant_id,
        tenant_id=tenant_id,
        detail={"reason": body.reason},
    )
    try:
        await session.commit()
    except IntegrityError:
        # Two concurrent requests raced past the read check; the partial
        # unique index (tenant_id, requested_by) WHERE requested caught the
        # second. Same outcome as the sequential case.
        await session.rollback()
        pending = await repo.get_pending(tenant_id, requested_by=user.user_id)
        return {"status": "already_requested",
                "access": _access_dict(pending) if pending else None}
    return {"status": "requested", "access": _access_dict(entry)}


@support_mode_router.get("/requests/pending")
async def list_pending_support_access(
    session: AsyncSession = Depends(get_session),
    _user: UserContext = Depends(require_super_admin),
) -> dict:
    """The approver's queue. Only the approver sees it.

    A14 (OQ-25): each row carries the engineer's prior denial history for
    that tenant — a denial never permanently denies the person, but it is
    never hidden from the person making the next decision.
    """
    repo = SupportAccessLogRepo(session)
    rows = await repo.list_pending()
    items = []
    for r in rows:
        d = _access_dict(r)
        count, last = await repo.denial_history(r.tenant_id, r.requested_by)
        d["prior_denials"] = {
            "count": count,
            "last_denied_at": (
                last.denied_at.isoformat() if last and last.denied_at else None
            ),
            "last_reason": last.reason if last else None,
        }
        items.append(d)
    return {"items": items}


async def _settle_request(
    request_id: str, session: AsyncSession, user: UserContext, verb: str,
) -> dict:
    """Shared approve/deny skeleton: load, gate on 'requested', mutate,
    audit, commit. Only the repo verb and audit detail differ."""
    repo = SupportAccessLogRepo(session)
    entry = await repo.get_by_id(request_id)
    if entry is None:
        raise HTTPException(404, "support access request not found")
    if entry.status != "requested":
        raise HTTPException(400, f"request is {entry.status}, not requested")

    detail = {"request_id": request_id, "requested_by": entry.requested_by}
    if verb == "approved":
        await repo.approve(entry, user.user_id)
        detail["expires_at"] = entry.expires_at.isoformat()
    else:
        await repo.deny(entry, user.user_id)
    await AuditRepo(session).append(
        actor_id=user.user_id,
        actor_email=user.email,
        action=f"support_access.{verb}",
        subject_type="tenant",
        subject_id=entry.tenant_id,
        tenant_id=entry.tenant_id,
        detail=detail,
    )
    await session.commit()
    return {"status": verb, "access": _access_dict(entry)}


@support_mode_router.post("/requests/{request_id}/approve")
async def approve_support_access(
    request_id: str,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_super_admin),
) -> dict:
    """Grant a pending request (SUPPORT_ACCESS_TTL_HOURS).

    ``require_super_admin`` is the point of the slice: platform_support
    raises the request and cannot approve it, so no internal person places
    themselves inside a customer tenant. The clock starts here rather than
    at request time, so a request that waited in the queue does not arrive
    already half spent. The grant admits its requester only.
    """
    return await _settle_request(request_id, session, user, "approved")


@support_mode_router.post("/requests/{request_id}/deny")
async def deny_support_access(
    request_id: str,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_super_admin),
) -> dict:
    return await _settle_request(request_id, session, user, "denied")


@support_mode_router.post("/{tenant_id}/revoke")
async def revoke_support_access(
    tenant_id: str,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_role("platform_super_admin", "platform_support")),
) -> dict:
    caller = None if user.role == "platform_super_admin" else user.user_id
    entry = await SupportAccessLogRepo(session).get_active(tenant_id, user_id=caller)
    if entry is None:
        raise HTTPException(404, "no active support access for this tenant")

    await SupportAccessLogRepo(session).revoke(entry, user.user_id)

    await AuditRepo(session).append(
        actor_id=user.user_id,
        actor_email=user.email,
        action="support_access.revoked",
        subject_type="tenant",
        subject_id=tenant_id,
        tenant_id=tenant_id,
    )
    await session.commit()
    return {"status": "revoked", "access": _access_dict(entry)}


@support_mode_router.get("/{tenant_id}")
async def get_support_access_status(
    tenant_id: str,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_role("platform_super_admin", "platform_support")),
) -> dict:
    repo = SupportAccessLogRepo(session)
    # Scoped to the CALLER for support (their grant, their request);
    # tenant-wide for the super admin, who is auditing the tenant's state
    # rather than asking about their own access.
    caller = None if user.role == "platform_super_admin" else user.user_id
    entry = await repo.get_active(tenant_id, user_id=caller)
    if entry is not None:
        return {"active": True, "access": _access_dict(entry)}
    # Distinguish "waiting on an approver" from "nobody has asked": the UI
    # offers a request button in one case and a status line in the other.
    pending = await repo.get_pending(tenant_id, requested_by=caller)
    if pending is not None:
        return {"active": False, "pending": True, "access": _access_dict(pending)}
    return {"active": False, "pending": False}
