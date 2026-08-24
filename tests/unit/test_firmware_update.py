"""Device-level firmware update tests (R4-3 P19, OQ-21).

The real Redfish path: ActionExecutor FIRMWARE_UPDATE drives the
simulator's UpdateService (SimpleUpdate -> task poll -> verify), and
FIRMWARE_ROLLBACK swaps back to the blue-green standby bank.
"""

from __future__ import annotations

import pytest

from harkeniq.actions.executor import ActionExecutor
from harkeniq.actions.queue import ActionQueue
from harkeniq.autonomy.preconditions import check_preconditions
from harkeniq.mock.simulator import MockSimulator
from harkeniq.models import ActionType, VerdictSeverity
from harkeniq.redfish.client import RedfishClient

ALLOW = {"actions": {"allow_list": ["FIRMWARE_UPDATE", "FIRMWARE_ROLLBACK"]}}


@pytest.fixture
async def dell_sim():
    sim = MockSimulator(device="dell-r750", port=0, no_auth=True)
    await sim.start()
    yield sim
    await sim.stop()


@pytest.fixture
async def executor(dell_sim):
    client = RedfishClient(host=dell_sim.url, verify_ssl=False, request_timeout=10)
    await client.connect("admin", "password")
    ex = ActionExecutor(client, "dell", ALLOW)
    ex.task_poll_interval = 0.0
    yield ex
    await client.close()


def _action(action_type: ActionType, params: dict):
    queue = ActionQueue()
    action = queue.enqueue(
        action_type, "firmware:bmc", "firmware-campaign",
        VerdictSeverity.CRITICAL, params,
    )
    queue.approve(action.id)
    return action


class TestFirmwareUpdate:
    async def test_update_applies_and_verifies(self, dell_sim, executor):
        assert dell_sim.firmware_banks["bmc"]["active"] == "7.00.00.00"
        outcome = await executor.execute(_action(
            ActionType.FIRMWARE_UPDATE,
            {"component": "bmc", "target_version": "7.10.30.00"},
        ))
        assert outcome.success is True, outcome.error_message
        banks = dell_sim.firmware_banks["bmc"]
        # Blue-green: new active, old preserved as standby
        assert banks["active"] == "7.10.30.00"
        assert banks["standby"] == "7.00.00.00"

    async def test_failed_update_reports_failure(self, dell_sim, executor):
        dell_sim.inject_firmware_update_failure()
        outcome = await executor.execute(_action(
            ActionType.FIRMWARE_UPDATE,
            {"component": "bmc", "target_version": "7.10.30.00"},
        ))
        assert outcome.success is False
        assert "Exception" in outcome.error_message
        # Nothing changed on the device
        assert dell_sim.firmware_banks["bmc"]["active"] == "7.00.00.00"

    async def test_rollback_swaps_to_standby(self, dell_sim, executor):
        await executor.execute(_action(
            ActionType.FIRMWARE_UPDATE,
            {"component": "bmc", "target_version": "7.10.30.00"},
        ))
        outcome = await executor.execute(_action(
            ActionType.FIRMWARE_ROLLBACK,
            {"component": "bmc", "expected_version": "7.00.00.00"},
        ))
        assert outcome.success is True
        assert dell_sim.firmware_banks["bmc"]["active"] == "7.00.00.00"

    async def test_rollback_without_standby_fails(self, dell_sim, executor):
        outcome = await executor.execute(_action(
            ActionType.FIRMWARE_ROLLBACK, {"component": "bmc"},
        ))
        assert outcome.success is False

    async def test_requires_target_version(self, executor):
        outcome = await executor.execute(_action(
            ActionType.FIRMWARE_UPDATE, {"component": "bmc"},
        ))
        assert outcome.success is False
        assert "target_version" in outcome.error_message

    async def test_not_in_default_allow_list(self, dell_sim):
        client = RedfishClient(host=dell_sim.url, verify_ssl=False,
                               request_timeout=10)
        await client.connect("admin", "password")
        try:
            ex = ActionExecutor(client, "dell", None)  # default allow list
            outcome = await ex.execute(_action(
                ActionType.FIRMWARE_UPDATE,
                {"component": "bmc", "target_version": "7.10.30.00"},
            ))
            assert outcome.success is False
            assert "not in allow list" in outcome.error_message
        finally:
            await client.close()

    async def test_non_bmc_component_rejected(self, executor):
        outcome = await executor.execute(_action(
            ActionType.FIRMWARE_UPDATE,
            {"component": "bios", "target_version": "2.0"},
        ))
        assert outcome.success is False
        assert "not implemented" in outcome.error_message


class TestFirmwarePreconditions:
    def test_healthy_device_passes(self):
        result = check_preconditions(
            ActionType.FIRMWARE_UPDATE,
            {"overall_health": "OK"}, {},
        )
        assert result.passed is True

    def test_degraded_device_refused(self):
        result = check_preconditions(
            ActionType.FIRMWARE_UPDATE,
            {"overall_health": "Critical"}, {},
        )
        assert result.passed is False
        assert any("health" in f.lower() for f in result.failed_checks)

    def test_concurrent_update_refused(self):
        result = check_preconditions(
            ActionType.FIRMWARE_UPDATE,
            {"overall_health": "OK", "firmware_update_in_progress": True}, {},
        )
        assert result.passed is False
