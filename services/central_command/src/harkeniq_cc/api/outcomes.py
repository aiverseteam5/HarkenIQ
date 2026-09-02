"""Outcomes API: vendor reliability metrics and detected fleet patterns (R4-1).

Metrics are computed on demand from cc_outcome_history (tenant-scoped via
cc_sites), so the endpoint does not depend on the intelligence loop's
timing. Patterns are read from cc_fleet_patterns, persisted by the loop.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_cc.api.deps import forbid_out_of_scope, get_scope, get_session, require_permission
from harkeniq_cc.auth import UserContext
from harkeniq_cc.db.repos import FleetPatternRepo, OutcomeHistoryRepo
from harkeniq_cc.outcome_aggregator import OutcomeAggregator

router = APIRouter(prefix="/api/outcomes", tags=["outcomes"])


@router.get(
    "/metrics",
    dependencies=[Depends(require_permission("fleet.view"))],
)
async def outcome_metrics(
    action_type: str | None = None,
    vendor: str | None = None,
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    """Aggregated action-outcome metrics grouped by (action_type, vendor,
    model), with per-site attribution. Backing data for the Console's
    vendor reliability comparison."""
    outcomes = await OutcomeHistoryRepo(session).list_outcome_dicts(
        user.tenant_id, scope=scope
    )
    aggregator = OutcomeAggregator()
    aggregator.ingest(outcomes)
    metrics = aggregator.get_metrics(action_type=action_type, vendor=vendor)
    return {
        "metrics": [
            {
                "action_type": m.action_type,
                "vendor": m.vendor,
                "model": m.model,
                "total_count": m.total_count,
                "success_count": m.success_count,
                "failure_count": m.failure_count,
                "partial_count": m.partial_count,
                "success_rate": round(m.success_rate, 4),
                "failure_rate": round(m.failure_rate, 4),
                "resolution_rate": round(m.resolution_rate, 4),
                "site_count": m.site_count,
                "failing_site_count": m.failing_site_count,
                "sites": sorted(m.site_counts),
            }
            for m in metrics
        ],
        "total_outcomes": len(outcomes),
        "tenant_id": user.tenant_id,
    }


@router.get(
    "/patterns",
    dependencies=[Depends(require_permission("fleet.view"))],
)
async def list_patterns(
    pattern_type: str | None = None,
    status: str | None = Query("active"),
    limit: int = Query(200, ge=1, le=1000),
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
    scope=Depends(get_scope),
) -> dict:
    """Detected fleet patterns (batch_failure, cross_site_batch, anomaly,
    reliability), newest first.

    A23: a pattern is tenant-level knowledge (a vendor/model cohort), but
    its evidence names the SITES it was detected across. A scoped caller
    reads the pattern with the site evidence narrowed to their own sites;
    a pattern whose named sites are all outside their scope is absent.
    """
    rows = await FleetPatternRepo(session).list_patterns(
        pattern_type=pattern_type, status=status or None, limit=limit,
        tenant_id=user.tenant_id,
    )
    return {
        "patterns": [
            {
                "pattern_id": r.id,
                "pattern_type": r.pattern_type,
                "description": r.description,
                "affected_scope": _narrow_sites(r.affected_scope or {}, scope),
                "confidence": r.confidence,
                "evidence": _narrow_sites(r.evidence or {}, scope),
                "status": r.status,
                "detected_at": r.detected_at.isoformat() if r.detected_at else None,
            }
            for r in rows
            if _pattern_visible(r, scope)
        ],
        "tenant_id": user.tenant_id,
    }


_SITE_KEYS = ("sites", "site_failure_counts")


def _visible_sites(scope) -> set[str] | None:
    if scope is None or getattr(scope, "tenant_wide", False):
        return None
    return set(getattr(scope, "site_ids", ()) or ())


def _narrow_sites(payload: dict, scope) -> dict:
    """Drop site identifiers the caller may not see from a JSON blob."""
    visible = _visible_sites(scope)
    if visible is None:
        return payload
    out = dict(payload)
    for key in _SITE_KEYS:
        value = out.get(key)
        if isinstance(value, dict):
            out[key] = {k: v for k, v in value.items() if k in visible}
        elif isinstance(value, list):
            out[key] = [s for s in value if s in visible]
    return out


def _pattern_visible(row, scope) -> bool:
    """A pattern that names sites is visible when at least one is the
    caller's; one that names no site is cohort knowledge and visible."""
    visible = _visible_sites(scope)
    if visible is None:
        return True
    named: set[str] = set()
    for blob in (row.affected_scope or {}, row.evidence or {}):
        for key in _SITE_KEYS:
            value = blob.get(key)
            if isinstance(value, dict):
                named |= set(value)
            elif isinstance(value, list):
                named |= set(value)
    return not named or bool(named & visible)
