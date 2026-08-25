"""DeviceProtocol -- abstract interface for device communication (R4-0).

All device protocols (Redfish, IPMI, gNMI, NETCONF) implement this
interface. Everything above this boundary (skills, trending, diagnosis,
autonomy, actions, fleet learning) operates on NormalizedDevice and
never references a specific protocol.

Architecture guarantee (from R4 Amendment):
  Adding gNMI or NETCONF later MUST NOT require changes to the reasoning,
  autonomy, skills, playbooks, or fleet-learning layers.

Contract notes:
  - Lifecycle: Poller owns connect() and disconnect() calls.
  - Errors: connect() raises ConnectionError on auth failure, TimeoutError
    on network unreachable. poll_sensors() raises ProtocolError on malformed
    response. All are subclasses of HarkenIQError.
  - Idempotency: poll_sensors() is idempotent and safe at any frequency.
    execute_action() may have side effects.
  - Credentials: Passed via connect(credentials). Credential refresh is
    the Poller's responsibility.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable

from harkeniq.errors import HarkenIQError


class ProtocolError(HarkenIQError):
    """Device protocol communication error."""


@runtime_checkable
class DeviceProtocol(Protocol):
    """Abstract device communication and normalization protocol.

    Implementations handle:
    - Transport layer (HTTP, gRPC, SSH, UDP, etc.)
    - Vendor detection
    - Resource discovery
    - Raw data fetching
    - Normalization to canonical NormalizedDevice
    """

    async def connect(self, credentials: dict) -> None:
        """Establish connection to device.

        Raises:
            ConnectionError: auth failure
            TimeoutError: network unreachable
        """
        ...

    async def disconnect(self) -> None:
        """Close connection and release resources."""
        ...

    async def detect_identity(self) -> Any:
        """Detect vendor, model, serial number.

        Returns a DeviceIdentity-compatible object (vendor, model,
        service_tag fields).
        """
        ...

    async def poll_sensors(self) -> Any:
        """Single poll cycle: fetch all sensor data and normalize.

        Returns a NormalizedDevice (protocol-agnostic sensor data).
        Idempotent and safe to call at any frequency.

        Raises:
            ProtocolError: malformed response from device
            TimeoutError: device unreachable
        """
        ...

    async def execute_action(self, action_type: str, params: dict) -> dict:
        """Execute an approved action on the device.

        Args:
            action_type: e.g., "IDENTIFY_LED", "SEL_CLEAR"
            params: action-specific parameters

        Returns:
            {"success": bool, "error": str (if failed), "duration_ms": float}
        """
        ...

    async def collect_config(self) -> dict:
        """Collect the device's BMC configuration attributes (R4-2 P13).

        Returns a flat {attribute_name: value} dict for config-drift
        detection. Protocols/vendors without a config surface return {}
        (drift detection then reports UNKNOWN, never a false drift).
        """
        ...

    async def collect_firmware_inventory(self) -> list[dict]:
        """Collect per-component firmware versions (R4-2 P14, R-AGENT-17).

        Returns [{"component": "bmc"|"bios"|"psu"|..., "name": str,
        "version": str}]. Components the protocol cannot observe are
        omitted, never guessed.
        """
        ...

    @property
    def name(self) -> str:
        """Protocol name: 'redfish' | 'ipmi' | 'gnmi' | etc."""
        ...


def create_device_protocol(
    protocol_name: str,
    host: str,
    **kwargs,
) -> DeviceProtocol:
    """Factory: create a DeviceProtocol from config.

    Args:
        protocol_name: "redfish" (default) | "ipmi" | "gnmi"
        host: device address
        **kwargs: protocol-specific config

    Returns:
        DeviceProtocol implementation
    """
    if protocol_name == "redfish":
        from harkeniq.protocols.redfish import RedfishDeviceProtocol
        return RedfishDeviceProtocol(host=host, **kwargs)
    if protocol_name == "ipmi":
        from harkeniq.protocols.ipmi import IPMIProtocol
        return IPMIProtocol(host=host, **kwargs)
    if protocol_name == "gnmi":
        from harkeniq.protocols.gnmi import GNMIProtocol
        return GNMIProtocol(host=host, **kwargs)
    raise ValueError(
        f"Unknown protocol: {protocol_name!r}. Supported: redfish, ipmi, gnmi"
    )
