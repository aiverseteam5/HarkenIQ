"""IPMIProtocol -- DeviceProtocol implementation for IPMI BMCs (R4-1).

IPMI-over-LAN (RMCP+, UDP port 623) via pyghmi. No ipmitool binary, no
root required (Doc 06 contract). pyghmi is synchronous; every call is
wrapped in asyncio.to_thread so the agent event loop never blocks.

Structure:
  - IPMIBackend: sync seam over the pyghmi session (real: PyghmiBackend).
    Tests inject a fake backend via backend_factory, so no UDP socket or
    OpenIPMI simulator is needed in CI.
  - IPMIProtocol: async DeviceProtocol wrapper + normalization to
    NormalizedDevice, producing exactly the fields the skill engine
    validates (skills/loader.py VALID_FIELDS) with health strings from
    {"OK", "Warning", "Critical", "Unknown"}.

Action semantics vs Redfish:
  - IDENTIFY_LED: chassis identify (blinking chassis LED). IPMI has no
    per-drive LED addressing; the 'target' param is recorded but the
    chassis identify is asserted.
  - SEL_CLEAR: atomic fetch-and-clear via get_event_log(clear=True)
    (pyghmi intentionally has no separate clear call, so no events are
    lost between fetch and clear).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

from harkeniq.protocols.device import ProtocolError
from harkeniq.redfish.normalize import (
    DeviceIdentity,
    NormalizedDevice,
    NormalizedDisk,
    NormalizedFan,
    NormalizedLogEntry,
    NormalizedMemory,
    NormalizedPowerMetrics,
    NormalizedPSU,
    NormalizedThermal,
    compute_health_rollup,
)

logger = logging.getLogger("harkeniq.protocols.ipmi")

# pyghmi.constants.Health: Ok=0, Warning=1, Critical=2, Failed=4 (maskable).
_HEALTH_OK = 0
_HEALTH_WARNING = 1
_HEALTH_CRITICAL = 2
_HEALTH_FAILED = 4


def health_to_str(health: Optional[int], unavailable: bool = False) -> str:
    """Map a pyghmi health int to the canonical health strings.

    Must return exactly "OK" | "Warning" | "Critical" | "Unknown" --
    compute_health_rollup treats any other string as worst-case.
    """
    if unavailable or health is None:
        return "Unknown"
    if health & (_HEALTH_CRITICAL | _HEALTH_FAILED):
        return "Critical"
    if health & _HEALTH_WARNING:
        return "Warning"
    return "OK"


def vendor_from_manufacturer(manufacturer: str) -> str:
    """Map an FRU manufacturer string to the canonical vendor slug."""
    m = (manufacturer or "").lower()
    if "dell" in m:
        return "dell"
    if "hewlett" in m or "hpe" in m:
        return "hpe"
    if not m:
        return "unknown"
    return m.split()[0]


class PyghmiBackend:
    """Real IPMI-over-LAN backend using a pyghmi Command session.

    All methods are synchronous; IPMIProtocol runs them in a thread.
    """

    def __init__(self, host: str, port: int, username: str, password: str) -> None:
        # Lazy import: pyghmi is only required when IPMI is actually used.
        from pyghmi.ipmi.command import Command

        self._cmd = Command(
            bmc=host, userid=username, password=password, port=port
        )

    def get_sensors(self) -> list:
        return list(self._cmd.get_sensor_data())

    def get_inventory(self) -> dict:
        info = self._cmd.get_inventory_of_component("System")
        return dict(info) if info else {}

    def get_event_log(self, clear: bool = False) -> list:
        return list(self._cmd.get_event_log(clear=clear))

    def set_identify(self, on: bool = True, blink: bool = False) -> None:
        self._cmd.set_identify(on=on, blink=blink)

    def close(self) -> None:
        session = getattr(self._cmd, "ipmi_session", None)
        if session is not None:
            try:
                session.logout()
            except Exception:  # pragma: no cover - best-effort logout
                pass


class IPMIProtocol:
    """DeviceProtocol implementation using IPMI-over-LAN via pyghmi."""

    def __init__(
        self,
        host: str,
        port: int = 623,
        backend_factory: Optional[Callable[..., Any]] = None,
        **kwargs,
    ) -> None:
        self._host = host
        self._port = port
        self._backend_factory = backend_factory or PyghmiBackend
        self._backend: Any = None
        self._identity: Optional[DeviceIdentity] = None

    @property
    def name(self) -> str:
        return "ipmi"

    async def connect(self, credentials: dict) -> None:
        """Open an RMCP+ session to the BMC.

        Raises ConnectionError on auth failure, TimeoutError when the
        BMC is unreachable (DeviceProtocol contract).
        """
        try:
            self._backend = await asyncio.to_thread(
                self._backend_factory,
                self._host,
                self._port,
                credentials.get("username", ""),
                credentials.get("password", ""),
            )
        except (TimeoutError, asyncio.TimeoutError):
            raise
        except Exception as e:
            if "timeout" in str(e).lower():
                raise TimeoutError(f"IPMI BMC unreachable: {e}") from e
            raise ConnectionError(f"IPMI connection failed: {e}") from e

    async def disconnect(self) -> None:
        if self._backend is not None:
            backend, self._backend = self._backend, None
            await asyncio.to_thread(backend.close)
        self._identity = None

    async def detect_identity(self) -> DeviceIdentity:
        """Detect vendor/model/serial from FRU inventory."""
        if self._backend is None:
            raise ProtocolError("Not connected")
        try:
            info = await asyncio.to_thread(self._backend.get_inventory)
        except Exception as e:
            raise ProtocolError(f"IPMI FRU inventory failed: {e}") from e
        manufacturer = str(
            info.get("Manufacturer") or info.get("Board manufacturer") or ""
        )
        self._identity = DeviceIdentity(
            vendor=vendor_from_manufacturer(manufacturer),
            model=str(info.get("Product name") or info.get("Model") or "unknown"),
            controller_type="ipmi",
            firmware_version=str(info.get("Hardware Version") or ""),
            service_tag=str(
                info.get("Serial Number") or info.get("Board serial number") or ""
            ),
        )
        return self._identity

    async def poll_sensors(self) -> NormalizedDevice:
        """Poll all IPMI sensors + SEL and normalize to NormalizedDevice."""
        if self._backend is None:
            raise ProtocolError("Not connected")
        if self._identity is None:
            await self.detect_identity()

        try:
            readings = await asyncio.to_thread(self._backend.get_sensors)
        except Exception as e:
            raise ProtocolError(f"IPMI sensor read failed: {e}") from e

        device = NormalizedDevice(identity=self._identity)
        power_watts: Optional[int] = None

        for reading in readings:
            rtype = (getattr(reading, "type", "") or "").lower()
            name = getattr(reading, "name", "") or ""
            value = getattr(reading, "value", None)
            units = (getattr(reading, "units", "") or "").strip()
            unavailable = bool(getattr(reading, "unavailable", 0))
            health = health_to_str(getattr(reading, "health", None), unavailable)
            states = [str(s) for s in (getattr(reading, "states", None) or [])]

            if rtype == "fan":
                device.fans.append(_normalize_fan(name, value, units, health))
            elif rtype == "temperature":
                device.thermals.append(_normalize_thermal(name, value, health))
            elif rtype == "power supply":
                device.psus.append(_normalize_psu(name, health, states, unavailable))
            elif rtype == "memory":
                device.memory.append(_normalize_memory(name, health, states, unavailable))
            elif rtype in ("drive bay", "drive slot", "drive slot / bay"):
                device.disks.append(_normalize_disk(name, health, states))
            elif units.lower() in ("w", "watts") and value is not None:
                # System power meter (e.g. "Pwr Consumption")
                if power_watts is None or "consumption" in name.lower():
                    power_watts = int(value)

        if power_watts is not None:
            device.power_metrics = NormalizedPowerMetrics(
                system_power_watts=power_watts
            )

        # SEL -> normalized log entries (read-only fetch, never clears)
        try:
            events = await asyncio.to_thread(self._backend.get_event_log)
            device.log_entries = [_normalize_sel_event(ev) for ev in events]
        except Exception as e:
            logger.warning("IPMI SEL read failed: %s", e)

        device.health_rollup = compute_health_rollup(device)
        return device

    async def execute_action(self, action_type: str, params: dict) -> dict:
        """Execute an IPMI action. Supported: IDENTIFY_LED, SEL_CLEAR."""
        if self._backend is None:
            raise ProtocolError("Not connected")
        import time

        started = time.monotonic()
        try:
            if action_type == "IDENTIFY_LED":
                await asyncio.to_thread(
                    self._backend.set_identify, True, True
                )
            elif action_type == "SEL_CLEAR":
                await asyncio.to_thread(self._backend.get_event_log, True)
            else:
                return {
                    "success": False,
                    "error": f"Action {action_type} not supported by ipmi protocol",
                    "duration_ms": 0.0,
                }
        except Exception as e:
            return {
                "success": False,
                "error": f"{action_type} failed: {e}",
                "duration_ms": (time.monotonic() - started) * 1000.0,
            }
        return {
            "success": True,
            "error": "",
            "duration_ms": (time.monotonic() - started) * 1000.0,
        }


# -- normalization helpers ---------------------------------------------------


def _normalize_fan(name: str, value, units: str, health: str) -> NormalizedFan:
    speed_rpm = None
    speed_pct = None
    if value is not None:
        if units.upper() == "RPM":
            speed_rpm = int(value)
        elif units == "%":
            speed_pct = int(value)
    return NormalizedFan(
        name=name,
        speed_rpm=speed_rpm,
        speed_pct=speed_pct,
        health=health,
        state="Enabled" if health != "Unknown" else "Absent",
    )


def _normalize_thermal(name: str, value, health: str) -> NormalizedThermal:
    context = "CPU" if "cpu" in name.lower() else (
        "Intake" if "inlet" in name.lower() or "ambient" in name.lower() else
        "Exhaust" if "exhaust" in name.lower() or "outlet" in name.lower() else ""
    )
    return NormalizedThermal(
        name=name,
        reading_c=float(value) if value is not None else None,
        health=health,
        context=context,
    )


def _normalize_psu(
    name: str, health: str, states: list[str], unavailable: bool
) -> NormalizedPSU:
    absent = unavailable or any("absent" in s.lower() for s in states)
    failed = any("failure" in s.lower() or "failed" in s.lower() for s in states)
    return NormalizedPSU(
        name=name,
        member_id=name,
        health="Critical" if failed else health,
        state="Absent" if absent else "Enabled",
    )


def _normalize_memory(
    name: str, health: str, states: list[str], unavailable: bool
) -> NormalizedMemory:
    lowered = [s.lower() for s in states]
    correctable = any("correctable ecc" in s and "un" not in s for s in lowered)
    uncorrectable = any("uncorrectable ecc" in s for s in lowered)
    absent = unavailable or any("absent" in s for s in lowered)
    return NormalizedMemory(
        name=name,
        health="Critical" if uncorrectable else health,
        state="Absent" if absent else "Enabled",
        alarm_ecc_correctable=correctable,
        alarm_ecc_uncorrectable=uncorrectable,
    )


def _normalize_disk(name: str, health: str, states: list[str]) -> NormalizedDisk:
    lowered = [s.lower() for s in states]
    fault = any("fault" in s for s in lowered)
    predictive = any("predictive" in s for s in lowered)
    return NormalizedDisk(
        name=name,
        health="Critical" if fault else ("Warning" if predictive else health),
        smart_alert=predictive or fault,
    )


_SEL_SEVERITY = {
    _HEALTH_OK: "OK",
    _HEALTH_WARNING: "Warning",
    _HEALTH_CRITICAL: "Critical",
    _HEALTH_FAILED: "Critical",
}


def _normalize_sel_event(event: dict) -> NormalizedLogEntry:
    severity_raw = event.get("severity")
    if isinstance(severity_raw, int):
        severity = "Critical" if severity_raw & (_HEALTH_CRITICAL | _HEALTH_FAILED) \
            else "Warning" if severity_raw & _HEALTH_WARNING else "OK"
    else:
        severity = str(severity_raw or "OK")
    return NormalizedLogEntry(
        id=str(event.get("record_id", "")),
        timestamp=str(event.get("timestamp", "")),
        severity=severity,
        message=str(event.get("event", "")),
        component_id=str(event.get("component", "")) or None,
        category=str(event.get("component_type", "")),
    )
