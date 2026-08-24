"""Config compliance tests (R4-2 P13).

Covers: policy parsing/loading, drift detection, remediation playbook
building, CONFIG_RESTORE execution via the real ActionExecutor against
the simulator, the finished (no-longer-stub) PlaybookExecutor wiring,
dry-run mode, and the agent compliance flow end-to-end (the "config
drift detected and corrected via playbook" exit criterion).
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from harkeniq.actions.executor import ActionExecutor
from harkeniq.actions.playbook_executor import PlaybookExecutor
from harkeniq.actions.queue import ActionQueue
from harkeniq.agent import Agent
from harkeniq.compliance.config_policy import (
    ConfigPolicy,
    ConfigPolicyError,
    build_remediation_playbook,
    detect_drift,
    load_config_policies,
    parse_policy,
)
from harkeniq.mock.simulator import MockSimulator
from harkeniq.models import ActionStatus, ActionType, PlaybookStatus, VerdictSeverity
from harkeniq.protocols.device import create_device_protocol
from harkeniq.redfish.client import RedfishClient

REPO = Path(__file__).parents[2]

POLICY_YAML = """\
policy_id: test-baseline
name: Test baseline
device_types: ["dell"]
severity: WARNING
expected:
  NTPConfigGroup.1.NTPEnable: Enabled
  SysLog.1.SysLogEnable: Enabled
"""


@pytest.fixture
async def dell_sim():
    sim = MockSimulator(device="dell-r750", port=0, no_auth=True)
    await sim.start()
    yield sim
    await sim.stop()


@pytest.fixture
async def dell_client(dell_sim):
    client = RedfishClient(host=dell_sim.url, verify_ssl=False, request_timeout=10)
    await client.connect("admin", "password")
    yield client
    await client.close()


def _policy(**overrides) -> ConfigPolicy:
    data = {
        "policy_id": "p1", "name": "P1", "device_types": ["dell"],
        "severity": "WARNING",
        "expected": {"NTPConfigGroup.1.NTPEnable": "Enabled"},
    }
    data.update(overrides)
    return parse_policy(data)


class TestPolicyParsing:
    def test_valid_policy(self, tmp_path):
        (tmp_path / "p.yaml").write_text(POLICY_YAML)
        policies = load_config_policies(tmp_path)
        assert set(policies) == {"test-baseline"}
        p = policies["test-baseline"]
        assert p.matches_device("dell")
        assert not p.matches_device("hpe")

    def test_wildcard_device(self):
        assert _policy(device_types=["*"]).matches_device("supermicro")

    def test_missing_required_field(self):
        with pytest.raises(ConfigPolicyError):
            parse_policy({"policy_id": "x", "name": "X"})

    def test_bad_severity(self):
        with pytest.raises(ConfigPolicyError):
            _policy(severity="FATAL")

    def test_unknown_keys_rejected(self):
        with pytest.raises(ConfigPolicyError):
            parse_policy({"policy_id": "x", "name": "X",
                          "expected": {"a": 1}, "bogus": True})

    def test_duplicate_policy_id_skipped(self, tmp_path):
        (tmp_path / "a.yaml").write_text(POLICY_YAML)
        (tmp_path / "b.yaml").write_text(POLICY_YAML)
        assert len(load_config_policies(tmp_path)) == 1

    def test_missing_directory_is_empty(self, tmp_path):
        assert load_config_policies(tmp_path / "nope") == {}


class TestDriftDetection:
    def test_compliant_no_findings(self):
        policy = _policy()
        assert detect_drift({"NTPConfigGroup.1.NTPEnable": "Enabled"}, policy) == []

    def test_drift_detected(self):
        policy = _policy()
        findings = detect_drift({"NTPConfigGroup.1.NTPEnable": "Disabled"}, policy)
        assert len(findings) == 1
        f = findings[0]
        assert f.status == "DRIFT"
        assert f.actual == "Disabled"
        assert f.expected == "Enabled"

    def test_missing_attribute_is_unknown_not_drift(self):
        policy = _policy()
        findings = detect_drift({}, policy)
        assert len(findings) == 1
        assert findings[0].status == "UNKNOWN"

    def test_remediation_playbook_built(self):
        policy = _policy()
        findings = detect_drift({"NTPConfigGroup.1.NTPEnable": "Disabled"}, policy)
        playbook = build_remediation_playbook(findings, policy)
        assert playbook is not None
        assert playbook.step_count == 1
        step = playbook.steps[0]
        assert step.action_type == ActionType.CONFIG_RESTORE
        attrs = json.loads(step.params["attributes_json"])
        assert attrs == {"NTPConfigGroup.1.NTPEnable": "Enabled"}
        assert step.verification_checks[0].operator == "equals"

    def test_no_playbook_for_unknown_only(self):
        policy = _policy()
        findings = detect_drift({}, policy)
        assert build_remediation_playbook(findings, policy) is None


class TestCollectConfig:
    async def test_redfish_dell_collects_attributes(self, dell_sim):
        proto = create_device_protocol("redfish", host=dell_sim.url)
        await proto.connect({"username": "admin", "password": "password"})
        try:
            config = await proto.collect_config()
            assert config["NTPConfigGroup.1.NTPEnable"] == "Enabled"
        finally:
            await proto.disconnect()

    async def test_drift_visible_in_snapshot(self, dell_sim):
        dell_sim.inject_config_drift("NTPConfigGroup.1.NTPEnable", "Disabled")
        proto = create_device_protocol("redfish", host=dell_sim.url)
        await proto.connect({"username": "admin", "password": "password"})
        try:
            config = await proto.collect_config()
            assert config["NTPConfigGroup.1.NTPEnable"] == "Disabled"
        finally:
            await proto.disconnect()

    async def test_ipmi_returns_empty(self):
        from harkeniq.mock.ipmi_sim import MockIPMIBMC
        from harkeniq.protocols.ipmi import IPMIProtocol

        bmc = MockIPMIBMC()
        proto = IPMIProtocol(host="10.0.0.1", backend_factory=bmc.factory())
        await proto.connect({"username": "admin", "password": "password"})
        assert await proto.collect_config() == {}
        await proto.disconnect()


def _make_restore_action(attributes: dict):
    queue = ActionQueue()
    action = queue.enqueue(
        ActionType.CONFIG_RESTORE, "config:test-baseline",
        "config-policy:test-baseline", VerdictSeverity.WARNING,
        {"attributes_json": json.dumps(attributes)},
    )
    queue.approve(action.id)
    return action


class TestConfigRestoreAction:
    async def test_restores_and_verifies(self, dell_sim, dell_client):
        dell_sim.inject_config_drift("SysLog.1.SysLogEnable", "Disabled")
        executor = ActionExecutor(
            dell_client, "dell",
            {"actions": {"allow_list": ["CONFIG_RESTORE"]}},
        )
        action = _make_restore_action({"SysLog.1.SysLogEnable": "Enabled"})
        outcome = await executor.execute(action)
        assert outcome.success is True
        assert dell_sim.bmc_attributes["SysLog.1.SysLogEnable"] == "Enabled"

    async def test_requires_attributes_json(self, dell_client):
        executor = ActionExecutor(
            dell_client, "dell",
            {"actions": {"allow_list": ["CONFIG_RESTORE"]}},
        )
        action = _make_restore_action({})
        action.params = {}
        outcome = await executor.execute(action)
        assert outcome.success is False
        assert "attributes_json" in outcome.error_message

    async def test_not_in_default_allow_list(self, dell_client):
        executor = ActionExecutor(dell_client, "dell", None)
        action = _make_restore_action({"SysLog.1.SysLogEnable": "Enabled"})
        outcome = await executor.execute(action)
        assert outcome.success is False
        assert "not in allow list" in outcome.error_message

    async def test_hpe_not_supported_yet(self):
        sim = MockSimulator(device="hpe-dl380-gen11", port=0, no_auth=True)
        await sim.start()
        client = RedfishClient(host=sim.url, verify_ssl=False, request_timeout=10)
        await client.connect("admin", "password")
        try:
            executor = ActionExecutor(
                client, "hpe", {"actions": {"allow_list": ["CONFIG_RESTORE"]}},
            )
            action = _make_restore_action({"X": "Y"})
            outcome = await executor.execute(action)
            assert outcome.success is False
            assert "not implemented" in outcome.error_message
        finally:
            await client.close()
            await sim.stop()


class TestPlaybookExecutorWiring:
    """R4-2: _run_action is no longer a stub -- steps hit the real BMC."""

    async def test_playbook_step_executes_real_action(self, dell_sim, dell_client):
        dell_sim.inject_config_drift("NTPConfigGroup.1.NTPEnable", "Disabled")
        action_executor = ActionExecutor(
            dell_client, "dell",
            {"actions": {"allow_list": ["CONFIG_RESTORE"]}},
        )
        policy = _policy()
        findings = detect_drift(
            {"NTPConfigGroup.1.NTPEnable": "Disabled"}, policy
        )
        playbook = build_remediation_playbook(findings, policy)

        async def config_state(device_id: str) -> dict:
            return dict(dell_sim.bmc_attributes)

        executor = PlaybookExecutor(
            action_executor=action_executor,
            get_device_state=config_state,
            verification_wait_scale=0.0,
        )
        execution = await executor.execute_playbook(playbook, "agent-1")
        assert execution.status == PlaybookStatus.COMPLETED
        assert dell_sim.bmc_attributes["NTPConfigGroup.1.NTPEnable"] == "Enabled"

    async def test_allow_list_applies_to_playbook_steps(self, dell_sim, dell_client):
        action_executor = ActionExecutor(dell_client, "dell", None)  # default list
        policy = _policy()
        findings = detect_drift(
            {"NTPConfigGroup.1.NTPEnable": "Disabled"}, policy
        )
        playbook = build_remediation_playbook(findings, policy)
        executor = PlaybookExecutor(
            action_executor=action_executor,
            get_device_state=None,
            verification_wait_scale=0.0,
        )
        execution = await executor.execute_playbook(playbook, "agent-1")
        assert execution.status != PlaybookStatus.COMPLETED
        assert "allow list" in execution.step_outcomes[0].error_message

    async def test_dry_run_changes_nothing(self, dell_sim, dell_client):
        dell_sim.inject_config_drift("NTPConfigGroup.1.NTPEnable", "Disabled")
        action_executor = ActionExecutor(
            dell_client, "dell",
            {"actions": {"allow_list": ["CONFIG_RESTORE"]}},
        )
        policy = _policy()
        findings = detect_drift(
            {"NTPConfigGroup.1.NTPEnable": "Disabled"}, policy
        )
        playbook = build_remediation_playbook(findings, policy)
        executor = PlaybookExecutor(
            action_executor=action_executor,
            get_device_state=None,
            verification_wait_scale=0.0,
            dry_run=True,
        )
        execution = await executor.execute_playbook(playbook, "agent-1")
        assert execution.status == PlaybookStatus.COMPLETED
        # Nothing actually changed on the BMC
        assert dell_sim.bmc_attributes["NTPConfigGroup.1.NTPEnable"] == "Disabled"


def make_agent_config(sim, tmp_path, **compliance_overrides):
    policy_dir = tmp_path / "policies"
    policy_dir.mkdir(exist_ok=True)
    (policy_dir / "test-baseline.yaml").write_text(POLICY_YAML)
    compliance = {
        "enabled": True,
        "policy_directory": str(policy_dir),
        "interval": 3600,
        "dry_run": False,
        "verification_wait_scale": 0.0,
    }
    compliance.update(compliance_overrides)
    return {
        "bmc": {"host": sim.url, "username": "admin", "password": "password",
                "verify_ssl": False},
        "skills": {"directory": str(REPO / "skills")},
        "polling": {"sensor_interval": 0.05},
        "actions": {"enabled": True, "approval_mode": "queue",
                    "allow_list": ["CONFIG_RESTORE"]},
        "compliance": compliance,
    }


class TestAgentComplianceFlow:
    """Exit criterion: drift detected and corrected via playbook."""

    async def test_drift_detected_proposed_and_corrected(self, dell_sim, tmp_path):
        agent = Agent(make_agent_config(dell_sim, tmp_path))
        await agent.start()
        try:
            assert set(agent.config_policies) == {"test-baseline"}

            # Compliant device: no findings with DRIFT status
            findings = await agent.check_compliance()
            assert all(f.status != "DRIFT" for f in findings)
            assert agent.action_queue.approved() == []

            # Inject drift -> proposal appears in the approval queue
            dell_sim.inject_config_drift("NTPConfigGroup.1.NTPEnable", "Disabled")
            findings = await agent.check_compliance()
            drift = [f for f in findings if f.status == "DRIFT"]
            assert len(drift) == 1
            pending = [a for a in agent.action_queue.all()
                       if a.type == ActionType.CONFIG_RESTORE]
            assert len(pending) == 1

            # Re-check does not duplicate the proposal
            await agent.check_compliance()
            pending = [a for a in agent.action_queue.all()
                       if a.type == ActionType.CONFIG_RESTORE]
            assert len(pending) == 1

            # Approve -> next poll cycle corrects via playbook
            agent.action_queue.approve(pending[0].id)
            await agent.poll_and_evaluate()
            assert pending[0].status == ActionStatus.COMPLETED
            assert pending[0].outcome.success is True
            assert dell_sim.bmc_attributes[
                "NTPConfigGroup.1.NTPEnable"] == "Enabled"

            # Follow-up compliance check is clean
            findings = await agent.check_compliance()
            assert all(f.status != "DRIFT" for f in findings)
        finally:
            await agent.stop()

    async def test_dry_run_reports_but_does_not_write(self, dell_sim, tmp_path):
        agent = Agent(make_agent_config(dell_sim, tmp_path, dry_run=True))
        await agent.start()
        try:
            dell_sim.inject_config_drift("SysLog.1.SysLogEnable", "Disabled")
            await agent.check_compliance()
            pending = [a for a in agent.action_queue.all()
                       if a.type == ActionType.CONFIG_RESTORE]
            agent.action_queue.approve(pending[0].id)
            await agent.poll_and_evaluate()
            assert pending[0].status == ActionStatus.COMPLETED
            # dry-run: the BMC still drifted
            assert dell_sim.bmc_attributes[
                "SysLog.1.SysLogEnable"] == "Disabled"
        finally:
            await agent.stop()
