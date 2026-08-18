"""BMC auto-detection and vendor identification (Doc 08 §2, Doc 05 §9).

Detects vendor (Dell/HPE), controller generation (iDRAC9/10, iLO5/6),
and resolves vendor-specific resource IDs. Runs once at agent startup.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from harkeniq.errors import RedfishConnectionError
from harkeniq.redfish.client import RedfishClient
from harkeniq.redfish.normalize import DeviceIdentity

logger = logging.getLogger("harkeniq.redfish.discovery")

# BMC auto-detect probe addresses (Doc 06 §5.2)
BMC_PROBE_ADDRESSES = [
    ("169.254.0.1", 443),   # Dell USB-NIC (iDRAC Direct)
    ("169.254.0.2", 443),   # HPE USB-NIC
    ("127.0.0.1", 443),     # localhost fallback
]


@dataclass
class RedfishPaths:
    """Vendor-specific Redfish URL paths, constructed from DeviceIdentity."""

    thermal: str
    power: str
    storage: str
    memory: str
    manager: str
    system: str
    service_root: str = "/redfish/v1/"
    sel_entries: str = ""     # Dell SEL or HPE IML
    smartstorage: str = ""    # HPE iLO5 only

    @classmethod
    def from_identity(cls, identity: DeviceIdentity) -> RedfishPaths:
        """Build paths from detected device identity."""
        cid = identity.chassis_id
        sid = identity.system_id
        mid = identity.manager_id

        paths = cls(
            thermal=f"/redfish/v1/Chassis/{cid}/Thermal",
            power=f"/redfish/v1/Chassis/{cid}/Power",
            storage=f"/redfish/v1/Systems/{sid}/Storage",
            memory=f"/redfish/v1/Systems/{sid}/Memory",
            manager=f"/redfish/v1/Managers/{mid}",
            system=f"/redfish/v1/Systems/{sid}",
        )

        if identity.vendor == "dell":
            paths.sel_entries = f"/redfish/v1/Managers/{mid}/LogServices/Sel/Entries"
        elif identity.vendor == "hpe":
            paths.sel_entries = f"/redfish/v1/Systems/{sid}/LogServices/IML/Entries"

        return paths


async def detect_vendor(service_root: dict[str, Any]) -> str:
    """Identify vendor from ServiceRoot Oem namespace.

    Returns "dell" or "hpe".
    Raises ValueError for unsupported vendors.
    """
    oem = service_root.get("Oem", {})
    if "Dell" in oem:
        return "dell"
    if "Hpe" in oem:
        return "hpe"
    raise ValueError(f"Unsupported vendor. Oem keys: {list(oem.keys())}")


async def detect_controller(vendor: str, manager_data: dict[str, Any]) -> tuple[str, str]:
    """Identify controller type and version from Manager response.

    Returns (controller_type, controller_version) e.g. ("iDRAC", "9").
    """
    model = manager_data.get("Model", "")

    if vendor == "dell":
        if "iDRAC10" in model:
            return "iDRAC", "10"
        if "iDRAC9" in model:
            return "iDRAC", "9"
        # Fallback: extract from model string
        return "iDRAC", model.replace("iDRAC", "").strip() or "unknown"

    if vendor == "hpe":
        if "iLO 6" in model:
            return "iLO", "6"
        if "iLO 5" in model:
            return "iLO", "5"
        return "iLO", model.replace("iLO ", "").strip() or "unknown"

    return "unknown", "unknown"


async def detect_identity(client: RedfishClient) -> DeviceIdentity:
    """Full detection: vendor, controller, system model, resource IDs.

    Performs 3 Redfish GETs:
    1. /redfish/v1/ → vendor detection
    2. /redfish/v1/Managers/{id} → controller type/version
    3. /redfish/v1/Systems/{id} → system model

    Returns a fully populated DeviceIdentity.
    """
    # Step 1: Service root → vendor
    root = await client.get("/redfish/v1/")
    vendor = await detect_vendor(root)

    # Step 2: Determine resource IDs
    if vendor == "dell":
        manager_id = "iDRAC.Embedded.1"
        system_id = "System.Embedded.1"
        chassis_id = "System.Embedded.1"
    else:
        manager_id = "1"
        system_id = "1"
        chassis_id = "1"

    # Step 3: Manager → controller type/version + firmware
    manager_data = await client.get(f"/redfish/v1/Managers/{manager_id}")
    controller_type, controller_version = await detect_controller(vendor, manager_data)
    firmware_version = manager_data.get("FirmwareVersion", "")

    # Dell: service tag from OEM, HPE: from Manager firmware OEM
    if vendor == "dell":
        service_tag = manager_data.get("Oem", {}).get("Dell", {}).get("ServiceTag", "")
        if not service_tag:
            service_tag = root.get("Oem", {}).get("Dell", {}).get("ServiceTag", "")
        hpe_fw = ""
    else:
        service_tag = ""
        hpe_fw = manager_data.get("Oem", {}).get("Hpe", {}).get("Firmware", {}).get("Current", {}).get("VersionString", "")
        if hpe_fw:
            firmware_version = hpe_fw

    # Step 4: System → model + serial (HPE)
    system_data = await client.get(f"/redfish/v1/Systems/{system_id}")
    model = system_data.get("Model", "")
    if vendor == "hpe" and not service_tag:
        service_tag = system_data.get("SerialNumber", "")

    identity = DeviceIdentity(
        vendor=vendor,
        model=model,
        controller_type=controller_type,
        controller_version=controller_version,
        firmware_version=firmware_version,
        service_tag=service_tag,
        system_id=system_id,
        chassis_id=chassis_id,
        manager_id=manager_id,
    )

    logger.info(
        "Detected: %s %s (%s%s) service_tag=%s fw=%s",
        vendor, model, controller_type, controller_version,
        service_tag, firmware_version,
    )
    return identity


async def auto_detect_bmc(
    verify_ssl: bool = False,
    timeout: int = 3,
) -> tuple[str, int]:
    """Probe known BMC addresses and return the first responsive one.

    Returns (host, port).
    Raises RedfishConnectionError if none respond.
    """
    for host, port in BMC_PROBE_ADDRESSES:
        client = RedfishClient(
            host=f"https://{host}",
            port=port,
            verify_ssl=verify_ssl,
            request_timeout=timeout,
        )
        try:
            # Don't create a session — just probe the service root
            import ssl as _ssl
            import aiohttp

            ssl_ctx = _ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = _ssl.CERT_NONE

            probe_timeout = aiohttp.ClientTimeout(total=timeout)
            async with aiohttp.ClientSession(timeout=probe_timeout) as session:
                async with session.get(f"https://{host}:{port}/redfish/v1/", ssl=ssl_ctx) as resp:
                    if resp.status == 200:
                        logger.info("BMC found at %s:%d", host, port)
                        return host, port
        except Exception:
            continue

    raise RedfishConnectionError("auto-detect", 0, Exception("No BMC found at any probe address"))
