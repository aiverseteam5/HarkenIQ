"""Action approval queue (Doc 06 §11A.2, decision D16).

All R1 actions require operator approval. Proposed actions enter the
queue as PENDING; the operator approves or denies them (denied is
final). Duplicate proposals for the same (sensor_id, action type) are
suppressed while an earlier one is still open.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from harkeniq.errors import ActionError
from harkeniq.models import Action, ActionStatus, ActionType, VerdictSeverity

#: Statuses that block a duplicate proposal for the same (sensor_id, type).
#: DENIED blocks too: denial is final (D16), so a persisting fault must not
#: re-propose an action the operator already refused.
_BLOCKING_STATUSES = (
    ActionStatus.PENDING,
    ActionStatus.APPROVED,
    ActionStatus.EXECUTING,
    ActionStatus.DENIED,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ActionQueue:
    """FIFO queue of proposed remediation actions."""

    def __init__(self) -> None:
        self._actions: dict[str, Action] = {}  # insertion-ordered

    def enqueue(
        self,
        action_type: ActionType,
        sensor_id: str,
        skill_name: str,
        verdict_severity: VerdictSeverity,
        params: Optional[dict[str, str]] = None,
    ) -> Optional[Action]:
        """Propose an action. Returns None if an equivalent action is open."""
        for existing in self._actions.values():
            if (
                existing.sensor_id == sensor_id
                and existing.type == action_type
                and existing.status in _BLOCKING_STATUSES
            ):
                return None
        action = Action(
            id=f"act-{uuid.uuid4().hex[:8]}",
            type=action_type,
            params=dict(params or {}),
            status=ActionStatus.PENDING,
            sensor_id=sensor_id,
            skill_name=skill_name,
            verdict_severity=verdict_severity,
            proposed_at=_utc_now(),
        )
        self._actions[action.id] = action
        return action

    def approve(self, action_id: str) -> Action:
        """Approve a PENDING action (PENDING -> APPROVED)."""
        action = self._require(action_id)
        if action.status != ActionStatus.PENDING:
            raise ActionError(
                f"Action {action_id} is {action.status.value}, only PENDING can be approved"
            )
        action.status = ActionStatus.APPROVED
        action.approved_at = _utc_now()
        return action

    def deny(self, action_id: str) -> Action:
        """Deny a PENDING action (PENDING -> DENIED; final)."""
        action = self._require(action_id)
        if action.status != ActionStatus.PENDING:
            raise ActionError(
                f"Action {action_id} is {action.status.value}, only PENDING can be denied"
            )
        action.status = ActionStatus.DENIED
        action.completed_at = _utc_now()
        return action

    def pending(self) -> list[Action]:
        return [a for a in self._actions.values() if a.status == ActionStatus.PENDING]

    def approved(self) -> list[Action]:
        return [a for a in self._actions.values() if a.status == ActionStatus.APPROVED]

    def get(self, action_id: str) -> Optional[Action]:
        return self._actions.get(action_id)

    def all(self) -> list[Action]:
        return list(self._actions.values())

    def restore(self, actions: list[Action]) -> None:
        """Restore actions from a checkpoint (oldest first)."""
        for action in actions:
            self._actions[action.id] = action

    def _require(self, action_id: str) -> Action:
        action = self._actions.get(action_id)
        if action is None:
            raise ActionError(f"Unknown action id: {action_id}")
        return action
