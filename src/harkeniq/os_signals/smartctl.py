"""Smartctl signal source (R3b-1 C3).

Runs `smartctl -a /dev/sdX` to read SMART attributes at higher polling
frequency than Redfish.  Parses key predictive failure indicators:
reallocated sectors, pending sectors, offline uncorrectable, wear leveling.

Requires smartmontools; gracefully returns empty if unavailable.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

from harkeniq.os_signals.collector import OSEvent, SignalSource, SignalSourceType

logger = logging.getLogger("harkeniq.os_signals.smartctl")

# SMART attributes that indicate hardware degradation
_CRITICAL_ATTRS = {
    "5": "Reallocated_Sector_Ct",
    "187": "Reported_Uncorrect",
    "188": "Command_Timeout",
    "196": "Reallocated_Event_Count",
    "197": "Current_Pending_Sector",
    "198": "Offline_Uncorrectable",
    "199": "UDMA_CRC_Error_Count",
}

# NVMe critical warnings
_NVME_WARNINGS = {
    "critical_warning": "NVMe critical warning flags",
    "media_errors": "NVMe media and data integrity errors",
    "percentage_used": "NVMe wear percentage",
}


class SmartctlSource:
    """SMART attribute signal source.

    Scans block devices and runs smartctl to read SMART health.
    Only reports events when concerning attributes are non-zero.
    """

    source_type = SignalSourceType.SMARTCTL

    def __init__(self, devices: list[str] | None = None) -> None:
        self._devices = devices  # None = auto-discover
        self._available: Optional[bool] = None
        self._last_values: dict[str, dict[str, int]] = {}  # device -> {attr_id: value}

    def collect(self) -> list[OSEvent]:
        if self._available is False:
            return []
        if self._available is None:
            self._available = self._check_available()
            if not self._available:
                return []

        devices = self._devices or self._discover_devices()
        events: list[OSEvent] = []

        for device in devices:
            try:
                new_events = self._check_device(device)
                events.extend(new_events)
            except Exception as e:
                logger.debug("smartctl failed for %s: %s", device, e)

        return events

    def reset(self) -> None:
        self._last_values.clear()

    def _check_device(self, device: str) -> list[OSEvent]:
        """Run smartctl on one device and extract concerning attributes."""
        try:
            result = subprocess.run(
                ["smartctl", "-A", "-H", device],
                capture_output=True, text=True, timeout=10,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return []

        events: list[OSEvent] = []
        output = result.stdout

        # Check overall health
        if "SMART overall-health self-assessment test result: FAILED" in output:
            events.append(OSEvent(
                source=SignalSourceType.SMARTCTL,
                timestamp=time.time(),
                severity="error",
                category="disk_io",
                message=f"SMART health FAILED on {device}",
                raw_line="SMART overall-health: FAILED",
                device_path=device,
                component_hint="disk",
                fields={"smart_health": "FAILED"},
            ))

        # Parse SMART attributes (ATA drives)
        prev = self._last_values.get(device, {})
        current: dict[str, int] = {}

        for line in output.splitlines():
            # Format: ID# ATTRIBUTE_NAME  FLAG  VALUE WORST THRESH TYPE  UPDATED  WHEN_FAILED RAW_VALUE
            match = re.match(
                r"\s*(\d+)\s+\S+\s+\S+\s+\d+\s+\d+\s+\d+\s+\S+\s+\S+\s+\S+\s+(\d+)",
                line,
            )
            if match:
                attr_id = match.group(1)
                raw_value = int(match.group(2))
                if attr_id in _CRITICAL_ATTRS and raw_value > 0:
                    current[attr_id] = raw_value
                    # Only report if value changed (avoid repeated alerts)
                    if prev.get(attr_id) != raw_value:
                        attr_name = _CRITICAL_ATTRS[attr_id]
                        events.append(OSEvent(
                            source=SignalSourceType.SMARTCTL,
                            timestamp=time.time(),
                            severity="warning",
                            category="disk_io",
                            message=f"{device}: {attr_name} = {raw_value}",
                            raw_line=line.strip(),
                            device_path=device,
                            component_hint="disk",
                            fields={"attr_id": attr_id, "attr_name": attr_name,
                                    "raw_value": raw_value},
                        ))

        # NVMe: check for critical warnings
        for key, desc in _NVME_WARNINGS.items():
            match = re.search(rf"{key}[:\s]+(\d+)", output, re.IGNORECASE)
            if match:
                val = int(match.group(1))
                if val > 0 and (key == "critical_warning" or key == "media_errors"):
                    events.append(OSEvent(
                        source=SignalSourceType.SMARTCTL,
                        timestamp=time.time(),
                        severity="warning" if key != "critical_warning" else "error",
                        category="nvme",
                        message=f"{device}: {desc} = {val}",
                        raw_line=f"{key}: {val}",
                        device_path=device,
                        component_hint="disk",
                        fields={key: val},
                    ))

        self._last_values[device] = current
        return events

    def _discover_devices(self) -> list[str]:
        """Find block devices that support SMART."""
        devices = []
        sys_block = Path("/sys/block")
        if sys_block.is_dir():
            for dev in sys_block.iterdir():
                name = dev.name
                if name.startswith(("sd", "nvme")) and not name[-1].isdigit():
                    # nvme0n1 is fine, nvme0n1p1 is a partition
                    if name.startswith("nvme") and "p" in name:
                        continue
                    devices.append(f"/dev/{name}")
        return sorted(devices)

    def _check_available(self) -> bool:
        try:
            result = subprocess.run(
                ["smartctl", "--version"],
                capture_output=True, timeout=3,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
