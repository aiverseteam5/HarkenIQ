"""State machine tests (Doc 06 §11, Doc 10 §2.15, Doc 12 §2.2)."""

import pytest

from harkeniq.models import AgentState
from harkeniq.state.machine import StateMachine


def walk(sm, *states):
    for s in states:
        sm.transition(s, f"test -> {s.value}")


class TestValidPaths:
    def test_initial_state_is_booting(self):
        assert StateMachine().current_state == AgentState.BOOTING

    def test_happy_observe_cycle(self):
        sm = StateMachine()
        walk(
            sm,
            AgentState.OBSERVING,
            AgentState.EVALUATING,
            AgentState.DECIDING,
            AgentState.OBSERVING,
        )
        assert sm.current_state == AgentState.OBSERVING

    def test_action_path(self):
        sm = StateMachine()
        walk(
            sm,
            AgentState.OBSERVING,
            AgentState.EVALUATING,
            AgentState.DECIDING,
            AgentState.AWAITING_AUTH,
            AgentState.ACTING,
            AgentState.REPORTING,
            AgentState.OBSERVING,
        )
        assert sm.current_state == AgentState.OBSERVING

    def test_auth_denied_path(self):
        sm = StateMachine()
        walk(
            sm,
            AgentState.OBSERVING,
            AgentState.EVALUATING,
            AgentState.DECIDING,
            AgentState.AWAITING_AUTH,
            AgentState.REPORTING,
            AgentState.OBSERVING,
        )
        assert sm.current_state == AgentState.OBSERVING


class TestInvalidTransitions:
    @pytest.mark.parametrize(
        "path,bad",
        [
            ([], AgentState.ACTING),  # BOOTING -> ACTING
            ([], AgentState.EVALUATING),  # BOOTING -> EVALUATING
            ([], AgentState.BOOTING),  # self-loop
            ([AgentState.OBSERVING], AgentState.REPORTING),
            ([AgentState.OBSERVING], AgentState.OBSERVING),
            ([AgentState.OBSERVING, AgentState.EVALUATING], AgentState.OBSERVING),
            (
                [AgentState.OBSERVING, AgentState.EVALUATING, AgentState.DECIDING],
                AgentState.ACTING,  # must go through AWAITING_AUTH
            ),
            (
                [
                    AgentState.OBSERVING,
                    AgentState.EVALUATING,
                    AgentState.DECIDING,
                    AgentState.AWAITING_AUTH,
                    AgentState.ACTING,
                ],
                AgentState.OBSERVING,  # ACTING must report first
            ),
        ],
    )
    def test_invalid_raises_value_error(self, path, bad):
        sm = StateMachine()
        walk(sm, *path)
        before = sm.current_state
        with pytest.raises(ValueError, match="Invalid state transition"):
            sm.transition(bad, "should fail")
        assert sm.current_state == before  # state unchanged on failure

    def test_can_transition(self):
        sm = StateMachine()
        assert sm.can_transition(AgentState.OBSERVING)
        assert not sm.can_transition(AgentState.ACTING)
        walk(sm, AgentState.OBSERVING, AgentState.EVALUATING, AgentState.DECIDING)
        assert sm.can_transition(AgentState.OBSERVING)
        assert sm.can_transition(AgentState.AWAITING_AUTH)
        assert not sm.can_transition(AgentState.REPORTING)


class TestHistory:
    def test_history_records_reason_and_timestamp(self):
        sm = StateMachine()
        sm.transition(AgentState.OBSERVING, "boot complete")
        sm.transition(AgentState.EVALUATING, "poll cycle done")
        history = sm.get_history()
        assert len(history) == 2
        assert history[0].from_state == AgentState.BOOTING
        assert history[0].to_state == AgentState.OBSERVING
        assert history[0].reason == "boot complete"
        assert history[0].timestamp.endswith("Z")
        assert history[1].reason == "poll cycle done"

    def test_failed_transition_not_recorded(self):
        sm = StateMachine()
        with pytest.raises(ValueError):
            sm.transition(AgentState.ACTING, "nope")
        assert sm.get_history() == []

    def test_history_is_a_copy(self):
        sm = StateMachine()
        sm.transition(AgentState.OBSERVING, "boot")
        sm.get_history().clear()
        assert len(sm.get_history()) == 1
