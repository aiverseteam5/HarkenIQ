"""Hardware-to-OS device mapping (spec A2.7 contract 5, doc 04 Cap 8a).

Maps Redfish hardware identifiers (drive serial, NIC MAC) to OS block
devices (/dev/sdX, /dev/nvmeXnY) by matching serial numbers.  This is
the bridge between "Redfish says drive in Bay 2 is failing" and
"that's /dev/sdb, mounted as /data".

R3a: serial-based mapping for block devices.
R3b: full process->service mapping (doc 04 Cap 8b).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("harkeniq.os_signals.device_map")


@dataclass
class DeviceMapping:
    """A mapping between a Redfish component and an OS device."""

    redfish_id: str        # e.g., "Disk.Bay.2:Enclosure.Internal.0-1"
    serial_number: str     # from Redfish drive inventory
    os_device: str         # e.g., "/dev/sdb"
    mount_point: str = ""  # e.g., "/data" (if mounted)
    filesystem: str = ""   # e.g., "ext4"
    confidence: float = 1.0


class HardwareDeviceMapper:
    """Maps Redfish hardware identifiers to OS devices.

    Uses /sys/block/*/device/serial and lsblk to build the mapping.
    """

    def __init__(self) -> None:
        self._cache: dict[str, DeviceMapping] = {}
        self._cache_ts: float = 0.0
        self._cache_ttl: float = 300.0  # 5 minute cache

    def map_drive(
        self, redfish_id: str, serial_number: str
    ) -> Optional[DeviceMapping]:
        """Find the OS block device matching a Redfish drive serial number.

        Searches /sys/block/*/device/ for a matching serial.
        """
        if not serial_number:
            return None

        # Check cache
        cache_key = serial_number.strip().lower()
        import time
        now = time.time()
        if cache_key in self._cache and (now - self._cache_ts) < self._cache_ttl:
            return self._cache[cache_key]

        # Scan /sys/block for matching serial
        mapping = self._scan_block_devices(redfish_id, serial_number)
        if mapping:
            self._cache[cache_key] = mapping
            self._cache_ts = now
        return mapping

    def get_all_mappings(
        self, redfish_drives: list[dict]
    ) -> list[DeviceMapping]:
        """Map all Redfish drives to OS devices.

        Args:
            redfish_drives: List of dicts with 'id' and 'serial_number' keys
                           (from Redfish Storage/Drives).
        """
        mappings = []
        for drive in redfish_drives:
            mapping = self.map_drive(
                drive.get("id", ""),
                drive.get("serial_number", ""),
            )
            if mapping:
                mappings.append(mapping)
        return mappings

    def _scan_block_devices(
        self, redfish_id: str, serial: str
    ) -> Optional[DeviceMapping]:
        """Scan /sys/block for a device matching the given serial number."""
        serial_clean = serial.strip().lower()
        sys_block = Path("/sys/block")
        if not sys_block.is_dir():
            return None

        for dev_dir in sys_block.iterdir():
            name = dev_dir.name
            # Skip virtual devices (loop, ram, dm-)
            if name.startswith(("loop", "ram", "dm-")):
                continue

            # Check serial in various locations
            for serial_path in [
                dev_dir / "device" / "serial",
                dev_dir / "device" / "wwid",
                dev_dir / "serial",
            ]:
                try:
                    if serial_path.is_file():
                        found = serial_path.read_text().strip().lower()
                        if serial_clean in found or found in serial_clean:
                            os_device = f"/dev/{name}"
                            mount, fs = self._get_mount_info(os_device)
                            return DeviceMapping(
                                redfish_id=redfish_id,
                                serial_number=serial,
                                os_device=os_device,
                                mount_point=mount,
                                filesystem=fs,
                            )
                except OSError:
                    continue

        return None

    def _get_mount_info(self, device: str) -> tuple[str, str]:
        """Get mount point and filesystem for a block device from /proc/mounts."""
        try:
            with open("/proc/mounts") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 3 and parts[0] == device:
                        return parts[1], parts[2]  # mount_point, fs_type
        except OSError:
            pass
        return "", ""
