"""Dmesg parser for kernel hardware errors (R3a).

Parses `dmesg` output (or /dev/kmsg) for hardware-related kernel messages:
- Machine Check Exceptions
- PCIe AER errors
- Disk I/O errors and SMART events
- NVMe errors
- Memory errors (ECC, page retirement)
- Driver failures and hardware detection issues

Uses `dmesg --time-format iso` when available, falls back to monotonic timestamps.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from typing import Optional

from harkeniq.os_signals.collector import OSEvent, SignalSource, SignalSourceType

logger = logging.getLogger("harkeniq.os_signals.dmesg")

# Kernel hardware error patterns
_PATTERNS: list[tuple[re.Pattern, str, str, str]] = [
    (re.compile(r"\bmce:.*Bank\s+\d+", re.IGNORECASE), "error", "mce", "cpu"),
    (re.compile(r"Hardware Error", re.IGNORECASE), "error", "mce", "cpu"),
    (re.compile(r"EDAC.*CE.*error", re.IGNORECASE), "warning", "mce", "memory"),
    (re.compile(r"EDAC.*UE.*error", re.IGNORECASE), "error", "mce", "memory"),
    (re.compile(r"pcieport.*AER", re.IGNORECASE), "error", "pcie_aer", "pcie"),
    (re.compile(r"ata\d+.*exception Emask", re.IGNORECASE), "error", "disk_io", "disk"),
    (re.compile(r"sd\s+\d+:\d+:\d+:\d+:.*\[sd[a-z]+\].*error", re.IGNORECASE), "error", "disk_io", "disk"),
    (re.compile(r"I/O error.*dev\s+sd[a-z]+", re.IGNORECASE), "error", "disk_io", "disk"),
    (re.compile(r"nvme\d+.*I/O.*Cmd.*err", re.IGNORECASE), "error", "nvme", "disk"),
    (re.compile(r"page allocation failure", re.IGNORECASE), "error", "mce", "memory"),
    (re.compile(r"Out of memory", re.IGNORECASE), "error", "mce", "memory"),
    (re.compile(r"Corrected error.*DIMM", re.IGNORECASE), "warning", "mce", "memory"),
    (re.compile(r"CPU\d+.*throttled", re.IGNORECASE), "warning", "thermal", "cpu"),
]

# Parse dmesg timestamps: [12345.678901] or ISO format
_TS_MONO = re.compile(r"^\[\s*(\d+\.\d+)\]\s*(.*)")
_TS_ISO = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4})\s*(.*)")


class DmesgSource:
    """Dmesg signal source (R3a).

    Calls `dmesg` subprocess to get kernel ring buffer messages.
    Tracks last-seen timestamp to avoid re-processing old messages.
    """

    source_type = SignalSourceType.DMESG

    def __init__(self, max_lines: int = 500) -> None:
        self._max_lines = max_lines
        self._last_mono_ts: float = 0.0

    def collect(self) -> list[OSEvent]:
        """Run dmesg and extract hardware events since last collection."""
        try:
            result = subprocess.run(
                ["dmesg", "--time-format", "raw"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                # Fallback: try without --time-format
                result = subprocess.run(
                    ["dmesg"],
                    capture_output=True, text=True, timeout=5,
                )
                if result.returncode != 0:
                    return []
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            logger.debug("dmesg unavailable: %s", e)
            return []

        events: list[OSEvent] = []
        for line in result.stdout.splitlines()[-self._max_lines:]:
            mono_ts, message = self._parse_timestamp(line)
            if mono_ts is not None and mono_ts <= self._last_mono_ts:
                continue  # already seen

            event = self._parse_line(message or line, mono_ts)
            if event:
                events.append(event)
                if mono_ts is not None:
                    self._last_mono_ts = mono_ts

        return events

    def reset(self) -> None:
        self._last_mono_ts = 0.0

    def _parse_timestamp(self, line: str) -> tuple[Optional[float], Optional[str]]:
        """Extract monotonic timestamp and remaining message."""
        m = _TS_MONO.match(line)
        if m:
            return float(m.group(1)), m.group(2)
        return None, line

    def _parse_line(self, line: str, mono_ts: Optional[float] = None) -> Optional[OSEvent]:
        """Match a dmesg line against hardware error patterns."""
        for pattern, severity, category, component in _PATTERNS:
            if pattern.search(line):
                device_path = ""
                dev_match = re.search(r"/dev/\w+|sd[a-z]+|nvme\d+", line)
                if dev_match:
                    device_path = dev_match.group()
                    if not device_path.startswith("/dev/"):
                        device_path = f"/dev/{device_path}"

                return OSEvent(
                    source=SignalSourceType.DMESG,
                    timestamp=time.time(),
                    severity=severity,
                    category=category,
                    message=line[:200],
                    raw_line=line,
                    device_path=device_path,
                    component_hint=component,
                    fields={"monotonic_ts": mono_ts} if mono_ts else {},
                )
        return None
