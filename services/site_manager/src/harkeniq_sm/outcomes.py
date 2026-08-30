"""One writer for action outcomes, whichever path produced the action.

A1 (2026-08-30). Two things execute actions on a node: an action the
node proposed and a human approved (ReportAction), and a directive the
Site Manager delivered (firmware campaigns, marketplace installs, and
now an Operational Agent's approved proposal). Only the first ever wrote
`sm_action_outcomes`.

That gap was not cosmetic. It meant every directed execution since R5-1
was invisible to the error budget, to fleet learning, and to the S5
evidence an operator reads before raising an autonomy level: a firmware
action could fail on every device in a campaign and the tenant's
evidence would still say nothing had happened. Same shape as the
KnowledgeBase defect S5 found -- a declared mechanism with no writer on
one of its paths.

So the write lives here once, and both paths call it.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select

from harkeniq_sm.db.models import ActionOutcomeRow

logger = logging.getLogger("harkeniq.sm.outcomes")


async def record_action_outcome(
    session,
    *,
    device_id: str,
    action_id: str,
    action_type: str,
    result: str,
    fault_resolved: Optional[bool] = None,
    post_state: Optional[dict] = None,
    actor: str = "",
) -> bool:
    """Persist one terminal outcome and fold it into the error budget.

    Idempotent per (device, action_id): a retried terminal report must
    not count twice, or the error budget would demote on duplicates.

    Returns True when this outcome newly dropped the class back.
    """
    existing = (
        await session.execute(
            select(ActionOutcomeRow).where(
                ActionOutcomeRow.device_id == device_id,
                ActionOutcomeRow.action_id == action_id,
            )
        )
    ).scalars().first()
    if existing is not None:
        return False

    session.add(ActionOutcomeRow(
        action_id=action_id,
        action_type=action_type,
        device_id=device_id,
        outcome=result,
        fault_resolved=fault_resolved,
        post_state=post_state if isinstance(post_state, dict) else None,
        actor=actor,
    ))

    # S5: fold the outcome into the A2.2 error budget. Demotion is
    # automatic and needs no human; only a human ever promotes.
    from harkeniq_sm.db.repos import ErrorBudgetRepo

    _, newly_dropped = await ErrorBudgetRepo(session).record(action_type, result)
    if newly_dropped:
        logger.warning(
            "Error budget drop-back for %s: autonomy withdrawn until an "
            "operator reviews the failures", action_type,
        )
    return newly_dropped
