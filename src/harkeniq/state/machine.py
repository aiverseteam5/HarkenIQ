"""Agent state machine (Doc 06 §11, Doc 10 §2.15).

Explicit 7-state machine with a validated transition table. Invalid
transitions raise ValueError; every transition is recorded with a reason
and timestamp.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from harkeniq.models import AgentState, StateTransition

logger = logging.getLogger("harkeniq.state.machine")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class StateMachine:
    """Agent state machine with validated transitions (Doc 06 §11.1)."""

    TRANSITIONS: dict[AgentState, set[AgentState]] = {
        AgentState.BOOTING: {AgentState.OBSERVING},
        AgentState.OBSERVING: {AgentState.EVALUATING},
        AgentState.EVALUATING: {AgentState.DECIDING},
        AgentState.DECIDING: {AgentState.OBSERVING, AgentState.AWAITING_AUTH},
        AgentState.AWAITING_AUTH: {AgentState.ACTING, AgentState.REPORTING},
        AgentState.ACTING: {AgentState.REPORTING},
        AgentState.REPORTING: {AgentState.OBSERVING},
    }

    def __init__(self) -> None:
        self._state = AgentState.BOOTING
        self._history: list[StateTransition] = []

    @property
    def current_state(self) -> AgentState:
        return self._state

    def can_transition(self, to_state: AgentState) -> bool:
        return to_state in self.TRANSITIONS.get(self._state, set())

    def transition(self, to_state: AgentState, reason: str) -> StateTransition:
        """Transition to a new state; raises ValueError if not allowed."""
        if not self.can_transition(to_state):
            raise ValueError(
                f"Invalid state transition {self._state.value} -> {to_state.value}"
            )
        record = StateTransition(
            from_state=self._state,
            to_state=to_state,
            reason=reason,
            timestamp=_utc_now(),
        )
        logger.info(
            "State transition %s -> %s: %s",
            self._state.value, to_state.value, reason,
        )
        self._state = to_state
        self._history.append(record)
        return record

    def get_history(self) -> list[StateTransition]:
        return list(self._history)
