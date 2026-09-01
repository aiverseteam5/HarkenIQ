"""Attention API: what deserves attention first in this tenant, and why.

S2 (2026-08-29). Read-only prioritization over evidence that already
exists. This router only FETCHES tenant-scoped inputs and hands them to
the pure composer in `harkeniq_cc.attention`; all judgement lives there
and is unit-testable without a database.

Governance: `fleet.view` (read-only intelligence). It names governed
capabilities in `recommended_next` but performs none of them and confers
no authority — invoking anything still goes through that capability's own
permission and approval path.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from harkeniq_cc.api.deps import get_scope, get_session, require_permission
from harkeniq_cc.auth import UserContext
from harkeniq_cc.governance import load_attention

router = APIRouter(prefix="/api/attention", tags=["attention"])


@router.get(
    "/",
    dependencies=[Depends(require_permission("fleet.view"))],
)
async def attention(
    site_id: str | None = Query(None, description="restrict to one site"),
    band: str | None = Query(
        None, description="filter: high|medium|low|insufficient_data"
    ),
    limit: int = Query(200, ge=1, le=1000),
    user: UserContext = Depends(require_permission("fleet.view")),
    scope=Depends(get_scope),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Ranked attention list plus a site rollup, with evidence and the
    next governed capability for each device.

    A thin caller over the ONE composer (A22.11). This router used to
    carry a near-verbatim copy whose `band` filter ran before ranking, so
    an operator filtering to "high" and an Operational Agent reading the
    same tenant saw different priorities for identical state -- and rank
    decides which devices consume an agent's proposal budget.

    E1.2 scope is now applied (A22.9). It never was: the route was
    declared READ_SCOPED and filtered nothing, so a site-scoped principal
    read every site. This is also the one read every Operational Agent is
    required to hold, which is why it is the first defect A5 fixes.
    """
    return await load_attention(
        session,
        tenant_id=user.tenant_id,
        site_id=site_id,
        scope=scope,
        band=band,
        limit=limit,
    )
