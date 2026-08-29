"""Incidents API: what happened, why, and what should happen next (S4).

Real incidents, projected from the Site Manager that consolidated them,
with the diagnosis attached. Replaces the critical-health pseudo-incidents
the fleet API used to synthesise.

Provenance is explicit, and that is a security property, not decoration
------------------------------------------------------------------------
The diagnosis text is produced by a reasoning provider. When that provider
is the LLM, the text was generated from device telemetry — which is
attacker-influenceable if a BMC is compromised (Platform-Design already
treats telemetry as untrusted at prompt time). This contract will be read
by a future Operational Agent that is itself a language model, so every
diagnosis carries `origin` and `trust`, and generated fields are grouped
under a `generated` block. A consumer must treat that content as evidence
to reason ABOUT, never as instruction to follow.

Read-only: `incident.view`. Nothing here mutates or authorises anything.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_cc.api.deps import get_session, require_permission
from harkeniq_cc.auth import UserContext
from harkeniq_cc.db.repos import (
    ApprovalRouteRepo,
    IncidentRepo,
    LearnedSignalRepo,
    SiteRepo,
)
from harkeniq_cc.learned_signals import signals_for_device

router = APIRouter(prefix="/api/incidents", tags=["incidents"])

#: Reasoning providers whose output is model-generated free text.
_GENERATED_PROVIDERS = {"llm"}


def _diagnosis(explanation: dict | None) -> dict | None:
    """Shape the reasoning result, with its provenance stated up front."""
    if not explanation:
        return None
    provider = explanation.get("provider", "unknown")
    generated = provider in _GENERATED_PROVIDERS
    return {
        "origin": provider,
        # Consumers (especially model-driven ones) must know whether this
        # text was generated from telemetry before they reason with it.
        "trust": "untrusted_generated" if generated else "deterministic",
        "confidence": explanation.get("confidence", 0.0),
        "generated": {
            "summary": explanation.get("summary", ""),
            "suggested_action": explanation.get("suggested_action", ""),
            "reasoning_steps": explanation.get("reasoning_steps", []),
        },
        # Citations and prior incidents are references the platform itself
        # produced, not free text the model invented.
        "evidence_cited": explanation.get("evidence_cited", []),
        "similar_past_incidents": explanation.get("similar_past_incidents", []),
    }


def _incident_dict(row, site_names: dict) -> dict:
    return {
        "incident_id": row.incident_id,
        "kind": row.kind,
        "status": row.status,
        "title": row.title,
        "device_agent_id": row.device_agent_id,
        "subsystem": row.subsystem,
        "site_id": row.site_id,
        "site_name": site_names.get(row.site_id, ""),
        "parent_incident_id": row.parent_incident_id,
        "is_parent": row.parent_incident_id is None,
        "confidence": row.confidence,
        # A2/A1.1: an inferred fault domain produces a lower-confidence
        # conclusion, and the surface must say so rather than presenting it
        # as confirmed.
        "inferred": row.inferred,
        "correlation": row.correlation_meta or {},
        "diagnosis": _diagnosis(row.explanation),
        "opened_at": row.opened_at.isoformat() if row.opened_at else None,
        "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
        "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
    }


@router.get(
    "/",
    dependencies=[Depends(require_permission("incident.view"))],
)
async def list_incidents(
    status: str = Query("open", description="open|resolved|all"),
    site_id: str | None = None,
    device_agent_id: str | None = None,
    limit: int = Query(200, ge=1, le=1000),
    user: UserContext = Depends(require_permission("incident.view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Incidents for the tenant, parents first with their children nested.

    Consolidation is preserved: the Site Manager groups correlated faults
    under one parent, and flattening that here would show N incidents for
    one root cause — exactly what consolidation exists to prevent.
    """
    repo = IncidentRepo(session)
    rows = await repo.list_incidents(
        user.tenant_id,
        status=None if status == "all" else status,
        site_id=site_id,
        device_agent_id=device_agent_id,
        limit=limit,
    )
    sites = await SiteRepo(session).list_all(user.tenant_id)
    site_names = {s.id: s.site_name for s in sites}

    by_id = {r.incident_id: r for r in rows}
    parents = [r for r in rows if r.parent_incident_id is None]
    children_by_parent: dict[str, list] = {}
    for r in rows:
        if r.parent_incident_id:
            children_by_parent.setdefault(r.parent_incident_id, []).append(r)

    items = []
    for parent in parents:
        entry = _incident_dict(parent, site_names)
        entry["children"] = [
            _incident_dict(c, site_names)
            for c in children_by_parent.get(parent.incident_id, [])
        ]
        entry["child_count"] = len(entry["children"])
        items.append(entry)

    # A child whose parent is outside this page (or already resolved) is
    # still shown, rather than disappearing into a parent nobody listed.
    orphans = [
        _incident_dict(r, site_names)
        for r in rows
        if r.parent_incident_id and r.parent_incident_id not in by_id
    ]
    for o in orphans:
        o["children"] = []
        o["child_count"] = 0
    items.extend(orphans)

    return {
        "incidents": items,
        "total": len(items),
        "diagnosed": sum(1 for i in items if i["diagnosis"]),
        "tenant_id": user.tenant_id,
    }


@router.get(
    "/{incident_id}",
    dependencies=[Depends(require_permission("incident.view"))],
)
async def get_incident(
    incident_id: str,
    user: UserContext = Depends(require_permission("incident.view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """One incident with its children, prior learning, and what comes next."""
    repo = IncidentRepo(session)
    row = await repo.get(user.tenant_id, incident_id)
    if row is None:
        raise HTTPException(status_code=404, detail="incident not found")

    sites = await SiteRepo(session).list_all(user.tenant_id)
    site_names = {s.id: s.site_name for s in sites}
    entry = _incident_dict(row, site_names)
    children = await repo.children_of(user.tenant_id, incident_id)
    entry["children"] = [_incident_dict(c, site_names) for c in children]
    entry["child_count"] = len(entry["children"])

    # S3 -> S4: has the fleet seen this before? Learned signals for the
    # affected device's cohort/site are prior knowledge, carried with the
    # same untrusted/deterministic distinction as the diagnosis.
    from harkeniq_cc.db.repos import FleetCacheRepo

    device = None
    if row.device_agent_id:
        device = await FleetCacheRepo(session).get_by_agent_id(row.device_agent_id)
    if device is not None and device.site_id == row.site_id:
        signals = await LearnedSignalRepo(session).list_active(user.tenant_id)
        entry["prior_learning"] = signals_for_device(
            signals, device.vendor, device.model, row.site_id,
        )
    else:
        entry["prior_learning"] = []

    # What is already waiting on a human for this device. The incident
    # names the governed next step; it never performs one.
    pending = [
        {"action_id": r.action_id, "action_type": r.action_type}
        for r in await ApprovalRouteRepo(session).list_pending(user.tenant_id)
        if r.device_agent_id == row.device_agent_id
    ]
    entry["current_state"] = {
        "pending_approvals": pending,
        "open_action_count": len(pending),
    }
    if pending:
        entry["recommended_next"] = {
            "capability": "review_pending_approval",
            "summary": f"{len(pending)} action awaiting a human decision.",
            "requires_approval": True,
            "available": True,
            "refs": [p["action_id"] for p in pending],
        }
    elif entry["diagnosis"] and entry["diagnosis"]["generated"]["suggested_action"]:
        entry["recommended_next"] = {
            "capability": "propose_action",
            # The suggestion is quoted, not executed — and it is generated
            # text, so it stays a human's decision to act on.
            "summary": entry["diagnosis"]["generated"]["suggested_action"],
            "requires_approval": True,
            "available": False,
            "unavailable_reason": (
                "Acting on a diagnosis is proposed by the device's agent "
                "through its own safety gates, not from this surface."
            ),
            "refs": [],
        }
    else:
        entry["recommended_next"] = {
            "capability": "investigate",
            "summary": "No diagnosis yet; evidence is still being gathered.",
            "requires_approval": False,
            "available": True,
            "refs": [],
        }
    return entry
