"""Redfish polling orchestrator (Doc 10 §2.2).

Coordinates: detect vendor → construct paths → GET endpoints → normalize.
Produces a NormalizedDevice on each poll cycle.
"""

from __future__ import annotations

import logging
from typing import Optional

from harkeniq.redfish.client import RedfishClient
from harkeniq.redfish import dell as dell_norm
from harkeniq.redfish import hpe as hpe_norm
from harkeniq.redfish.discovery import RedfishPaths, detect_identity
from harkeniq.redfish.normalize import (
    DeviceIdentity,
    NormalizedDevice,
    NormalizedLogEntry,
    compute_health_rollup,
)

logger = logging.getLogger("harkeniq.poller")


class Poller:
    """Orchestrates Redfish polling and normalization for a single BMC."""

    def __init__(self, client: RedfishClient):
        self.client = client
        self.identity: Optional[DeviceIdentity] = None
        self.paths: Optional[RedfishPaths] = None

    async def detect(self) -> DeviceIdentity:
        """Run vendor detection. Must be called before poll_sensors()."""
        self.identity = await detect_identity(self.client)
        self.paths = RedfishPaths.from_identity(self.identity)
        return self.identity

    async def poll_sensors(self) -> NormalizedDevice:
        """Single poll cycle: GET all sensor endpoints → normalize → return.

        Returns a fully populated NormalizedDevice with health rollup.
        """
        if not self.identity or not self.paths:
            await self.detect()

        vendor = self.identity.vendor
        device = NormalizedDevice(identity=self.identity)

        # Thermal (fans + temperatures)
        thermal_data = await self.client.get(self.paths.thermal)
        if vendor == "dell":
            device.fans = dell_norm.normalize_fans(thermal_data)
            device.thermals = dell_norm.normalize_thermals(thermal_data)
        else:
            device.fans = hpe_norm.normalize_fans(thermal_data)
            device.thermals = hpe_norm.normalize_thermals(thermal_data)

        # Power (PSUs + power metrics)
        power_data = await self.client.get(self.paths.power)
        if vendor == "dell":
            device.psus, device.power_metrics = dell_norm.normalize_psus(power_data)
        else:
            device.psus, device.power_metrics = hpe_norm.normalize_psus(power_data)

        # Storage (drives)
        device.disks = await self._poll_drives(vendor)

        # Memory (DIMMs + metrics)
        device.memory = await self._poll_memory(vendor)

        # Health rollup
        device.health_rollup = compute_health_rollup(device)

        logger.info(
            "Poll complete: %s — fan=%s disk=%s mem=%s psu=%s thermal=%s",
            self.identity.model,
            device.health_rollup.fan,
            device.health_rollup.disk,
            device.health_rollup.memory,
            device.health_rollup.psu,
            device.health_rollup.thermal,
        )
        return device

    async def poll_logs(self) -> list[NormalizedLogEntry]:
        """Poll event logs and return normalized entries."""
        if not self.identity or not self.paths:
            await self.detect()

        vendor = self.identity.vendor
        try:
            log_data = await self.client.get(self.paths.sel_entries)
        except Exception as e:
            logger.warning("Failed to poll logs: %s", e)
            return []

        if vendor == "dell":
            return dell_norm.normalize_log_entries(log_data)
        else:
            return hpe_norm.normalize_log_entries(log_data)

    async def _poll_drives(self, vendor: str) -> list:
        """Poll all drives from storage controllers."""
        from harkeniq.redfish.normalize import NormalizedDisk

        drives: list[NormalizedDisk] = []

        try:
            storage_data = await self.client.get(self.paths.storage)
        except Exception as e:
            logger.warning("Failed to poll storage: %s", e)
            return drives

        # Iterate storage controller members and their drives
        for member in storage_data.get("Members", []):
            controller_uri = member.get("@odata.id", "")
            if not controller_uri:
                continue

            try:
                controller_data = await self.client.get(controller_uri)
            except Exception:
                continue

            # Get drive links from controller
            drive_links = controller_data.get("Drives", [])
            if not drive_links:
                # Try Drives@odata.count pattern or embedded drives
                drive_links = controller_data.get("Drives@odata.navigationLink", [])

            for drive_link in drive_links:
                drive_uri = drive_link.get("@odata.id", "") if isinstance(drive_link, dict) else ""
                if not drive_uri:
                    continue
                try:
                    drive_data = await self.client.get(drive_uri)
                    if vendor == "dell":
                        drives.append(dell_norm.normalize_disk(drive_data))
                    else:
                        drives.append(hpe_norm.normalize_disk(drive_data))
                except Exception as e:
                    logger.warning("Failed to poll drive %s: %s", drive_uri, e)

        # For HPE iLO5: also check SmartStorage
        if vendor == "hpe" and self.paths.smartstorage:
            smartstorage_drives = await self._poll_smartstorage_drives()
            drives = hpe_norm.merge_standard_and_smartstorage(drives, smartstorage_drives)

        return drives

    async def _poll_smartstorage_drives(self) -> list:
        """Poll HPE SmartStorage drives (iLO5 only)."""
        from harkeniq.redfish.normalize import NormalizedDisk

        drives: list[NormalizedDisk] = []
        try:
            controllers = await self.client.get(self.paths.smartstorage)
            for member in controllers.get("Members", []):
                ctrl_uri = member.get("@odata.id", "")
                if not ctrl_uri:
                    continue
                drives_uri = f"{ctrl_uri}/DiskDrives"
                try:
                    drives_data = await self.client.get(drives_uri)
                    for drive in drives_data.get("Members", []):
                        drives.append(hpe_norm.normalize_disk_smartstorage(drive))
                except Exception as e:
                    logger.warning("Failed to poll SmartStorage drives: %s", e)
        except Exception as e:
            logger.warning("Failed to poll SmartStorage: %s", e)
        return drives

    async def _poll_memory(self, vendor: str) -> list:
        """Poll all DIMMs and their metrics."""
        from harkeniq.redfish.normalize import NormalizedMemory

        dimms: list[NormalizedMemory] = []

        try:
            mem_data = await self.client.get(self.paths.memory)
        except Exception as e:
            logger.warning("Failed to poll memory: %s", e)
            return dimms

        for member in mem_data.get("Members", []):
            dimm_uri = member.get("@odata.id", "")
            if not dimm_uri:
                continue
            try:
                dimm_data = await self.client.get(dimm_uri)
                # Try to get MemoryMetrics
                metrics_data = None
                try:
                    metrics_data = await self.client.get(f"{dimm_uri}/MemoryMetrics")
                except Exception:
                    pass  # MemoryMetrics may not exist

                if vendor == "dell":
                    dimms.append(dell_norm.normalize_memory(dimm_data, metrics_data))
                else:
                    dimms.append(hpe_norm.normalize_memory(dimm_data, metrics_data))
            except Exception as e:
                logger.warning("Failed to poll DIMM %s: %s", dimm_uri, e)

        return dimms
