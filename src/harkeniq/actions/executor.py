"""Action executor: performs approved actions against the BMC (Doc 06 §11A.4).

R1 actions are all safe-on-self: IDENTIFY_LED, COLLECT_DIAGNOSTICS,
FAN_RESET. Every execution is allow-list checked and audit logged
(refused / executing / success / failed).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

from harkeniq.errors import ActionError, RedfishError
from harkeniq.models import Action, ActionOutcome, ActionStatus, ActionType
from harkeniq.redfish.client import RedfishClient

logger = logging.getLogger("harkeniq.actions")

DEFAULT_ALLOW_LIST = ["IDENTIFY_LED", "COLLECT_DIAGNOSTICS", "FAN_RESET"]

#: The action classes this executor's OWN dispatch chain implements
#: (Redfish direct path). Capability Registry: this is a declaration of
#: implementation REACH, never of policy -- ``allow_list`` above is the
#: policy, and the two are deliberately separate because the Registry has
#: to be able to say "the code can do this, but this node does not permit
#: it", which a single merged list can never express.
#:
#: Kept honest by test_capability_declaration_truthful: every member must
#: actually dispatch, and every ActionType outside it must not.
EXECUTOR_DISPATCH_ACTIONS = frozenset({
    "IDENTIFY_LED",
    "COLLECT_DIAGNOSTICS",
    "FAN_RESET",
    "CONFIG_RESTORE",
    "FIRMWARE_UPDATE",
    "FIRMWARE_ROLLBACK",
    "SEL_CLEAR",
    "BMC_RESET",
    "POWER_CYCLE",
    "POWER_CAP_ADJUST",
})

_VENDOR_IDS = {
    "dell": {"chassis": "System.Embedded.1", "manager": "iDRAC.Embedded.1"},
    "hpe": {"chassis": "1", "manager": "1"},
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ActionExecutor:
    """Executes approved actions via per-vendor Redfish endpoints."""

    def __init__(
        self,
        client: Optional[RedfishClient],
        vendor: str,
        config: Optional[dict] = None,
        checkpoint=None,
        protocol=None,
    ) -> None:
        # R4-1: when a non-Redfish DeviceProtocol is supplied, dispatch goes
        # through protocol.execute_action() and the Redfish vendor coupling
        # (chassis/manager IDs) does not apply. Redfish keeps the legacy
        # direct-dispatch path unchanged.
        self.protocol = protocol
        self._protocol_dispatch = (
            protocol is not None and getattr(protocol, "name", "redfish") != "redfish"
        )
        if self._protocol_dispatch:
            self.vendor = vendor
            self.chassis_id = ""
            self.manager_id = ""
        else:
            if vendor not in _VENDOR_IDS:
                raise ActionError(f"Unsupported vendor: {vendor}")
            self.vendor = vendor
            self.chassis_id = _VENDOR_IDS[vendor]["chassis"]
            self.manager_id = _VENDOR_IDS[vendor]["manager"]
        self.client = client
        actions_cfg = (config or {}).get("actions") or {}
        self.allow_list: list[str] = list(
            actions_cfg.get("allow_list", DEFAULT_ALLOW_LIST)
        )
        self.checkpoint = checkpoint

    async def execute(self, action: Action) -> ActionOutcome:
        """Execute an approved action; returns the outcome (never raises).

        Updates ``action.status`` / ``completed_at`` / ``outcome`` in place
        and writes audit entries via the checkpoint manager when present.
        """
        target = action.params.get("target", "") or action.sensor_id

        if action.type.value not in self.allow_list:
            logger.warning("Action %s refused: %s not in allow list", action.id, action.type.value)
            outcome = self._finish(
                action, target, success=False,
                error=f"Action type {action.type.value} not in allow list",
                started=None,
            )
            await self._audit(action, target, "refused")
            return outcome

        action.status = ActionStatus.EXECUTING
        await self._audit(action, target, "executing")
        started = time.monotonic()
        try:
            if self._protocol_dispatch:
                try:
                    result = await self.protocol.execute_action(
                        action.type.value, dict(action.params)
                    )
                except (ActionError, RedfishError):
                    raise
                except Exception as e:  # ProtocolError, TimeoutError, ...
                    raise ActionError(
                        f"{action.type.value} failed via {self.protocol.name}: {e}"
                    ) from e
                if not result.get("success"):
                    raise ActionError(
                        result.get("error")
                        or f"{action.type.value} failed via {self.protocol.name}"
                    )
            elif action.type == ActionType.IDENTIFY_LED:
                await self._identify_led(action)
            elif action.type == ActionType.COLLECT_DIAGNOSTICS:
                await self._collect_diagnostics()
            elif action.type == ActionType.FAN_RESET:
                await self._fan_reset()
            elif action.type == ActionType.CONFIG_RESTORE:
                await self._config_restore(action)
            elif action.type == ActionType.FIRMWARE_UPDATE:
                await self._firmware_update(action)
            elif action.type == ActionType.FIRMWARE_ROLLBACK:
                await self._firmware_rollback(action)
            elif action.type == ActionType.SEL_CLEAR:
                await self._sel_clear()
            elif action.type == ActionType.BMC_RESET:
                await self._bmc_reset()
            elif action.type == ActionType.POWER_CYCLE:
                await self._power_cycle(action)
            elif action.type == ActionType.POWER_CAP_ADJUST:
                await self._power_cap_adjust(action)
            else:  # pragma: no cover - allow list guards this
                raise ActionError(f"Unknown action type: {action.type}")
        except (RedfishError, ActionError) as e:
            logger.error("Action %s (%s) failed: %s", action.id, action.type.value, e)
            outcome = self._finish(action, target, success=False, error=str(e), started=started)
            await self._audit(action, target, "failed")
            return outcome

        logger.info("Action %s (%s) on %s succeeded", action.id, action.type.value, target)
        outcome = self._finish(action, target, success=True, error=None, started=started)
        await self._audit(action, target, "success")
        return outcome

    # -- per-action endpoints (Doc 06 §11A.4) -------------------------------

    async def _identify_led(self, action: Action) -> None:
        drive = action.params.get("target", "")
        if not drive:
            raise ActionError("IDENTIFY_LED requires a 'target' param (drive name)")
        path = f"/redfish/v1/Chassis/{self.chassis_id}/Drives/{quote(drive, safe='')}"
        await self.client.patch(path, {"IndicatorLED": "Blinking"})
        data = await self.client.get(path)
        if data.get("IndicatorLED") != "Blinking":
            raise ActionError(
                f"IDENTIFY_LED verification failed: IndicatorLED is "
                f"{data.get('IndicatorLED')!r}, expected 'Blinking'"
            )

    async def _collect_diagnostics(self) -> None:
        if self.vendor == "dell":
            path = (
                f"/redfish/v1/Managers/{self.manager_id}/Oem/Dell/DellLCService"
                "/Actions/DellLCService.ExportSystemConfiguration"
            )
            await self.client.post(path, {"ExportFormat": "JSON", "ShareType": "Local"})
        else:
            await self.client.get(
                f"/redfish/v1/Managers/{self.manager_id}/ActiveHealthSystem"
            )

    async def _config_restore(self, action: Action) -> None:
        """Restore drifted BMC config attributes to policy values (R4-2).

        Params: {"attributes_json": '{"Key": "expected", ...}'}. Dell:
        PATCH DellAttributes then GET to verify each restored value.
        """
        raw = action.params.get("attributes_json", "")
        if not raw:
            raise ActionError("CONFIG_RESTORE requires an 'attributes_json' param")
        try:
            attributes = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ActionError(f"CONFIG_RESTORE attributes_json is not valid JSON: {e}")
        if not isinstance(attributes, dict) or not attributes:
            raise ActionError("CONFIG_RESTORE attributes_json must be a non-empty object")

        if self.vendor != "dell":
            raise ActionError(
                f"CONFIG_RESTORE not implemented for vendor {self.vendor!r} (R4-2: Dell first)"
            )
        path = (
            f"/redfish/v1/Managers/{self.manager_id}/Oem/Dell/DellAttributes"
            f"/{self.manager_id}"
        )
        await self.client.patch(path, {"Attributes": attributes})
        data = await self.client.get(path)
        current = data.get("Attributes", {})
        for key, expected in attributes.items():
            if current.get(key) != expected:
                raise ActionError(
                    f"CONFIG_RESTORE verification failed: {key} is "
                    f"{current.get(key)!r}, expected {expected!r}"
                )

    # -- firmware update (R4-3 P19, OQ-21) -----------------------------------
    #
    # Blue-green semantics: SimpleUpdate writes the new image; the BMC keeps
    # the previous bank as standby. Verification re-reads the running
    # version; FIRMWARE_ROLLBACK swaps back to the standby bank. These
    # actions are campaign-driven (SM FirmwareOrchestrator) -- never
    # proposed by skills -- and are absent from the default allow list.

    #: Seconds between task polls; tests set 0.
    task_poll_interval: float = 2.0
    #: Give up after this many polls (~2 min at the default interval).
    task_poll_limit: int = 60

    async def _firmware_update(self, action: Action) -> None:
        target_version = action.params.get("target_version", "")
        image_uri = action.params.get("image_uri", "")
        component = action.params.get("component", "bmc")
        if not target_version:
            raise ActionError("FIRMWARE_UPDATE requires a 'target_version' param")
        if component != "bmc":
            raise ActionError(
                f"FIRMWARE_UPDATE for component {component!r} not implemented "
                "(R4-3: BMC first)"
            )
        if not image_uri:
            image_uri = f"harkeniq://firmware/{component}-{target_version}.bin"

        response = await self.client.post(
            "/redfish/v1/UpdateService/Actions/UpdateService.SimpleUpdate",
            {"ImageURI": image_uri, "Targets": []},
        )
        task_uri = response.get("@odata.id", "")
        if not task_uri:
            raise ActionError("SimpleUpdate did not return a task")

        state = await self._poll_task(task_uri)
        if state != "Completed":
            raise ActionError(f"Firmware update task ended in {state}")

        # Verify the running version (fresh read; never a cached identity)
        manager = await self.client.get(
            f"/redfish/v1/Managers/{self.manager_id}"
        )
        running = manager.get("FirmwareVersion", "")
        if running != target_version:
            raise ActionError(
                f"Firmware verification failed: running {running!r}, "
                f"expected {target_version!r}"
            )

    async def _firmware_rollback(self, action: Action) -> None:
        component = action.params.get("component", "bmc")
        response = await self.client.post(
            "/redfish/v1/UpdateService/Actions/Oem/HarkenIQ.FirmwareRollback",
            {"Component": component},
        )
        expected = action.params.get("expected_version", "")
        active = response.get("ActiveVersion", "")
        if expected and active != expected:
            raise ActionError(
                f"Rollback verification failed: active {active!r}, "
                f"expected {expected!r}"
            )

    async def _poll_task(self, task_uri: str) -> str:
        import asyncio

        for _ in range(self.task_poll_limit):
            task = await self.client.get(task_uri)
            state = task.get("TaskState", "")
            if state in ("Completed", "Exception", "Killed", "Cancelled"):
                return state
            if self.task_poll_interval > 0:
                await asyncio.sleep(self.task_poll_interval)
        raise ActionError(f"Firmware task did not finish: {task_uri}")

    # -- R3a actions (QA-020: these four had no Redfish branch until R7) ----

    async def _sel_clear(self) -> None:
        """Clear the hardware event log: Dell SEL / HPE IML (atomic)."""
        if self.vendor == "dell":
            path = (
                f"/redfish/v1/Managers/{self.manager_id}/LogServices/Sel"
                "/Actions/LogService.ClearLog"
            )
        else:
            path = (
                f"/redfish/v1/Systems/{self.chassis_id}/LogServices/IML"
                "/Actions/LogService.ClearLog"
            )
        await self.client.post(path, {})

    async def _bmc_reset(self) -> None:
        """Graceful BMC restart; the host OS is untouched."""
        await self.client.post(
            f"/redfish/v1/Managers/{self.manager_id}/Actions/Manager.Reset",
            {"ResetType": "GracefulRestart"},
        )

    async def _power_cycle(self, action: Action) -> None:
        """Host power cycle (A2.1 preconditions gate this upstream)."""
        reset_type = action.params.get("reset_type", "ForceRestart")
        await self.client.post(
            f"/redfish/v1/Systems/{self.chassis_id}"
            "/Actions/ComputerSystem.Reset",
            {"ResetType": reset_type},
        )

    async def _power_cap_adjust(self, action: Action) -> None:
        """Set the chassis power cap and read back the applied value."""
        raw = action.params.get("target_watts", "")
        try:
            target = int(raw)
        except (TypeError, ValueError):
            raise ActionError(
                "POWER_CAP_ADJUST requires an integer 'target_watts' param"
            )
        path = f"/redfish/v1/Chassis/{self.chassis_id}/Power"
        await self.client.patch(
            path, {"PowerControl": [{"PowerLimit": {"LimitInWatts": target}}]}
        )
        data = await self.client.get(path)
        control = (data.get("PowerControl") or [{}])[0]
        applied = (control.get("PowerLimit") or {}).get("LimitInWatts")
        if applied != target:
            raise ActionError(
                f"POWER_CAP_ADJUST verification failed: cap is {applied!r}, "
                f"expected {target}"
            )

    async def _fan_reset(self) -> None:
        if self.vendor == "dell":
            path = (
                f"/redfish/v1/Managers/{self.manager_id}/Oem/Dell/DellAttributes"
                f"/{self.manager_id}"
            )
            await self.client.patch(
                path, {"Attributes": {"ThermalSettings.1.FanSpeedOffset": "Off"}}
            )
        else:
            await self.client.patch(
                f"/redfish/v1/Chassis/{self.chassis_id}/Thermal",
                {"Oem": {"Hpe": {"ThermalConfiguration": "OptimalCooling"}}},
            )

    # -- helpers ------------------------------------------------------------

    def _finish(
        self,
        action: Action,
        target: str,
        success: bool,
        error: Optional[str],
        started: Optional[float],
    ) -> ActionOutcome:
        outcome = ActionOutcome(
            action_id=action.id,
            type=action.type,
            target=target,
            success=success,
            error_message=error,
            duration_ms=(time.monotonic() - started) * 1000.0 if started else 0.0,
            timestamp=_utc_now(),
        )
        action.status = ActionStatus.COMPLETED if success else ActionStatus.FAILED
        action.completed_at = outcome.timestamp
        action.outcome = outcome
        return outcome

    async def _audit(self, action: Action, target: str, outcome: str) -> None:
        if self.checkpoint is None:
            return
        await self.checkpoint.save_audit_entry(
            action=action.type.value,
            target=target,
            outcome=outcome,
            evidence_json=json.dumps(
                {"action_id": action.id, "sensor_id": action.sensor_id,
                 "skill": action.skill_name}
            ),
        )
