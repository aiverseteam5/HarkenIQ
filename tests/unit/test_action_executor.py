"""Action executor tests against the mock simulator (Doc 06 §11A.4)."""

import pytest

from harkeniq.actions.executor import ActionExecutor
from harkeniq.actions.queue import ActionQueue
from harkeniq.mock.simulator import MockSimulator
from harkeniq.models import ActionStatus, ActionType, VerdictSeverity
from harkeniq.redfish.client import RedfishClient
from harkeniq.state.checkpoint import CheckpointManager

DELL_DRIVE = "Solid State Disk 0:1:0"
HPE_DRIVE = "NVMe Drive 0"


@pytest.fixture
async def dell_sim():
    sim = MockSimulator(device="dell-r750", port=0, no_auth=True)
    await sim.start()
    yield sim
    await sim.stop()


@pytest.fixture
async def hpe_sim():
    sim = MockSimulator(device="hpe-dl360-gen10", port=0, no_auth=True)
    await sim.start()
    yield sim
    await sim.stop()


@pytest.fixture
async def dell_client(dell_sim):
    client = RedfishClient(host=dell_sim.url, verify_ssl=False, request_timeout=10)
    await client.connect("admin", "password")
    yield client
    await client.close()


@pytest.fixture
async def hpe_client(hpe_sim):
    client = RedfishClient(host=hpe_sim.url, verify_ssl=False, request_timeout=10)
    await client.connect("admin", "password")
    yield client
    await client.close()


def make_action(action_type=ActionType.IDENTIFY_LED, target=DELL_DRIVE, approve=True):
    queue = ActionQueue()
    params = {"target": target} if target else {}
    action = queue.enqueue(
        action_type, "disk:sda", "disk-health", VerdictSeverity.CRITICAL, params
    )
    if approve:
        queue.approve(action.id)
    return action


class TestIdentifyLed:
    async def test_dell_led_success(self, dell_sim, dell_client):
        executor = ActionExecutor(dell_client, "dell")
        action = make_action(target=DELL_DRIVE)
        outcome = await executor.execute(action)
        assert outcome.success is True
        assert outcome.target == DELL_DRIVE
        assert outcome.duration_ms > 0
        assert action.status == ActionStatus.COMPLETED
        assert action.completed_at
        assert action.outcome is outcome
        assert dell_sim.action_state["led"][DELL_DRIVE] == "Blinking"

    async def test_hpe_led_success(self, hpe_sim, hpe_client):
        executor = ActionExecutor(hpe_client, "hpe")
        action = make_action(target=HPE_DRIVE)
        outcome = await executor.execute(action)
        assert outcome.success is True
        assert hpe_sim.action_state["led"][HPE_DRIVE] == "Blinking"

    async def test_led_verified_via_get(self, dell_sim, dell_client):
        executor = ActionExecutor(dell_client, "dell")
        await executor.execute(make_action(target=DELL_DRIVE))
        data = await dell_client.get(
            "/redfish/v1/Chassis/System.Embedded.1/Drives/Solid%20State%20Disk%200%3A1%3A0"
        )
        assert data["IndicatorLED"] == "Blinking"

    async def test_unknown_drive_fails(self, dell_client):
        executor = ActionExecutor(dell_client, "dell")
        action = make_action(target="No Such Drive")
        outcome = await executor.execute(action)
        assert outcome.success is False
        assert action.status == ActionStatus.FAILED
        assert "404" in outcome.error_message

    async def test_missing_target_fails(self, dell_client):
        executor = ActionExecutor(dell_client, "dell")
        action = make_action(target=None)
        outcome = await executor.execute(action)
        assert outcome.success is False
        assert "target" in outcome.error_message


class TestCollectDiagnostics:
    async def test_dell_export_system_configuration(self, dell_sim, dell_client):
        executor = ActionExecutor(dell_client, "dell")
        action = make_action(ActionType.COLLECT_DIAGNOSTICS, target="")
        outcome = await executor.execute(action)
        assert outcome.success is True
        diags = dell_sim.action_state["diagnostics"]
        assert diags[0]["vendor"] == "dell"
        assert diags[0]["params"]["ExportFormat"] == "JSON"

    async def test_hpe_active_health_system(self, hpe_sim, hpe_client):
        executor = ActionExecutor(hpe_client, "hpe")
        action = make_action(ActionType.COLLECT_DIAGNOSTICS, target="")
        outcome = await executor.execute(action)
        assert outcome.success is True
        assert hpe_sim.action_state["diagnostics"][0]["resource"] == "ActiveHealthSystem"


class TestFanReset:
    async def test_dell_fan_speed_offset(self, dell_sim, dell_client):
        executor = ActionExecutor(dell_client, "dell")
        action = make_action(ActionType.FAN_RESET, target="")
        outcome = await executor.execute(action)
        assert outcome.success is True
        reset = dell_sim.action_state["fan_reset"][0]
        assert reset["attributes"]["ThermalSettings.1.FanSpeedOffset"] == "Off"

    async def test_hpe_thermal_patch(self, hpe_sim, hpe_client):
        executor = ActionExecutor(hpe_client, "hpe")
        action = make_action(ActionType.FAN_RESET, target="")
        outcome = await executor.execute(action)
        assert outcome.success is True
        assert hpe_sim.action_state["fan_reset"][0]["vendor"] == "hpe"


class TestAllowListAndAudit:
    async def test_refused_when_not_in_allow_list(self, dell_sim, dell_client, tmp_path):
        cp = CheckpointManager(tmp_path / "checkpoint.db")
        executor = ActionExecutor(
            dell_client, "dell",
            config={"actions": {"allow_list": ["IDENTIFY_LED"]}},
            checkpoint=cp,
        )
        action = make_action(ActionType.FAN_RESET, target="")
        outcome = await executor.execute(action)
        assert outcome.success is False
        assert "allow list" in outcome.error_message
        assert action.status == ActionStatus.FAILED
        # nothing hit the BMC
        assert dell_sim.action_state["fan_reset"] == []
        rows = cp._conn.execute("SELECT * FROM audit_log").fetchall()
        await cp.close()
        assert [r["outcome"] for r in rows] == ["refused"]

    async def test_audit_trail_on_success(self, dell_client, tmp_path):
        cp = CheckpointManager(tmp_path / "checkpoint.db")
        executor = ActionExecutor(dell_client, "dell", checkpoint=cp)
        await executor.execute(make_action(target=DELL_DRIVE))
        rows = cp._conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        await cp.close()
        assert [r["outcome"] for r in rows] == ["executing", "success"]
        assert rows[0]["action"] == "IDENTIFY_LED"
        assert rows[0]["target"] == DELL_DRIVE

    async def test_audit_trail_on_failure(self, dell_client, tmp_path):
        cp = CheckpointManager(tmp_path / "checkpoint.db")
        executor = ActionExecutor(dell_client, "dell", checkpoint=cp)
        await executor.execute(make_action(target="No Such Drive"))
        rows = cp._conn.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        await cp.close()
        assert [r["outcome"] for r in rows] == ["executing", "failed"]

    async def test_unsupported_vendor_rejected(self, dell_client):
        from harkeniq.errors import ActionError

        with pytest.raises(ActionError, match="vendor"):
            ActionExecutor(dell_client, "supermicro")


class TestSimulatorReset:
    async def test_reset_clears_action_state(self, dell_sim, dell_client):
        executor = ActionExecutor(dell_client, "dell")
        await executor.execute(make_action(target=DELL_DRIVE))
        assert dell_sim.action_state["led"]
        await dell_sim.reset()
        assert dell_sim.action_state == {"led": {}, "diagnostics": [], "fan_reset": []}
