"""Platform admin dashboard, feature toggles, release management, health endpoints."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_console.api.deps import get_session, require_super_admin
from harkeniq_console.auth import UserContext
from harkeniq_console.db.repos import (
    AuditRepo,
    FeatureFlagRepo,
    InvoiceRepo,
    SubscriptionRepo,
    SupportTicketRepo,
    TenantRepo,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _tenant_health_dict(t, sub, open_tickets) -> dict:
    return {
        "id": t.id,
        "name": t.name,
        "slug": t.slug,
        "status": t.status,
        "delinquency_status": t.delinquency_status,
        "billing_country": t.billing_country,
        "currency": t.currency,
        "plan": sub.plan if sub else "none",
        "node_commit": sub.node_commit if sub else 0,
        "billing_frequency": sub.billing_frequency if sub else None,
        "open_tickets": open_tickets,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


# ── Admin Dashboard ──────────────────────────────────────────────────


@router.get("/dashboard")
async def dashboard(
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_super_admin),
) -> dict:
    tenant_count = await TenantRepo(session).count()
    open_tickets = await SupportTicketRepo(session).count_open()

    # MRR from paid invoices
    revenue = await InvoiceRepo(session).get_revenue_by_plan()
    total_paid = sum(r["total_cents"] for r in revenue)

    # total committed nodes across active subscriptions
    from sqlalchemy import select, func
    from harkeniq_console.db.models import Subscription
    node_result = await session.execute(
        select(func.sum(Subscription.node_commit)).where(
            Subscription.status == "active",
        )
    )
    total_nodes = node_result.scalar_one() or 0

    return {
        "active_tenants": tenant_count,
        "total_nodes": total_nodes,
        "total_revenue_cents": total_paid,
        "open_tickets": open_tickets,
        "revenue_by_type": revenue,
    }


@router.get("/tenants/health")
async def tenants_health(
    page: int = 1,
    page_size: int = 50,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_super_admin),
) -> dict:
    tenants, total = await TenantRepo(session).list_filtered(
        page=page, page_size=page_size,
    )
    items = []
    for t in tenants:
        sub = await SubscriptionRepo(session).get_by_tenant(t.id)
        open_t = await SupportTicketRepo(session).count_open(t.id)
        items.append(_tenant_health_dict(t, sub, open_t))
    return {"items": items, "total": total, "page": page}


@router.get("/events/recent")
async def recent_events(
    limit: int = 20,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_super_admin),
) -> dict:
    entries, _ = await AuditRepo(session).list_filtered(page=1, page_size=limit)
    return {
        "items": [
            {
                "id": e.id,
                "ts": e.ts.isoformat() if e.ts else None,
                "action": e.action,
                "actor_email": e.actor_email,
                "subject_type": e.subject_type,
                "subject_id": e.subject_id,
                "tenant_id": e.tenant_id,
            }
            for e in entries
        ]
    }


# ── System Health ────────────────────────────────────────────────────


@router.get("/system")
async def system_info(
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_super_admin),
) -> dict:
    # Check DB connectivity
    db_ok = False
    try:
        from sqlalchemy import text
        await session.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        pass

    return {
        "services": {
            "console": {"status": "healthy", "version": "0.1.0"},
            "database": {"status": "healthy" if db_ok else "unhealthy"},
        },
        "uptime": "unknown",
    }


@router.get("/health/detailed")
async def detailed_health(
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_super_admin),
) -> dict:
    from sqlalchemy import text

    checks = {}

    # DB check
    try:
        await session.execute(text("SELECT 1"))
        checks["database"] = {"status": "healthy", "latency_ms": 0}
    except Exception as exc:
        checks["database"] = {"status": "unhealthy", "error": str(exc)}

    # Table counts as a proxy for DB health
    try:
        from harkeniq_console.db.models import Tenant, Invoice, SupportTicket
        from sqlalchemy import select, func
        tenant_count = (await session.execute(select(func.count()).select_from(Tenant))).scalar_one()
        invoice_count = (await session.execute(select(func.count()).select_from(Invoice))).scalar_one()
        ticket_count = (await session.execute(select(func.count()).select_from(SupportTicket))).scalar_one()
        checks["data"] = {
            "tenants": tenant_count,
            "invoices": invoice_count,
            "tickets": ticket_count,
        }
    except Exception:
        checks["data"] = {"status": "error"}

    return {
        "services": {
            "console": {"status": "healthy", "version": "0.1.0"},
            "central_command": {"status": "unknown", "note": "requires gRPC probe"},
            "keycloak": {"status": "unknown", "note": "requires HTTP probe"},
        },
        "checks": checks,
    }


# ── Feature Toggles ──────────────────────────────────────────────────


@router.get("/features")
async def list_features(
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_super_admin),
) -> dict:
    globals_ = await FeatureFlagRepo(session).list_globals()
    return {
        "items": [
            {
                "id": f.id,
                "feature_name": f.feature_name,
                "enabled": f.enabled,
                "tenant_id": f.tenant_id,
                "updated_by": f.updated_by,
                "updated_at": f.updated_at.isoformat() if f.updated_at else None,
            }
            for f in globals_
        ]
    }


@router.get("/features/tenant/{tenant_id}")
async def list_tenant_features(
    tenant_id: str,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_super_admin),
) -> dict:
    flags = await FeatureFlagRepo(session).list_by_tenant(tenant_id)
    return {
        "items": [
            {
                "id": f.id,
                "feature_name": f.feature_name,
                "enabled": f.enabled,
                "tenant_id": f.tenant_id,
                "updated_by": f.updated_by,
                "updated_at": f.updated_at.isoformat() if f.updated_at else None,
            }
            for f in flags
        ]
    }


class ToggleFeatureRequest(BaseModel):
    enabled: bool


@router.put("/features/tenant/{tenant_id}/{feature_name}")
async def toggle_tenant_feature(
    tenant_id: str,
    feature_name: str,
    body: ToggleFeatureRequest,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_super_admin),
) -> dict:
    tenant = await TenantRepo(session).get_by_id(tenant_id)
    if tenant is None:
        raise HTTPException(404, "tenant not found")

    flag = await FeatureFlagRepo(session).set_flag(
        tenant_id, feature_name, body.enabled, updated_by=user.user_id,
    )
    await AuditRepo(session).append(
        actor_id=user.user_id,
        actor_email=user.email,
        action="feature_flag.toggled",
        subject_type="feature_flag",
        subject_id=flag.id,
        tenant_id=tenant_id,
        detail={"feature": feature_name, "enabled": body.enabled},
    )
    await session.commit()
    return {
        "id": flag.id,
        "feature_name": flag.feature_name,
        "enabled": flag.enabled,
        "tenant_id": flag.tenant_id,
    }


@router.put("/features/global/{feature_name}")
async def toggle_global_feature(
    feature_name: str,
    body: ToggleFeatureRequest,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_super_admin),
) -> dict:
    # global flags have tenant_id=None — use set_flag with special handling
    from harkeniq_console.db.models import FeatureFlag
    from sqlalchemy import select

    existing = (
        await session.execute(
            select(FeatureFlag).where(
                FeatureFlag.tenant_id.is_(None),
                FeatureFlag.feature_name == feature_name,
            )
        )
    ).scalar_one_or_none()

    if existing is None:
        from harkeniq_console.db.models import new_id, utcnow
        existing = FeatureFlag(
            feature_name=feature_name,
            enabled=body.enabled,
            updated_by=user.user_id,
        )
        session.add(existing)
        await session.flush()
    else:
        existing.enabled = body.enabled
        existing.updated_by = user.user_id
        from harkeniq_console.db.models import utcnow
        existing.updated_at = utcnow()
        await session.flush()

    await AuditRepo(session).append(
        actor_id=user.user_id,
        actor_email=user.email,
        action="feature_flag.global_toggled",
        subject_type="feature_flag",
        subject_id=existing.id,
        detail={"feature": feature_name, "enabled": body.enabled},
    )
    await session.commit()
    return {
        "id": existing.id,
        "feature_name": existing.feature_name,
        "enabled": existing.enabled,
        "tenant_id": None,
    }


# ── Release Management ───────────────────────────────────────────────


@router.get("/releases")
async def list_releases(
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_super_admin),
) -> dict:
    from harkeniq_console.db.repos import SettingsRepo
    repo = SettingsRepo(session)
    releases = await repo.get("platform_releases")
    return {
        "releases": releases.value if releases else {
            "site_manager": {"current": "0.1.0", "latest": "0.1.0"},
            "agent": {"current": "0.1.0", "latest": "0.1.0"},
            "cli": {"current": "0.1.0", "latest": "0.1.0"},
            "skill_packs": {"current": "0.1.0", "latest": "0.1.0"},
        }
    }


class UpdateReleaseRequest(BaseModel):
    component: str
    version: str
    release_notes: str = ""


@router.post("/releases")
async def update_release(
    body: UpdateReleaseRequest,
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_super_admin),
) -> dict:
    from harkeniq_console.db.repos import SettingsRepo
    repo = SettingsRepo(session)
    releases_setting = await repo.get("platform_releases")
    releases = releases_setting.value if releases_setting else {}
    releases[body.component] = {
        "current": body.version,
        "latest": body.version,
        "release_notes": body.release_notes,
    }
    await repo.set("platform_releases", releases, updated_by=user.user_id)
    await AuditRepo(session).append(
        actor_id=user.user_id,
        actor_email=user.email,
        action="release.updated",
        subject_type="release",
        subject_id=body.component,
        detail={"version": body.version},
    )
    await session.commit()
    return {"component": body.component, "version": body.version}


# ── Admin Billing Stats (kept for backwards compat with Phase 4) ─────


@router.get("/billing/overview")
async def billing_overview(
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_super_admin),
) -> dict:
    revenue = await InvoiceRepo(session).get_revenue_by_plan()
    total_paid = sum(r["total_cents"] for r in revenue)

    from sqlalchemy import select, func
    from harkeniq_console.db.models import Subscription
    node_result = await session.execute(
        select(func.sum(Subscription.node_commit)).where(Subscription.status == "active")
    )
    total_nodes = node_result.scalar_one() or 0
    sub_count = (await session.execute(
        select(func.count()).select_from(Subscription).where(Subscription.status == "active")
    )).scalar_one()

    return {
        "mrr_cents": total_paid // max(sub_count, 1) if sub_count else 0,
        "arr_cents": total_paid,
        "active_tenants": sub_count,
        "total_nodes": total_nodes,
        "currency": "USD",
    }


@router.get("/billing/revenue")
async def billing_revenue(
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_super_admin),
) -> dict:
    revenue = await InvoiceRepo(session).get_revenue_by_plan()
    return {"items": revenue}


@router.get("/billing/delinquent")
async def billing_delinquent(
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_super_admin),
) -> dict:
    from sqlalchemy import select
    from harkeniq_console.db.models import Tenant
    tenants = (await session.execute(
        select(Tenant).where(Tenant.delinquency_status != "current")
    )).scalars().all()

    items = []
    for t in tenants:
        overdue_invoices = await InvoiceRepo(session).list_overdue()
        tenant_overdue = [i for i in overdue_invoices if i.tenant_id == t.id]
        amount = sum(i.total_cents for i in tenant_overdue)
        now = datetime.now(timezone.utc)
        days = 0
        if tenant_overdue:
            earliest = min(i.due_at for i in tenant_overdue if i.due_at)
            if earliest:
                if earliest.tzinfo is None:
                    earliest = earliest.replace(tzinfo=timezone.utc)
                days = max(0, (now - earliest).days)
        items.append({
            "tenant_id": t.id,
            "tenant_name": t.name,
            "slug": t.slug,
            "delinquency_status": t.delinquency_status,
            "overdue_amount_cents": amount,
            "days_overdue": days,
            "currency": t.currency,
        })
    return {"items": items}


@router.get("/billing/reconciliation")
async def billing_reconciliation(
    session: AsyncSession = Depends(get_session),
    user: UserContext = Depends(require_super_admin),
) -> dict:
    # Placeholder — real reconciliation runs as a background job
    return {"items": []}
