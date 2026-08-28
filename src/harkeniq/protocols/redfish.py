"""RedfishProtocol -- DeviceProtocol implementation for Redfish BMCs (R4-0).

Wraps the existing RedfishClient, vendor normalizers (dell.py, hpe.py),
and discovery logic behind the DeviceProtocol interface. No logic changes
to existing code -- just restructuring behind the protocol abstraction.

Backward compatible: existing agent code that uses RedfishClient directly
continues to work. This wrapper is an additional entry point.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from harkeniq.protocols.device import ProtocolError
from harkeniq.redfish.client import RedfishClient

logger = logging.getLogger("harkeniq.protocols.redfish")


class RedfishDeviceProtocol:
    """DeviceProtocol implementation using Redfish REST API.

    Delegates to the existing RedfishClient for HTTP transport and to
    vendor-specific normalizers (dell.py, hpe.py) for data mapping.
    """

    def __init__(
        self,
        host: str,
        verify_ssl: bool = False,
        allow_list: Optional[list[str]] = None,
        **kwargs,
    ) -> None:
        self._host = host
        self._verify_ssl = verify_ssl
        # QA-023: the protocol previously granted itself EVERY ActionType
        # ("unrestricted by design") — any direct caller bypassed R-X6.
        # Default = the R1 locally-authorized set; the agent passes its
        # configured list.
        self._allow_list = allow_list
        self._client: Optional[RedfishClient] = None
        self._poller: Any = None
        self._identity: Any = None
        self._vendor: str = ""

    @property
    def name(self) -> str:
        return "redfish"

    @property
    def client(self) -> Optional[RedfishClient]:
        """Underlying RedfishClient (legacy accessor for existing agent code)."""
        return self._client

    @property
    def poller(self) -> Any:
        """Underlying Poller (legacy accessor for existing agent code)."""
        return self._poller

    async def connect(self, credentials: dict) -> None:
        """Connect to BMC via Redfish session authentication."""
        self._client = RedfishClient(
            host=self._host,
            verify_ssl=self._verify_ssl,
        )
        try:
            await self._client.connect(
                credentials.get("username", ""),
                credentials.get("password", ""),
            )
        except Exception as e:
            raise ConnectionError(f"Redfish connection failed: {e}") from e
        from harkeniq.poller import Poller
        self._poller = Poller(self._client)

    async def disconnect(self) -> None:
        if self._client:
            await self._client.close()
            self._client = None
            self._poller = None

    async def detect_identity(self) -> Any:
        """Detect vendor/model/serial via Redfish service root."""
        if self._client is None or self._poller is None:
            raise ProtocolError("Not connected")
        self._identity = await self._poller.detect()
        self._vendor = self._identity.vendor
        return self._identity

    async def poll_sensors(self) -> Any:
        """Poll all Redfish endpoints and normalize to NormalizedDevice."""
        if self._client is None or self._poller is None:
            raise ProtocolError("Not connected")
        if self._identity is None:
            await self.detect_identity()
        return await self._poller.poll_sensors()

    async def collect_config(self) -> dict:
        """Collect BMC config attributes for drift detection (R4-2 P13).

        Dell: GET DellAttributes -> Attributes dict. Other vendors
        return {} until their attribute surface is mapped.
        """
        if self._client is None:
            raise ProtocolError("Not connected")
        if self._identity is None:
            await self.detect_identity()
        if self._vendor != "dell":
            return {}
        manager_id = self._identity.manager_id or "iDRAC.Embedded.1"
        path = (
            f"/redfish/v1/Managers/{manager_id}/Oem/Dell/DellAttributes"
            f"/{manager_id}"
        )
        try:
            data = await self._client.get(path)
        except Exception as e:
            logger.warning("Config collection failed: %s", e)
            return {}
        attributes = data.get("Attributes", {})
        return dict(attributes) if isinstance(attributes, dict) else {}

    async def collect_firmware_inventory(self) -> list[dict]:
        """Collect BMC / BIOS / PSU firmware versions (R4-2 P14).

        Harvested from endpoints the poller already knows (manager,
        system, power) -- no UpdateService dependency, which older BMC
        firmware often lacks.
        """
        if self._client is None or self._poller is None:
            raise ProtocolError("Not connected")
        if self._identity is None:
            await self.detect_identity()
        inventory: list[dict] = []
        if self._identity.firmware_version:
            inventory.append({
                "component": "bmc",
                "name": self._identity.controller_type or "BMC",
                "version": str(self._identity.firmware_version),
            })
        paths = self._poller.paths
        try:
            system = await self._client.get(paths.system)
            bios = system.get("BiosVersion", "")
            if bios:
                inventory.append(
                    {"component": "bios", "name": "BIOS", "version": str(bios)}
                )
        except Exception as e:
            logger.warning("BIOS version collection failed: %s", e)
        try:
            power = await self._client.get(paths.power)
            for psu in power.get("PowerSupplies", []):
                version = psu.get("FirmwareVersion", "")
                if version:
                    inventory.append({
                        "component": "psu",
                        "name": psu.get("Name") or psu.get("MemberId", "PSU"),
                        "version": str(version),
                    })
        except Exception as e:
            logger.warning("PSU firmware collection failed: %s", e)
        return inventory

    async def execute_action(self, action_type: str, params: dict) -> dict:
        """Execute a Redfish action via the vendor-aware ActionExecutor.

        The internal executor enforces the allow list this protocol was
        constructed with (QA-023): the agent passes its configured list;
        direct callers get the R1 default set, never the full ActionType
        surface. Audit of approved actions remains the agent-level
        executor's responsibility.
        """
        if self._client is None:
            raise ProtocolError("Not connected")
        if self._identity is None:
            await self.detect_identity()
        from harkeniq.actions.executor import ActionExecutor
        from harkeniq.models import Action, ActionType

        try:
            atype = ActionType(action_type)
        except ValueError:
            return {"success": False, "error": f"Unknown action type: {action_type}",
                    "duration_ms": 0.0}
        from harkeniq.actions.executor import DEFAULT_ALLOW_LIST

        executor = ActionExecutor(
            self._client,
            self._vendor,
            config={"actions": {
                "allow_list": self._allow_list or list(DEFAULT_ALLOW_LIST),
            }},
        )
        action = Action(id=f"proto-{uuid.uuid4().hex[:8]}", type=atype,
                        params=dict(params))
        outcome = await executor.execute(action)
        return {
            "success": outcome.success,
            "error": outcome.error_message or "",
            "duration_ms": outcome.duration_ms,
        }
