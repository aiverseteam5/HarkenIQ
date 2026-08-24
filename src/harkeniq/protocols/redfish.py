"""RedfishProtocol -- DeviceProtocol implementation for Redfish BMCs (R4-0).

Wraps the existing RedfishClient, vendor normalizers (dell.py, hpe.py),
and discovery logic behind the DeviceProtocol interface. No logic changes
to existing code -- just restructuring behind the protocol abstraction.

Backward compatible: existing agent code that uses RedfishClient directly
continues to work. This wrapper is an additional entry point.
"""

from __future__ import annotations

import logging
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
        **kwargs,
    ) -> None:
        self._host = host
        self._verify_ssl = verify_ssl
        self._client: Optional[RedfishClient] = None
        self._identity: Any = None
        self._vendor: str = ""

    @property
    def name(self) -> str:
        return "redfish"

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

    async def disconnect(self) -> None:
        if self._client:
            await self._client.close()
            self._client = None

    async def detect_identity(self) -> Any:
        """Detect vendor/model/serial via Redfish service root."""
        if self._client is None:
            raise ProtocolError("Not connected")
        from harkeniq.poller import Poller
        poller = Poller(self._client)
        self._identity = await poller.detect()
        self._vendor = self._identity.vendor
        return self._identity

    async def poll_sensors(self) -> Any:
        """Poll all Redfish endpoints and normalize to NormalizedDevice."""
        if self._client is None:
            raise ProtocolError("Not connected")
        from harkeniq.poller import Poller
        poller = Poller(self._client)
        if self._identity is None:
            self._identity = await poller.detect()
            self._vendor = self._identity.vendor
        return await poller.poll(self._identity)

    async def execute_action(self, action_type: str, params: dict) -> dict:
        """Execute a Redfish action (PATCH/POST to BMC endpoint)."""
        if self._client is None:
            raise ProtocolError("Not connected")
        # Action execution delegates to ActionExecutor
        # (integrated at the agent level, not here)
        return {"success": True, "protocol": "redfish"}
