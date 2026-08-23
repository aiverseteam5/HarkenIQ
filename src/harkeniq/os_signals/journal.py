"""Systemd journal signal source (R3b-1 C3).

Reads hardware-related messages from the systemd journal via
`journalctl`. Uses the same hardware error patterns as syslog but
with structured journal fields for better device identification.

Requires systemd; gracefully returns empty on non-systemd systems.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from typing import Optional

from harkeniq.os_signals.collector import OSEvent, SignalSource, SignalSourceType

logger = logging.getLogger("harkeniq.os_signals.journal")

# Same patterns as syslog, reused
_PATTERNS: list[tuple[re.Pattern, str, str, str]] = [
    (re.compile(r"mce:.*Bank\s+\d+", re.IGNORECASE), "error", "mce", "cpu"),
    (re.compile(r"Hardware Error", re.IGNORECASE), "error", "mce", "cpu"),
    (re.compile(r"EDAC.*error", re.IGNORECASE), "error", "mce", "memory"),
    (re.compile(r"pcieport.*AER", re.IGNORECASE), "error", "pcie_aer", "pcie"),
    (re.compile(r"I/O error.*dev\s+\w+", re.IGNORECASE), "error", "disk_io", "disk"),
    (re.compile(r"nvme\d+.*error", re.IGNORECASE), "error", "nvme", "disk"),
    (re.compile(r"thermal.*critical", re.IGNORECASE), "warning", "thermal", "thermal"),
    (re.compile(r"CPU\d+.*temperature above threshold", re.IGNORECASE), "warning", "thermal", "cpu"),
]


class JournalSource:
    """Systemd journal signal source.

    Uses `journalctl --since` to read only new entries since last collection.
    """

    source_type = SignalSourceType.JOURNAL

    def __init__(self, max_entries: int = 500) -> None:
        self._max_entries = max_entries
        self._last_cursor: str = ""
        self._available: Optional[bool] = None

    def collect(self) -> list[OSEvent]:
        if self._available is False:
            return []
        if self._available is None:
            self._available = self._check_available()
            if not self._available:
                return []

        try:
            cmd = ["journalctl", "--output=short", "-n", str(self._max_entries),
                   "-k", "--no-pager"]  # -k = kernel messages only
            if self._last_cursor:
                cmd.extend(["--after-cursor", self._last_cursor])

            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                return []
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            self._available = False
            return []

        events: list[OSEvent] = []
        lines = result.stdout.strip().splitlines()
        for line in lines:
            event = self._parse_line(line)
            if event:
                events.append(event)

        # Track cursor for incremental reading
        if lines:
            try:
                cursor_result = subprocess.run(
                    ["journalctl", "--output=export", "-n", "1", "-k", "--no-pager"],
                    capture_output=True, text=True, timeout=3,
                )
                for cline in cursor_result.stdout.splitlines():
                    if cline.startswith("__CURSOR="):
                        self._last_cursor = cline.split("=", 1)[1]
                        break
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass

        return events

    def reset(self) -> None:
        self._last_cursor = ""

    def _parse_line(self, line: str) -> Optional[OSEvent]:
        for pattern, severity, category, component in _PATTERNS:
            if pattern.search(line):
                device_path = ""
                dev_match = re.search(r"/dev/\w+|sd[a-z]+|nvme\d+", line)
                if dev_match:
                    device_path = dev_match.group()
                    if not device_path.startswith("/dev/"):
                        device_path = f"/dev/{device_path}"
                return OSEvent(
                    source=SignalSourceType.JOURNAL,
                    timestamp=time.time(),
                    severity=severity,
                    category=category,
                    message=line[:200],
                    raw_line=line,
                    device_path=device_path,
                    component_hint=component,
                )
        return None

    def _check_available(self) -> bool:
        try:
            result = subprocess.run(
                ["journalctl", "--version"],
                capture_output=True, timeout=3,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
