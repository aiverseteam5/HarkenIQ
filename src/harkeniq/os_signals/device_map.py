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
from dataclasses import field
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

    def get_full_mapping(
        self, redfish_id: str, serial_number: str
    ) -> Optional["FullDeviceMapping"]:
        """Full hardware-to-application mapping (R3b-1 C4, doc 04 Cap 8b).

        Maps: Redfish drive -> /dev/sdX -> mount point -> processes -> services.
        This is the "wow demo": "Drive in Bay 2 is failing. It is /dev/sdb,
        mounted as /data/postgres. Your PostgreSQL database will lose its
        primary storage within approximately 72 hours."
        """
        base = self.map_drive(redfish_id, serial_number)
        if base is None:
            return None

        processes = []
        if base.mount_point:
            processes = self._find_processes_using_mount(base.mount_point)

        services = []
        for proc in processes:
            svc = self._get_service_for_pid(proc["pid"])
            if svc and svc not in services:
                services.append(svc)

        return FullDeviceMapping(
            redfish_id=base.redfish_id,
            serial_number=base.serial_number,
            os_device=base.os_device,
            mount_point=base.mount_point,
            filesystem=base.filesystem,
            processes=processes,
            services=services,
        )

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

    def _find_processes_using_mount(self, mount_point: str) -> list[dict]:
        """Find processes with open files on the given mount point.

        Scans /proc/[pid]/fd/ for symlinks pointing into the mount.
        """
        import os
        results = []
        proc = Path("/proc")
        if not proc.is_dir():
            return results

        for pid_dir in proc.iterdir():
            if not pid_dir.name.isdigit():
                continue
            pid = int(pid_dir.name)
            try:
                fd_dir = pid_dir / "fd"
                if not fd_dir.is_dir():
                    continue
                uses_mount = False
                for fd in fd_dir.iterdir():
                    try:
                        target = os.readlink(str(fd))
                        if target.startswith(mount_point):
                            uses_mount = True
                            break
                    except OSError:
                        continue
                if uses_mount:
                    comm = ""
                    try:
                        comm = (pid_dir / "comm").read_text().strip()
                    except OSError:
                        pass
                    results.append({"pid": pid, "comm": comm})
            except (OSError, PermissionError):
                continue

        return results

    def _get_service_for_pid(self, pid: int) -> str:
        """Get the systemd service name for a PID.

        Reads /proc/[pid]/cgroup to find the systemd slice/service.
        """
        cgroup_path = Path(f"/proc/{pid}/cgroup")
        try:
            for line in cgroup_path.read_text().splitlines():
                # Format: hierarchy-ID:controller-list:cgroup-path
                parts = line.split(":", 2)
                if len(parts) >= 3:
                    cgroup = parts[2]
                    # Extract service name from cgroup path
                    # e.g., /system.slice/postgresql.service -> postgresql.service
                    if ".service" in cgroup:
                        for segment in cgroup.split("/"):
                            if segment.endswith(".service"):
                                return segment
        except OSError:
            pass
        return ""


@dataclass
class FullDeviceMapping:
    """Complete hardware-to-application mapping (R3b-1 C4).

    Extends DeviceMapping with process and service information.
    """

    redfish_id: str
    serial_number: str
    os_device: str
    mount_point: str = ""
    filesystem: str = ""
    processes: list[dict] = field(default_factory=list)  # [{pid, comm}]
    services: list[str] = field(default_factory=list)     # ["postgresql.service"]
    confidence: float = 1.0

    def impact_summary(self) -> str:
        """Human-readable impact statement for the diagnosis."""
        parts = [f"Hardware: {self.redfish_id} (serial {self.serial_number})"]
        parts.append(f"OS device: {self.os_device}")
        if self.mount_point:
            parts.append(f"Mount: {self.mount_point} ({self.filesystem})")
        if self.services:
            parts.append(f"Affected services: {', '.join(self.services)}")
        elif self.processes:
            procs = ", ".join(f"{p['comm']}(pid {p['pid']})" for p in self.processes[:5])
            parts.append(f"Affected processes: {procs}")
        return " | ".join(parts)
