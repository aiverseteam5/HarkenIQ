"""Learning feedback API (QA-033, R-C1).

Surfaces the fleet learning loop: candidate skills received from Site
Managers and the LearningFeedbackTracker's cycles (pattern → skill →
distribution → measured improvement → promotion recommendation).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_cc.api.deps import get_session, require_permission
from harkeniq_cc.auth import UserContext
from harkeniq_cc.db.repos import (
    CandidateSkillRepo,
    LearnedSignalRepo,
    LearningCycleRepo,
)

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
    status: str | None = None,
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """R-C1 learning cycles, from the DURABLE ledger (S3).

    These used to be read from the intelligence engine's in-process
    tracker, so the record of what the fleet learned vanished on restart.
    The tracker is still the live working set; `cc_learning_cycles` is the
    ledger, written at the end of every detection pass.
    """
    rows = await LearningCycleRepo(session).list_cycles(
        user.tenant_id, status=status,
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
                "outcomes_before": c.outcomes_before or {},
                "outcomes_after": c.outcomes_after or {},
                "improvement_pct": c.improvement_pct,
                # Recommended is not promoted: promotion stays governed by
                # the marketplace human review path.
                "promotion_recommended": c.promotion_recommended,
                "status": c.status,
                "started_at": c.started_at.isoformat() if c.started_at else None,
                "updated_at": c.updated_at.isoformat() if c.updated_at else None,
                "completed_at": (
                    c.completed_at.isoformat() if c.completed_at else None
                ),
            }
            for c in rows
        ],
        "tenant_id": user.tenant_id,
    }


@router.get("/signals")
async def list_signals(
    scope_type: str | None = None,
    scope_ref: str | None = None,
    user: UserContext = Depends(require_permission("fleet.view")),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Durable learned signals — the knowledge the loop produced (S3).

    This is the contract a future Operational Agent consumes as evidence:
    "this cohort/site exhibits X, historical outcomes show Y, confidence Z".
    It is knowledge, not authority: nothing here permits an action, and
    every consumer still passes through the governed capability path.
    """
    rows = await LearnedSignalRepo(session).list_active(
        user.tenant_id, scope_type=scope_type, scope_ref=scope_ref,
    )
    return {
        "signals": [
            {
                "signal_key": s.signal_key,
                "scope_type": s.scope_type,
                "scope_ref": s.scope_ref,
                "action_type": s.action_type,
                "vendor": s.vendor,
                "model": s.model,
                "statement": s.statement,
                "evidence": s.evidence or {},
                "confidence": s.confidence,
                "observation_count": s.observation_count,
                "source_pattern_id": s.source_pattern_id,
                "source_cycle_id": s.source_cycle_id,
                "first_observed_at": (
                    s.first_observed_at.isoformat() if s.first_observed_at else None
                ),
                "last_confirmed_at": (
                    s.last_confirmed_at.isoformat() if s.last_confirmed_at else None
                ),
            }
            for s in rows
        ],
        "tenant_id": user.tenant_id,
    }
