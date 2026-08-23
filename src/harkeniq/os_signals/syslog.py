"""Syslog parser for hardware-related events (R3a).

Parses /var/log/syslog (or /var/log/messages) for hardware error patterns:
- Machine Check Exceptions (MCE)
- PCIe Advanced Error Reporting (AER)
- Disk I/O errors
- NVMe errors
- Memory ECC errors (via edac/mcelog)

Only extracts hardware-relevant lines; ignores application-level messages.
"""

from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

from harkeniq.os_signals.collector import OSEvent, SignalSource, SignalSourceType

logger = logging.getLogger("harkeniq.os_signals.syslog")

# Hardware error patterns with severity and category
_PATTERNS: list[tuple[re.Pattern, str, str, str]] = [
    # (regex, severity, category, component_hint)
    (re.compile(r"mce:\s*(.*)", re.IGNORECASE), "error", "mce", "cpu"),
    (re.compile(r"Machine check events logged", re.IGNORECASE), "error", "mce", "cpu"),
    (re.compile(r"EDAC\s+MC\d+:\s*(.*)", re.IGNORECASE), "error", "mce", "memory"),
    (re.compile(r"pcieport.*AER.*\b(Corrected|Uncorrected|Fatal)\b(.*)", re.IGNORECASE), "error", "pcie_aer", "pcie"),
    (re.compile(r"ata\d+.*hard resetting link", re.IGNORECASE), "error", "disk_io", "disk"),
    (re.compile(r"ata\d+.*COMRESET failed", re.IGNORECASE), "error", "disk_io", "disk"),
    (re.compile(r"sd[a-z]+.*I/O error", re.IGNORECASE), "error", "disk_io", "disk"),
    (re.compile(r"\bsd\s+\d+:\d+:\d+:\d+:.*I/O error", re.IGNORECASE), "error", "disk_io", "disk"),
    (re.compile(r"blk_update_request: I/O error", re.IGNORECASE), "error", "disk_io", "disk"),
    (re.compile(r"nvme\d+:.*I/O error", re.IGNORECASE), "error", "nvme", "disk"),
    (re.compile(r"nvme\d+.*controller is down", re.IGNORECASE), "error", "nvme", "disk"),
    (re.compile(r"EXT4-fs error.*\(device\s+(sd\w+)\)", re.IGNORECASE), "error", "disk_io", "disk"),
    (re.compile(r"XFS.*\(sd\w+\).*I/O error", re.IGNORECASE), "error", "disk_io", "disk"),
    (re.compile(r"thermal.*critical", re.IGNORECASE), "warning", "thermal", "thermal"),
    (re.compile(r"CPU\d+ Package temperature above threshold", re.IGNORECASE), "warning", "thermal", "cpu"),
]

# Syslog timestamp formats
_TS_PATTERN = re.compile(
    r"^(\w{3}\s+\d+\s+\d{2}:\d{2}:\d{2})\s+\S+\s+(.*)"
)

DEFAULT_LOG_PATHS = ["/var/log/syslog", "/var/log/messages"]


class SyslogSource:
    """Syslog signal source (R3a)."""

    source_type = SignalSourceType.SYSLOG

    def __init__(self, log_path: Optional[str] = None, max_lines: int = 1000) -> None:
        self._log_path = log_path or self._find_log()
        self._max_lines = max_lines
        self._last_offset: int = 0
        self._last_inode: int = 0

    def collect(self) -> list[OSEvent]:
        """Read new lines from syslog and extract hardware events."""
        if not self._log_path or not os.path.isfile(self._log_path):
            return []

        events: list[OSEvent] = []
        try:
            stat = os.stat(self._log_path)
            # Detect log rotation (inode changed or file shrunk)
            if stat.st_ino != self._last_inode or stat.st_size < self._last_offset:
                self._last_offset = 0
                self._last_inode = stat.st_ino

            with open(self._log_path, "r", errors="replace") as f:
                f.seek(self._last_offset)
                lines_read = 0
                for line in f:
                    lines_read += 1
                    if lines_read > self._max_lines:
                        break
                    event = self._parse_line(line.rstrip())
                    if event:
                        events.append(event)
                self._last_offset = f.tell()
                self._last_inode = stat.st_ino
        except OSError as e:
            logger.warning("Cannot read syslog %s: %s", self._log_path, e)

        return events

    def reset(self) -> None:
        self._last_offset = 0
        self._last_inode = 0

    def _parse_line(self, line: str) -> Optional[OSEvent]:
        """Match a syslog line against hardware error patterns."""
        for pattern, severity, category, component in _PATTERNS:
            if pattern.search(line):
                # Extract device path if present
                device_path = ""
                dev_match = re.search(r"/dev/\w+|nvme\d+|sd[a-z]+", line)
                if dev_match:
                    device_path = dev_match.group()
                    if not device_path.startswith("/dev/"):
                        device_path = f"/dev/{device_path}"

                pci_match = re.search(r"(\d{4}:[0-9a-f]{2}:[0-9a-f]{2}\.\d)", line, re.IGNORECASE)
                if pci_match and not device_path:
                    device_path = f"pci:{pci_match.group(1)}"

                return OSEvent(
                    source=SignalSourceType.SYSLOG,
                    timestamp=time.time(),
                    severity=severity,
                    category=category,
                    message=line[:200],  # cap message length
                    raw_line=line,
                    device_path=device_path,
                    component_hint=component,
                )
        return None

    def _find_log(self) -> str:
        """Find the syslog file on this system."""
        for path in DEFAULT_LOG_PATHS:
            if os.path.isfile(path):
                return path
        return ""
