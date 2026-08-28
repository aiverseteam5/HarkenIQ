"""Learning feedback API (QA-033, R-C1).

Surfaces the fleet learning loop: candidate skills received from Site
Managers and the LearningFeedbackTracker's cycles (pattern → skill →
distribution → measured improvement → promotion recommendation).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_cc.api.deps import get_cc_state, get_session, require_permission
from harkeniq_cc.auth import UserContext
from harkeniq_cc.db.repos import CandidateSkillRepo

router = APIRouter(prefix="/api/learning", tags=["learning"])


@router.get("/candidates")
async def list_candidates(
    status: str | None = None,
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Candidate skills received from Site Managers, tenant-scoped."""
    rows = await CandidateSkillRepo(session).list_candidates(
        user.tenant_id, status=status,
    )
    return {
        "candidates": [
            {
                "skill_id": r.skill_id,
                "site_id": r.site_id,
                "source_device": r.source_device,
                "source_component": r.source_component,
                "validation_state": r.validation_state,
                "warnings": r.warnings or [],
                "dry_run_matches": r.dry_run_matches,
                "status": r.status,
                "cycle_id": r.cycle_id,
                "generated_at": r.generated_at.isoformat(),
                "received_at": r.received_at.isoformat(),
                "yaml_text": r.yaml_text,
            }
            for r in rows
        ],
    }


@router.get("/cycles")
async def list_cycles(
    user: UserContext = Depends(require_permission("fleet.view")),
    state=Depends(get_cc_state),
) -> dict:
    """R-C1 learning cycles from the live intelligence engine.

    Cycles are in-process state (they reset with the loop's aggregation
    cursor on restart, by design — see intelligence.py); candidate rows
    are the durable record.
    """
    engine = getattr(state, "intelligence", None)
    if engine is None:
        return {"cycles": [], "promotions": []}
    tracker = engine.feedback
    cycles = list(tracker.get_active_cycles()) + list(
        tracker.get_completed_cycles()
    )
    return {
        "cycles": [
            {
                "cycle_id": c.cycle_id,
                "pattern_id": c.pattern_id,
                "pattern_type": c.pattern_type,
                "skill_id": c.skill_id,
                "sites_distributed": c.sites_distributed,
                "devices_applied": c.devices_applied,
                "outcomes_before": c.outcomes_before,
                "outcomes_after": c.outcomes_after,
                "improvement_pct": c.improvement_pct,
                "promoted": c.promoted,
                "started_at": c.started_at,
                "completed_at": c.completed_at,
            }
            for c in cycles
        ],
        "promotions": tracker.promotions,
    }
