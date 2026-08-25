"""Compatibility shim (R6-P1): the canonical device model moved.

The normalized dataclasses defined here through R5 are protocol-neutral
(Redfish, IPMI, and gNMI all produce them), so they now live in
``harkeniq.protocols.model``. This module re-exports every public name so
existing imports keep working; new code should import from
``harkeniq.protocols.model`` directly.
"""

from __future__ import annotations

from harkeniq.protocols.model import (
    DeviceIdentity,
    HealthRollup,
    NormalizedDevice,
    NormalizedDisk,
    NormalizedFan,
    NormalizedInterface,
    NormalizedLogEntry,
    NormalizedMemory,
    NormalizedPSU,
    NormalizedPowerMetrics,
    NormalizedThermal,
    compute_health_rollup,
    worst_health,
)

__all__ = [
    "DeviceIdentity",
    "HealthRollup",
    "NormalizedDevice",
    "NormalizedDisk",
    "NormalizedFan",
    "NormalizedInterface",
    "NormalizedLogEntry",
    "NormalizedMemory",
    "NormalizedPSU",
    "NormalizedPowerMetrics",
    "NormalizedThermal",
    "compute_health_rollup",
    "worst_health",
]
