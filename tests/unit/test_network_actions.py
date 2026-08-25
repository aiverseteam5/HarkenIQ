"""R6-P6 unit tests: network action safety chain.

Named tests (review 8A + outside-voice T2/T6/T7 + decision 3A/7A): every
refusal branch is first-class — self-preservation incl. the fail-closed
resolution branch, redundant-path unverifiable, T1 quorum, LAG blast
radius, denial finality (D16), enable-without-pre-state.
"""

import pytest

from harkeniq.actions.executor import ActionExecutor
from harkeniq.actions.queue import ActionQueue
from harkeniq.autonomy.network_safety import (
    ManagementPathResolver,
    NetworkActionTracker,
)
from harkeniq.autonomy.preconditions import (
    ACTION_RISK,
    check_preconditions,
)
from harkeniq.mock.switch_sim import SwitchSimulator
from harkeniq.models import ActionStatus, ActionType, VerdictSeverity
from harkeniq.protocols.gnmi import GNMIProtocol


def device_state(**overrides):
    data = {
        "target_interface": "Ethernet0",
        "interfaces": {
            "Ethernet0": {"oper_state": "Up", "lag_name": "PortChannel1"},
            "Ethernet4": {"oper_state": "Up", "lag_name": "PortChannel1"},
            "Ethernet8": {"oper_state": "Up", "lag_name": None},
        },
        "diagnosis_confidence": 0.9,
        "diagnosis_classification": "hardware_degradation",
    }
    data.update(overrides)
    return data


def agent_state(**overrides):
    data = {
        "mgmt_interfaces": {"eth0"},  # mgmt path far from the data ports
        "corroborating_peers": 2,
        "network_tracker": NetworkActionTracker(),
    }
    data.update(overrides)
    return data


class TestSelfPreservation:
    def test_target_on_management_path_refused(self):
        result = check_preconditions(
            ActionType.INTERFACE_DISABLE,
            device_state(),
            agent_state(mgmt_interfaces={"Ethernet0"}),
        )
        assert not result.passed
        assert any("self-preservation" in f for f in result.failed_checks)

    def test_target_lag_on_management_path_refused(self):
        # Management rides PortChannel1: disabling a MEMBER is refused.
        result = check_preconditions(
            ActionType.INTERFACE_DISABLE,
            device_state(),
            agent_state(mgmt_interfaces={"PortChannel1"}),
        )
        assert not result.passed
        assert any("self-preservation" in f for f in result.failed_checks)

    def test_resolution_failure_fails_closed(self):
        # mgmt_interfaces ABSENT = resolver returned None = refuse.
        state = agent_state()
        del state["mgmt_interfaces"]
        result = check_preconditions(
            ActionType.INTERFACE_DISABLE, device_state(), state
        )
        assert not result.passed
        assert any("fail-closed" in f for f in result.failed_checks)


class TestManagementPathResolver:
    def test_longest_prefix_and_lag_expansion(self):
        routes = [
            ("0.0.0.0", "0.0.0.0", "Ethernet8"),          # default
            ("10.1.0.0", "255.255.0.0", "PortChannel1"),  # SM subnet
        ]
        resolver = ManagementPathResolver(
            "sm.example", {"PortChannel1": ["Ethernet0", "Ethernet4"]},
            route_reader=lambda: routes,
            resolve_host=lambda h: "10.1.2.3",
        )
        assert resolver.management_interfaces() == {
            "PortChannel1", "Ethernet0", "Ethernet4"
        }

    def test_member_egress_implicates_lag_and_siblings(self):
        resolver = ManagementPathResolver(
            "sm.example", {"PortChannel1": ["Ethernet0", "Ethernet4"]},
            route_reader=lambda: [("0.0.0.0", "0.0.0.0", "Ethernet0")],
            resolve_host=lambda h: "10.1.2.3",
        )
        assert resolver.management_interfaces() == {
            "PortChannel1", "Ethernet0", "Ethernet4"
        }

    def test_any_failure_returns_none_never_guesses(self):
        def broken_reader():
            raise OSError("no /proc/net/route")

        resolver = ManagementPathResolver(
            "sm.example", route_reader=broken_reader,
            resolve_host=lambda h: "10.1.2.3",
        )
        assert resolver.management_interfaces() is None
        resolver2 = ManagementPathResolver(
            "sm.example", route_reader=lambda: [],
            resolve_host=lambda h: "10.1.2.3",
        )
        assert resolver2.management_interfaces() is None


class TestRedundantPath:
    def test_lag_sibling_up_passes_locally(self):
        result = check_preconditions(
            ActionType.INTERFACE_DISABLE, device_state(), agent_state()
        )
        assert result.passed, result.reason

    def test_lag_sibling_down_refused(self):
        state = device_state()
        state["interfaces"]["Ethernet4"]["oper_state"] = "Down"
        result = check_preconditions(
            ActionType.INTERFACE_DISABLE, state, agent_state()
        )
        assert not result.passed
        assert any("redundant path" in f for f in result.failed_checks)

    def test_non_lag_port_needs_sm_verification(self):
        state = device_state(target_interface="Ethernet8")
        result = check_preconditions(
            ActionType.INTERFACE_DISABLE, state, agent_state()
        )
        assert not result.passed
        result_verified = check_preconditions(
            ActionType.INTERFACE_DISABLE, state,
            agent_state(sm_redundancy_verified=True),
        )
        assert result_verified.passed, result_verified.reason


class TestQuorumGate:
    def test_single_peer_is_propose_only(self):
        result = check_preconditions(
            ActionType.INTERFACE_RESET, device_state(),
            agent_state(corroborating_peers=1),
        )
        assert not result.passed
        assert any("propose-only" in f for f in result.failed_checks)


class TestDiagnosisGates:
    def test_low_confidence_refused(self):
        result = check_preconditions(
            ActionType.INTERFACE_DISABLE,
            device_state(diagnosis_confidence=0.5), agent_state(),
        )
        assert not result.passed

    def test_congestion_never_disabled_away(self):
        result = check_preconditions(
            ActionType.INTERFACE_DISABLE,
            device_state(diagnosis_classification="load_correlated"),
            agent_state(),
        )
        assert not result.passed
        assert any("R-M5" in f for f in result.failed_checks)

    def test_reset_has_no_diagnosis_classification_gate(self):
        result = check_preconditions(
            ActionType.INTERFACE_RESET,
            device_state(diagnosis_confidence=0.0,
                         diagnosis_classification=""),
            agent_state(),
        )
        assert result.passed, result.reason


class TestBlastRadius:
    def test_never_two_ports_of_one_lag(self):
        tracker = NetworkActionTracker()
        tracker.record("Ethernet0", "PortChannel1", now=1000.0)
        allowed, reason = tracker.allows(
            "Ethernet4", "PortChannel1", now=1100.0
        )
        assert not allowed and "never two ports of one LAG" in reason

    def test_switch_domain_limit_one_per_window(self):
        tracker = NetworkActionTracker()
        tracker.record("Ethernet0", None, now=1000.0)
        allowed, reason = tracker.allows("Ethernet8", None, now=1100.0)
        assert not allowed and "switch domain" in reason

    def test_window_expiry_allows_again(self):
        tracker = NetworkActionTracker(window_s=1800.0)
        tracker.record("Ethernet0", "PortChannel1", now=1000.0)
        allowed, _ = tracker.allows("Ethernet4", "PortChannel1", now=3000.0)
        assert allowed

    def test_precondition_consults_tracker(self):
        tracker = NetworkActionTracker()
        tracker.record("Ethernet4", "PortChannel1")
        result = check_preconditions(
            ActionType.INTERFACE_DISABLE, device_state(),
            agent_state(network_tracker=tracker),
        )
        assert not result.passed
        assert any("blast radius" in f for f in result.failed_checks)


class TestEnableRestoreSemantics:
    def test_enable_with_prestate_passes(self):
        result = check_preconditions(
            ActionType.INTERFACE_ENABLE,
            device_state(prestate_exists=True), agent_state(),
        )
        assert result.passed

    def test_enable_without_prestate_classifies_high(self):
        result = check_preconditions(
            ActionType.INTERFACE_ENABLE, device_state(), agent_state()
        )
        assert not result.passed
        assert any("HIGH" in f for f in result.failed_checks)


class TestClearCounters:
    def test_requires_snapshot(self):
        result = check_preconditions(
            ActionType.CLEAR_COUNTERS, device_state(), agent_state()
        )
        assert not result.passed
        passed = check_preconditions(
            ActionType.CLEAR_COUNTERS,
            device_state(counters_snapshot_recorded=True), agent_state(),
        )
        assert passed.passed


class TestRiskClassification:
    def test_network_action_risk_levels(self):
        assert ACTION_RISK[ActionType.INTERFACE_DISABLE] == "high"
        assert ACTION_RISK[ActionType.INTERFACE_RESET] == "high"
        assert ACTION_RISK[ActionType.INTERFACE_ENABLE] == "low"
        assert ACTION_RISK[ActionType.CLEAR_COUNTERS] == "low"


class TestDenialFinality:
    def test_denied_network_action_is_final_d16(self):
        queue = ActionQueue()
        action = queue.enqueue(
            ActionType.INTERFACE_DISABLE, "interface:Ethernet0",
            "interface-health", VerdictSeverity.CRITICAL,
            {"interface": "Ethernet0"},
        )
        queue.deny(action.id)
        assert action.status == ActionStatus.DENIED
        # Re-proposing the same fault does NOT create a new action: DENIED
        # blocks (denial is final, never silently re-queued).
        again = queue.enqueue(
            ActionType.INTERFACE_DISABLE, "interface:Ethernet0",
            "interface-health", VerdictSeverity.CRITICAL,
            {"interface": "Ethernet0"},
        )
        assert again is None


class TestExecutorIntegration:
    @pytest.mark.asyncio
    async def test_disable_through_executor_and_protocol(self):
        sim = SwitchSimulator(
            num_ports=2, translib_write=True, client_auth="none"
        )
        await sim.start()
        try:
            proto = GNMIProtocol(
                host="127.0.0.1", port=sim.port, plaintext=True
            )
            await proto.connect({})
            executor = ActionExecutor(
                None, "sonic",
                {"actions": {"allow_list": ["INTERFACE_DISABLE"]}},
                protocol=proto,
            )
            queue = ActionQueue()
            action = queue.enqueue(
                ActionType.INTERFACE_DISABLE, "interface:Ethernet0",
                "interface-health", VerdictSeverity.CRITICAL,
                {"interface": "Ethernet0"},
            )
            queue.approve(action.id)
            outcome = await executor.execute(action)
            assert outcome.success is True
            assert sim.state.ports["Ethernet0"].admin_status == "down"
            await proto.disconnect()
        finally:
            await sim.stop()

    @pytest.mark.asyncio
    async def test_not_on_allow_list_refused(self):
        executor = ActionExecutor(
            None, "sonic", {"actions": {"allow_list": ["IDENTIFY_LED"]}},
            protocol=GNMIProtocol(host="x", plaintext=True),
        )
        queue = ActionQueue()
        action = queue.enqueue(
            ActionType.INTERFACE_DISABLE, "interface:Ethernet0",
            "interface-health", VerdictSeverity.CRITICAL, {},
        )
        queue.approve(action.id)
        outcome = await executor.execute(action)
        assert outcome.success is False
        assert "not in allow list" in outcome.error_message
