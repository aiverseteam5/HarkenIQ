"""A2 (D3): an approved proposal is not a guaranteed execution.

    Approved proposal version  !=  guaranteed execution.

It means "this proposal was authorized against THIS agent configuration
version". Execution still requires the current hard gates, every time.

Two halves, and both matter:

  A V3 proposal keeps its V3 meaning and V3 attribution. It is never
  silently reinterpreted as V4, because an outcome that renamed its own
  cause would lie about what produced it.

  A V3 proposal is never silently executed just because somebody once
  approved it. A revoked scope, a retired agent, an active stop switch
  or a paused agent still refuses it — and the Site Manager's lease,
  preconditions and blast radius, and the node's own allow list, refuse
  it afterwards and independently.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from harkeniq_cc.agent_activation import (
    DISPATCH_GATES,
    dispatch_permitted,
    proposal_version_is_honoured,
)


def _all_clear(**over):
    gates = {name: True for name in DISPATCH_GATES}
    gates.update(over)
    return gates


class TestTheGateAlgebra:
    def test_every_gate_clear_permits(self):
        assert dispatch_permitted(**_all_clear())[0] is True

    @pytest.mark.parametrize("missing", DISPATCH_GATES)
    def test_an_unevaluated_gate_refuses(self, missing):
        """Fail-closed, the same default `execution_permitted` uses at
        the Site Manager: an input nobody supplied must never read as
        consent."""
        gates = _all_clear()
        gates.pop(missing)
        allowed, reason = dispatch_permitted(**gates)
        assert allowed is False
        assert missing in reason
        assert "refusal, not a pass" in reason

    @pytest.mark.parametrize("gate", DISPATCH_GATES)
    def test_any_single_gate_can_refuse(self, gate):
        allowed, reason = dispatch_permitted(**_all_clear(**{gate: "no"}))
        assert allowed is False
        assert reason == "no"

    def test_a_false_verdict_refuses_and_names_the_gate(self):
        allowed, reason = dispatch_permitted(**_all_clear(stop_switch=False))
        assert allowed is False
        assert "stop_switch" in reason

    def test_none_means_this_gate_does_not_object(self):
        assert dispatch_permitted(**_all_clear(budget=None))[0] is True

    def test_the_gates_are_central_command_only(self):
        """The lease, preconditions, blast radius and the node's allow
        list are NOT here — they run afterwards and independently, and
        this must never look like a substitute for them."""
        assert "lease" not in DISPATCH_GATES
        assert "preconditions" not in DISPATCH_GATES
        assert "blast_radius" not in DISPATCH_GATES
        assert "allow_list" not in DISPATCH_GATES


class TestVersionIsHonouredNotRewritten:
    def _proposal(self, actor):
        return SimpleNamespace(actor=actor, id="p1")

    def _agent(self, agent_id="ag1", version=4):
        return SimpleNamespace(id=agent_id, version=version, status="active")

    def test_a_v3_proposal_under_a_v4_agent_is_still_honoured(self):
        """Superseded is not invalid: the decision was coherent when it
        was made, and the hard gates below still apply."""
        ok, why = proposal_version_is_honoured(
            self._proposal("op-agent:ag1@v3"), self._agent(version=4)
        )
        assert ok is True
        assert "version 3" in why, "the proposal must keep naming v3"

    def test_it_is_never_reinterpreted_as_the_current_version(self):
        _, why = proposal_version_is_honoured(
            self._proposal("op-agent:ag1@v3"), self._agent(version=4)
        )
        assert "version 4" not in why

    def test_a_proposal_from_another_agent_is_refused(self):
        ok, why = proposal_version_is_honoured(
            self._proposal("op-agent:other@v1"), self._agent()
        )
        assert ok is False
        assert "different agent" in why

    def test_an_unattributed_proposal_is_refused(self):
        ok, why = proposal_version_is_honoured(self._proposal(""), self._agent())
        assert ok is False
        assert "no agent attribution" in why


class TestTheRatifiedRefusals:
    """Each condition the ratification named, as its own refusal."""

    def test_a_retired_agent_cannot_dispatch_an_old_approval(self):
        allowed, reason = dispatch_permitted(
            **_all_clear(agent_active="the agent is 'retired', not active")
        )
        assert allowed is False
        assert "retired" in reason

    def test_a_revoked_identity_refuses(self):
        allowed, reason = dispatch_permitted(
            **_all_clear(agent_identity="this proposal belongs to a different agent")
        )
        assert allowed is False

    def test_a_cross_tenant_proposal_refuses(self):
        allowed, reason = dispatch_permitted(
            **_all_clear(tenant_scope="the agent belongs to another tenant")
        )
        assert allowed is False
        assert "another tenant" in reason

    def test_an_active_stop_switch_refuses(self):
        allowed, reason = dispatch_permitted(
            **_all_clear(stop_switch="the tenant stop switch is active")
        )
        assert allowed is False
        assert "stop switch" in reason

    def test_a_paused_agent_refuses(self):
        allowed, reason = dispatch_permitted(
            **_all_clear(budget="the agent is paused: held by ops")
        )
        assert allowed is False
        assert "paused" in reason
