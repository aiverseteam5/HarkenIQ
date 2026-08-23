"""Pluggable OS signal collector abstraction (spec A2.7 contract 5).

OSSignalCollector manages registered signal sources (syslog, dmesg, etc.)
and collects events from all active sources.  Each source is a plugin
that implements the SignalSource protocol.

R3a: syslog + dmesg sources.
R3b/R4: journal, smartctl, /proc, application mapping.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Protocol

logger = logging.getLogger("harkeniq.os_signals")


class SignalSourceType(str, Enum):
    SYSLOG = "syslog"
    DMESG = "dmesg"
    JOURNAL = "journal"      # R3b
    SMARTCTL = "smartctl"    # R3b
    PROC = "proc"            # R3b


@dataclass
class OSEvent:
    """A single hardware-related OS event."""

    source: SignalSourceType
    timestamp: float
    severity: str            # "error" | "warning" | "info"
    category: str            # "mce" | "pcie_aer" | "disk_io" | "nvme" | "thermal" | "general"
    message: str
    raw_line: str            # original log line
    device_path: str = ""    # e.g., "/dev/sda", "pci:0000:3b:00.0"
    component_hint: str = "" # e.g., "memory", "disk", "network"
    fields: dict[str, Any] = field(default_factory=dict)


class SignalSource(Protocol):
    """Protocol for pluggable signal sources."""

    source_type: SignalSourceType

    def collect(self) -> list[OSEvent]:
        """Collect new events since last call."""
        ...

    def reset(self) -> None:
        """Reset internal cursor/state."""
        ...


class OSSignalCollector:
    """Manages signal sources and collects events from all of them.

    Usage:
        collector = OSSignalCollector()
        collector.register(SyslogSource())
        collector.register(DmesgSource())
        events = collector.collect_all()
    """

    def __init__(self) -> None:
        self._sources: dict[SignalSourceType, SignalSource] = {}

    def register(self, source: SignalSource) -> None:
        """Register a signal source plugin."""
        self._sources[source.source_type] = source
        logger.info("Registered OS signal source: %s", source.source_type.value)

    def collect_all(self) -> list[OSEvent]:
        """Collect events from all registered sources."""
        events: list[OSEvent] = []
        for source_type, source in self._sources.items():
            try:
                new_events = source.collect()
                events.extend(new_events)
            except Exception as e:
                logger.warning(
                    "Failed to collect from %s: %s", source_type.value, e
                )
        return sorted(events, key=lambda e: e.timestamp)

    @property
    def active_sources(self) -> list[str]:
        return [s.value for s in self._sources]
