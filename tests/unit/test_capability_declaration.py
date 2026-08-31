"""Capability Registry: the node's declaration must be TRUE.

The Registry is worth nothing if a protocol can claim reach it does not
have, or quietly gain reach it never declares. Every other layer --
Site Manager column, Central Command cache, /api/capabilities, the
Operational Agent's binding and proposal validation, the Console page --
is a reflection of these declarations, so this file is where the whole
chain is anchored to reality.

Two directions, both required:

  declared  => the protocol really dispatches it (no phantom capability)
  undeclared => the protocol really refuses it  (no hidden capability)

D1 is encoded here as a test rather than a comment: INTERFACE_RESET is a
fully governed action class with no implementation on any protocol this
platform ships, and the Registry must keep saying so until an
implementation slice changes that fact -- at which point this test is
what tells whoever writes it to update the declaration.
"""

from __future__ import annotations

import pytest

from harkeniq.actions.executor import EXECUTOR_DISPATCH_ACTIONS
from harkeniq.autonomy.preconditions import (
    ACTION_REVERSIBILITY,
    ACTION_RISK,
    INVERSE_ACTION,
    REV_IRREVERSIBLE,
    REV_NONE,
    REV_REVERSIBLE,
    REV_SELF_REVERTING,
)
from harkeniq.capabilities import (
    DECLARATION_VERSION,
    PROTOCOL_NAMES,
    action_facts,
    declare,
    effective_actions,
    platform_implementations,
    protocol_reach,
    protocol_reach_of,
)
from harkeniq.mock.ipmi_sim import MockIPMIBMC
from harkeniq.mock.switch_sim import SwitchSimulator
from harkeniq.models import ActionType
from harkeniq.protocols.device import create_device_protocol
from harkeniq.protocols.gnmi import GNMIProtocol
from harkeniq.protocols.ipmi import IPMIProtocol
from harkeniq.protocols.redfish import RedfishDeviceProtocol

ALL_ACTIONS = {a.value for a in ActionType}


class TestReversibilityDeclaration:
    """The one new declaration the Registry introduces."""

    def test_every_action_class_declares_reversibility(self):
        assert set(ACTION_REVERSIBILITY) == set(ActionType)

    def test_reversibility_values_are_from_the_vocabulary(self):
        allowed = {REV_NONE, REV_SELF_REVERTING, REV_REVERSIBLE, REV_IRREVERSIBLE}
        assert set(ACTION_REVERSIBILITY.values()) <= allowed

    def test_reversible_classes_name_an_inverse_and_others_do_not(self):
        for action, rev in ACTION_REVERSIBILITY.items():
            if rev == REV_REVERSIBLE:
                assert action in INVERSE_ACTION, (
                    f"{action.value} is declared reversible but names no "
                    f"action class that reverses it"
                )
            else:
                assert action not in INVERSE_ACTION

    def test_every_inverse_is_itself_a_governed_action_class(self):
        for action, inverse in INVERSE_ACTION.items():
            assert isinstance(inverse, ActionType)
            assert inverse in ACTION_RISK

    def test_reversibility_is_a_separate_axis_from_risk(self):
        """Not a style point: if the two axes agreed, one would be
        redundant and the Registry would be reporting risk twice."""
        # A low-risk action that can never be undone.
        assert ACTION_RISK[ActionType.SEL_CLEAR] == "low"
        assert ACTION_REVERSIBILITY[ActionType.SEL_CLEAR] == REV_IRREVERSIBLE
        # A medium-risk action the device recovers from by itself.
        assert ACTION_RISK[ActionType.POWER_CYCLE] == "medium"
        assert ACTION_REVERSIBILITY[ActionType.POWER_CYCLE] == REV_SELF_REVERTING
        # A high-risk action with a governed inverse.
        assert ACTION_RISK[ActionType.FIRMWARE_UPDATE] == "high"
        assert ACTION_REVERSIBILITY[ActionType.FIRMWARE_UPDATE] == REV_REVERSIBLE


class TestDeclarationsExist:
    def test_every_shipped_protocol_declares_its_reach(self):
        for name in PROTOCOL_NAMES:
            reach = protocol_reach(name)
            assert reach is not None, f"{name} declares no reach"

    def test_factory_and_registry_know_the_same_protocols(self):
        """A protocol added to the factory and forgotten here would have
        UNKNOWN reach for every device using it."""
        for name in PROTOCOL_NAMES:
            proto = create_device_protocol(name, host="10.0.0.1")
            assert protocol_reach_of(proto) == protocol_reach(name)
        with pytest.raises(ValueError):
            create_device_protocol("netconf", host="10.0.0.1")

    def test_declared_actions_are_all_governed_action_classes(self):
        """A protocol cannot invent a capability outside the vocabulary."""
        for name in PROTOCOL_NAMES:
            assert protocol_reach(name) <= ALL_ACTIONS

    def test_unknown_protocol_reads_unknown_not_empty(self):
        assert protocol_reach("netconf") is None
        assert protocol_reach("") is None

    def test_instance_without_a_declaration_reads_unknown(self):
        class Undeclared:
            name = "mystery"

        assert protocol_reach_of(Undeclared()) is None


class TestRedfishDeclarationIsTrue:
    """Redfish executes through the ActionExecutor's own dispatch chain."""

    def test_declaration_is_the_dispatch_chain_not_a_copy(self):
        assert RedfishDeviceProtocol.supported_actions() is EXECUTOR_DISPATCH_ACTIONS

    def test_every_declared_action_has_a_dispatch_branch(self):
        import inspect

        from harkeniq.actions import executor as executor_module

        source = inspect.getsource(executor_module.ActionExecutor.execute)
        for action in EXECUTOR_DISPATCH_ACTIONS:
            assert f"ActionType.{action}" in source, (
                f"redfish declares {action} but ActionExecutor.execute has "
                f"no branch for it"
            )

    def test_every_undeclared_action_has_no_dispatch_branch(self):
        import inspect

        from harkeniq.actions import executor as executor_module

        source = inspect.getsource(executor_module.ActionExecutor.execute)
        for action in ALL_ACTIONS - set(EXECUTOR_DISPATCH_ACTIONS):
            assert f"ActionType.{action}" not in source, (
                f"ActionExecutor.execute dispatches {action} but redfish "
                f"does not declare it -- a hidden capability"
            )


class TestIPMIDeclarationIsTrue:
    @pytest.mark.asyncio
    async def test_declared_actions_really_execute(self):
        bmc = MockIPMIBMC(device="dell-r750")
        proto = IPMIProtocol(host="10.0.0.1", backend_factory=bmc.factory())
        await proto.connect({"username": "admin", "password": "password"})
        try:
            for action in sorted(IPMIProtocol.supported_actions()):
                result = await proto.execute_action(action, {})
                assert result["success"] is True, (
                    f"ipmi declares {action} but executing it failed: "
                    f"{result.get('error')}"
                )
        finally:
            await proto.disconnect()

    @pytest.mark.asyncio
    async def test_undeclared_actions_are_refused_as_unsupported(self):
        bmc = MockIPMIBMC(device="dell-r750")
        proto = IPMIProtocol(host="10.0.0.1", backend_factory=bmc.factory())
        await proto.connect({"username": "admin", "password": "password"})
        try:
            for action in sorted(ALL_ACTIONS - IPMIProtocol.supported_actions()):
                result = await proto.execute_action(action, {})
                assert result["success"] is False, (
                    f"ipmi executed {action} without declaring it"
                )
                assert "not supported" in result.get("error", "")
        finally:
            await proto.disconnect()


class TestGNMIDeclarationIsTrue:
    @pytest.mark.asyncio
    async def test_declared_actions_really_execute(self):
        sim = SwitchSimulator(num_ports=2, translib_write=True, client_auth="none")
        await sim.start()
        try:
            proto = GNMIProtocol(host="127.0.0.1", port=sim.port, plaintext=True)
            await proto.connect({})
            params = {"interface": "Ethernet0"}
            for action in sorted(GNMIProtocol.supported_actions()):
                result = await proto.execute_action(action, dict(params))
                assert result["success"] is True, (
                    f"gnmi declares {action} but executing it failed: "
                    f"{result.get('error')}"
                )
            await proto.disconnect()
        finally:
            await sim.stop()

    @pytest.mark.asyncio
    async def test_undeclared_actions_are_refused_as_unsupported(self):
        sim = SwitchSimulator(num_ports=2, translib_write=True, client_auth="none")
        await sim.start()
        try:
            proto = GNMIProtocol(host="127.0.0.1", port=sim.port, plaintext=True)
            await proto.connect({})
            for action in sorted(ALL_ACTIONS - GNMIProtocol.supported_actions()):
                result = await proto.execute_action(action, {"interface": "Ethernet0"})
                assert result["success"] is False, (
                    f"gnmi executed {action} without declaring it"
                )
                # The refusal must SAY why. CLEAR_COUNTERS refuses with
                # its own transport-specific reason rather than the
                # generic fall-through, and both are honest refusals --
                # what the Registry needs is that neither succeeds.
                assert result.get("error"), (
                    f"gnmi refused {action} without saying why"
                )
            await proto.disconnect()
        finally:
            await sim.stop()


class TestD1InterfaceReset:
    """D1: governed vocabulary, zero executor reach, and the Registry
    must say so. Deleting the action class to make the Registry pass is
    explicitly refused -- this is a capability truth problem, not a
    reason to drop governed semantics."""

    def test_interface_reset_remains_a_governed_action_class(self):
        assert ActionType.INTERFACE_RESET in ACTION_RISK
        assert ACTION_RISK[ActionType.INTERFACE_RESET] == "high"
        assert ActionType.INTERFACE_RESET in ACTION_REVERSIBILITY

    def test_interface_reset_keeps_its_preconditions(self):
        from harkeniq.autonomy.preconditions import _PRECONDITION_MAP

        assert ActionType.INTERFACE_RESET in _PRECONDITION_MAP

    def test_no_protocol_implements_interface_reset(self):
        for name in PROTOCOL_NAMES:
            assert "INTERFACE_RESET" not in protocol_reach(name), (
                f"{name} now implements INTERFACE_RESET -- if that is real, "
                f"update this test WITH the safety, validation and live "
                f"proof the D1 follow-up slice requires"
            )

    def test_registry_reports_it_unimplemented(self):
        facts = action_facts()
        assert facts["INTERFACE_RESET"]["implemented"] is False
        assert facts["INTERFACE_RESET"]["implemented_by"] == []

    def test_the_unimplemented_set_is_pinned(self):
        """Pinned, so a class joining or leaving this set is a deliberate
        change with a slice behind it rather than a silent drift.

        CLEAR_COUNTERS was FOUND by this test during the Registry slice:
        R6 correctly refused to fake a counter clear that SONiC exposes
        only over CLI, but nothing upstream knew, so the Operational
        Agent's interface condition mapped to a class no executor can
        run. Same shape as D1, same treatment: governed semantics kept,
        implementation deferred to its own slice, Registry tells the
        truth in the meantime."""
        unimplemented = sorted(
            k for k, v in action_facts().items() if not v["implemented"]
        )
        assert unimplemented == ["CLEAR_COUNTERS", "INTERFACE_RESET"]

    def test_clear_counters_keeps_its_governed_semantics(self):
        from harkeniq.autonomy.preconditions import _PRECONDITION_MAP

        assert ACTION_RISK[ActionType.CLEAR_COUNTERS] == "low"
        assert ActionType.CLEAR_COUNTERS in _PRECONDITION_MAP
        assert ActionType.CLEAR_COUNTERS in ACTION_REVERSIBILITY

    def test_a_node_permitting_it_still_cannot_do_it(self):
        """The allow list is policy; it cannot conjure an implementation."""
        d = declare("gnmi", ["INTERFACE_RESET", "INTERFACE_DISABLE"])
        assert "INTERFACE_RESET" in d["allow_list"]
        assert "INTERFACE_RESET" not in d["implemented"]
        assert d["effective"] == ["INTERFACE_DISABLE"]


class TestPlatformFacts:
    def test_every_action_class_appears_even_when_unimplemented(self):
        assert set(platform_implementations()) == ALL_ACTIONS
        assert set(action_facts()) == ALL_ACTIONS

    def test_facts_restate_nothing(self):
        facts = action_facts()
        for action in ActionType:
            row = facts[action.value]
            assert row["risk"] == ACTION_RISK[action]
            assert row["reversibility"] == ACTION_REVERSIBILITY[action]
            inverse = INVERSE_ACTION.get(action)
            assert row["inverse_action"] == (inverse.value if inverse else None)


class TestNodeDeclaration:
    def test_effective_is_reach_intersect_policy(self):
        d = declare("redfish", ["IDENTIFY_LED", "SEL_CLEAR", "INTERFACE_DISABLE"])
        # INTERFACE_DISABLE is permitted but redfish cannot do it.
        assert d["effective"] == ["IDENTIFY_LED", "SEL_CLEAR"]
        assert effective_actions(d) == frozenset({"IDENTIFY_LED", "SEL_CLEAR"})

    def test_three_sets_are_reported_separately(self):
        """'no code' and 'not permitted here' are different problems and
        must stay distinguishable at the Registry."""
        d = declare("ipmi", ["IDENTIFY_LED"])
        assert d["implemented"] == ["IDENTIFY_LED", "SEL_CLEAR"]
        assert d["allow_list"] == ["IDENTIFY_LED"]
        assert d["effective"] == ["IDENTIFY_LED"]

    def test_empty_allow_list_yields_no_effective_reach(self):
        d = declare("redfish", [])
        assert d["effective"] == []
        assert effective_actions(d) == frozenset()

    def test_unknown_protocol_declares_unknown_never_empty(self):
        d = declare("netconf", ["IDENTIFY_LED"])
        assert d["reach_known"] is False
        assert d["implemented"] is None
        assert d["effective"] is None
        assert effective_actions(d) is None

    def test_unknown_is_not_the_empty_set(self):
        """The distinction that keeps a pre-upgrade fleet working."""
        assert effective_actions(None) is None
        assert effective_actions({}) is None
        assert effective_actions({"reach_known": False}) is None
        assert effective_actions(declare("redfish", [])) == frozenset()

    def test_declaration_carries_a_version(self):
        assert declare("redfish", [])["version"] == DECLARATION_VERSION


class TestAgentDeclaresItself:
    """The node is the authoritative source, so the agent must actually
    build the declaration -- a source nobody calls is the house bug this
    codebase has already found eight times."""

    def _agent(self, tmp_path, **overrides):
        from harkeniq.agent import Agent

        config = {
            "agent": {"id": "agent-cap-test", "name": "node-1"},
            "bmc": {"host": "https://127.0.0.1:1", "username": "u",
                    "password": "p", "verify_ssl": False},
            "skills": {"directory": str(tmp_path / "skills")},
            "checkpoint": {"path": str(tmp_path / "cp.db")},
            "actions": {"enabled": True, "approval_mode": "queue",
                        "allow_list": ["IDENTIFY_LED", "SEL_CLEAR"]},
        }
        for key, value in overrides.items():
            config.setdefault(key, {}).update(value)
        return Agent(config)

    def test_the_agent_declares_reach_and_policy_separately(self, tmp_path):
        declaration = self._agent(tmp_path).declare_capabilities()
        assert declaration["protocol"] == "redfish"
        assert declaration["allow_list"] == ["IDENTIFY_LED", "SEL_CLEAR"]
        # Reach is wider than policy, and both are reported.
        assert "POWER_CYCLE" in declaration["implemented"]
        assert "POWER_CYCLE" not in declaration["effective"]
        assert declaration["effective"] == ["IDENTIFY_LED", "SEL_CLEAR"]

    def test_a_switch_agent_declares_the_gnmi_reach(self, tmp_path):
        agent = self._agent(
            tmp_path,
            bmc={"protocol": "gnmi", "host": "127.0.0.1", "port": 8080},
            actions={"allow_list": ["INTERFACE_DISABLE", "INTERFACE_RESET"]},
        )
        declaration = agent.declare_capabilities()
        assert declaration["protocol"] == "gnmi"
        # D1 on the node itself: permitted, not implemented, so not
        # effective. The node's allow list cannot conjure an executor.
        assert "INTERFACE_RESET" in declaration["allow_list"]
        assert "INTERFACE_RESET" not in declaration["implemented"]
        assert declaration["effective"] == ["INTERFACE_DISABLE"]

    def test_an_agent_with_no_allow_list_declares_the_default(self, tmp_path):
        from harkeniq.actions.executor import DEFAULT_ALLOW_LIST

        agent = self._agent(tmp_path)
        agent.config["actions"].pop("allow_list")
        declaration = agent.declare_capabilities()
        assert declaration["allow_list"] == sorted(DEFAULT_ALLOW_LIST)

    def test_the_declaration_is_sent_at_registration(self, tmp_path):
        """The reporter must actually carry it. A declaration built and
        dropped on the floor is worse than none: every consumer would
        read the fleet as undeclared and never know why."""
        import inspect

        from harkeniq import agent as agent_module
        from harkeniq.reporting import grpc_stub

        source = inspect.getsource(agent_module.Agent._register_with_sm)
        assert "capabilities=self.declare_capabilities()" in source
        register = inspect.getsource(grpc_stub.SiteManagerReporter.register_agent)
        assert "capabilities_json=" in register
