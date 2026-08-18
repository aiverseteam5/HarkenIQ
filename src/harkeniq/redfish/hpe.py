"""HPE iLO Redfish normalization (Doc 08 §4, HPE column).

Transforms raw HPE Redfish JSON into normalized dataclasses.
Handles iLO5 SmartStorage compatibility for RAID-attached drives.
"""

from __future__ import annotations

from typing import Any, Optional

from harkeniq.redfish.normalize import (
    NormalizedDisk,
    NormalizedFan,
    NormalizedLogEntry,
    NormalizedMemory,
    NormalizedPowerMetrics,
    NormalizedPSU,
    NormalizedThermal,
)


def _get(data: dict, *keys: str, default: Any = None) -> Any:
    """Safely traverse nested dict keys."""
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key, default)
        if current is default:
            return default
    return current


# ---------------------------------------------------------------------------
# Fan normalization (Doc 08 §4.1, HPE column)
# ---------------------------------------------------------------------------


def normalize_fans(thermal_json: dict) -> list[NormalizedFan]:
    """Normalize HPE fan data from /Chassis/1/Thermal response."""
    fans = thermal_json.get("Fans", [])
    redundancy = thermal_json.get("Redundancy", [])
    redundancy_health = _get(redundancy[0], "Status", "Health", default="Unknown") if redundancy else "Unknown"

    result = []
    for fan in fans:
        # HPE may use FanName instead of Name
        name = fan.get("FanName", fan.get("Name", ""))
        result.append(NormalizedFan(
            name=name,
            speed_rpm=fan.get("Reading"),
            speed_pct=None,  # HPE does not expose fan duty cycle
            health=_get(fan, "Status", "Health", default="Unknown"),
            state=_get(fan, "Status", "State", default="Unknown"),
            threshold_low_critical=fan.get("LowerThresholdCritical"),
            redundancy_health=redundancy_health,
            location=fan.get("PhysicalContext", ""),
            oem_data={
                k: v for k, v in {
                    "location_detail": _get(fan, "Oem", "Hpe", "Location"),
                    "hot_pluggable": _get(fan, "Oem", "Hpe", "HotPluggable"),
                }.items() if v is not None
            },
        ))
    return result


# ---------------------------------------------------------------------------
# Disk normalization — standard Redfish path (Doc 08 §4.2, HPE column)
# ---------------------------------------------------------------------------


def normalize_disk(drive_json: dict) -> NormalizedDisk:
    """Normalize a single HPE drive from /Storage/{cid}/Drives/{did} response."""
    hpe_oem = _get(drive_json, "Oem", "Hpe", default={})

    return NormalizedDisk(
        name=drive_json.get("Model", drive_json.get("Name", "")),
        serial=drive_json.get("SerialNumber", ""),
        media_type=drive_json.get("MediaType", ""),
        protocol=drive_json.get("Protocol", ""),
        capacity_bytes=drive_json.get("CapacityBytes"),
        health=_get(drive_json, "Status", "Health", default="Unknown"),
        life_left_pct=drive_json.get("PredictedMediaLifeLeftPercent"),
        smart_alert=bool(drive_json.get("FailurePredicted", False)),
        raid_status=None,  # HPE standard path doesn't expose RAID status
        temperature_c=hpe_oem.get("CurrentTemperatureCelsius"),
        slot=str(drive_json.get("Location", "")),
        oem_data={
            k: v for k, v in {
                "current_temperature_celsius": hpe_oem.get("CurrentTemperatureCelsius"),
                "power_on_hours": hpe_oem.get("PowerOnHours"),
                "drive_status": hpe_oem.get("DriveStatus"),
                "ssd_endurance_utilization_pct": hpe_oem.get("SSDEnduranceUtilizationPercentage"),
            }.items() if v is not None
        },
    )


# ---------------------------------------------------------------------------
# Disk normalization — SmartStorage path (iLO5 only, Doc 08 §4.2 + §6)
# ---------------------------------------------------------------------------


def normalize_disk_smartstorage(drive_json: dict) -> NormalizedDisk:
    """Normalize a single HPE SmartStorage drive (iLO5 SmartArray).

    Key transformations:
    - CapacityMiB → capacity_bytes (multiply by 1048576)
    - SSDEnduranceUtilizationPercentage → life_left_pct (invert: 100 - value)
    - InterfaceType → protocol (same string values)
    """
    capacity_mib = drive_json.get("CapacityMiB")
    capacity_bytes = int(capacity_mib * 1048576) if capacity_mib is not None else None

    endurance = drive_json.get("SSDEnduranceUtilizationPercentage")
    life_left = (100 - endurance) if endurance is not None else None

    return NormalizedDisk(
        name=drive_json.get("Model", drive_json.get("Name", "")),
        serial=drive_json.get("SerialNumber", ""),
        media_type=drive_json.get("MediaType", ""),
        protocol=drive_json.get("InterfaceType", ""),  # SmartStorage uses InterfaceType
        capacity_bytes=capacity_bytes,
        health=_get(drive_json, "Status", "Health", default="Unknown"),
        life_left_pct=life_left,
        smart_alert=False,  # SmartStorage doesn't expose FailurePredicted
        raid_status=None,
        temperature_c=drive_json.get("CurrentTemperatureCelsius"),
        slot=str(drive_json.get("Location", "")),
        oem_data={
            k: v for k, v in {
                "current_temperature_celsius": drive_json.get("CurrentTemperatureCelsius"),
                "power_on_hours": drive_json.get("PowerOnHours"),
                "ssd_endurance_utilization_pct": endurance,
                "disk_drive_use": drive_json.get("DiskDriveUse"),
            }.items() if v is not None
        },
    )


def merge_standard_and_smartstorage(
    standard_drives: list[NormalizedDisk],
    smartstorage_drives: list[NormalizedDisk],
) -> list[NormalizedDisk]:
    """Merge standard Redfish drives with SmartStorage drives, deduplicating by serial.

    Standard path preferred for duplicates (iLO5 exposes NVMe drives on standard
    path only, SmartArray drives on both paths).
    """
    seen_serials: set[str] = set()
    result: list[NormalizedDisk] = []

    # Standard drives first (preferred)
    for d in standard_drives:
        if d.serial and d.serial in seen_serials:
            continue
        seen_serials.add(d.serial)
        result.append(d)

    # SmartStorage drives (only add if not already present)
    for d in smartstorage_drives:
        if d.serial and d.serial in seen_serials:
            continue
        seen_serials.add(d.serial)
        result.append(d)

    return result


# ---------------------------------------------------------------------------
# Memory normalization (Doc 08 §4.3, HPE column)
# ---------------------------------------------------------------------------


def normalize_memory(
    dimm_json: dict,
    metrics_json: Optional[dict] = None,
) -> NormalizedMemory:
    """Normalize a single HPE DIMM from /Memory/{id} + /MemoryMetrics."""
    mem_loc = dimm_json.get("MemoryLocation", {})
    alarm_trips = _get(metrics_json or {}, "HealthData", "AlarmTrips", default={})
    lifetime = (metrics_json or {}).get("LifeTime", {})
    current_period = (metrics_json or {}).get("CurrentPeriod", {})

    return NormalizedMemory(
        name=dimm_json.get("Id", ""),
        capacity_mib=dimm_json.get("CapacityMiB"),
        type=dimm_json.get("MemoryDeviceType", ""),
        speed_mhz=dimm_json.get("OperatingSpeedMhz"),
        health=_get(dimm_json, "Status", "Health", default="Unknown"),
        state=_get(dimm_json, "Status", "State", default="Unknown"),
        socket=mem_loc.get("Socket"),
        channel=mem_loc.get("Channel"),
        slot=mem_loc.get("Slot"),
        ecc_correctable_lifetime=lifetime.get("CorrectableECCErrorCount", 0),
        ecc_uncorrectable_lifetime=lifetime.get("UncorrectableECCErrorCount", 0),
        ecc_correctable_current=current_period.get("CorrectableECCErrorCount", 0),
        alarm_ecc_correctable=alarm_trips.get("CorrectableECCError", False),
        alarm_ecc_uncorrectable=alarm_trips.get("UncorrectableECCError", False),
        alarm_temperature=alarm_trips.get("Temperature", False),
        oem_data={
            k: v for k, v in {
                "dimm_status": _get(dimm_json, "Oem", "Hpe", "DIMMStatus"),
            }.items() if v is not None
        },
    )


# ---------------------------------------------------------------------------
# PSU normalization (Doc 08 §4.4, HPE column)
# ---------------------------------------------------------------------------


def normalize_psus(power_json: dict) -> tuple[list[NormalizedPSU], NormalizedPowerMetrics]:
    """Normalize HPE PSUs + power metrics from /Chassis/1/Power."""
    supplies = power_json.get("PowerSupplies", [])
    redundancy = power_json.get("Redundancy", [])
    redundancy_health = _get(redundancy[0], "Status", "Health", default="Unknown") if redundancy else "Unknown"
    redundancy_mode = redundancy[0].get("Mode", "") if redundancy else ""

    psus = []
    for psu in supplies:
        hpe_oem = _get(psu, "Oem", "Hpe", default={})
        psus.append(NormalizedPSU(
            name=psu.get("Name", ""),
            member_id=psu.get("MemberId", ""),
            type=psu.get("PowerSupplyType", ""),
            capacity_watts=psu.get("PowerCapacityWatts"),
            output_watts=psu.get("LastPowerOutputWatts"),
            input_voltage=psu.get("LineInputVoltage"),
            health=_get(psu, "Status", "Health", default="Unknown"),
            state=_get(psu, "Status", "State", default="Unknown"),
            model=psu.get("Model", ""),
            serial=psu.get("SerialNumber", ""),
            redundancy_health=redundancy_health,
            redundancy_mode=redundancy_mode,
            oem_data={
                k: v for k, v in {
                    "bay_number": hpe_oem.get("BayNumber"),
                    "hot_pluggable": hpe_oem.get("HotPluggable"),
                    "mismatched": hpe_oem.get("Mismatched"),
                    "psu_status_state": _get(hpe_oem, "PowerSupplyStatus", "State"),
                }.items() if v is not None
            },
        ))

    pc = power_json.get("PowerControl", [{}])[0] if power_json.get("PowerControl") else {}
    pm = pc.get("PowerMetrics", {})
    power_metrics = NormalizedPowerMetrics(
        system_power_watts=pc.get("PowerConsumedWatts"),
        system_power_avg=pm.get("AverageConsumedWatts"),
        system_power_peak=pm.get("MaxConsumedWatts"),
        system_power_min=pm.get("MinConsumedWatts"),
        interval_minutes=pm.get("IntervalInMin"),
    )

    return psus, power_metrics


# ---------------------------------------------------------------------------
# Thermal normalization (Doc 08 §4.5) — identical to Dell
# ---------------------------------------------------------------------------


def normalize_thermals(thermal_json: dict) -> list[NormalizedThermal]:
    """Normalize HPE temperature sensors from /Chassis/1/Thermal."""
    temps = thermal_json.get("Temperatures", [])
    result = []
    for t in temps:
        result.append(NormalizedThermal(
            name=t.get("Name", ""),
            reading_c=t.get("ReadingCelsius"),
            health=_get(t, "Status", "Health", default="Unknown"),
            threshold_warning=t.get("UpperThresholdNonCritical"),
            threshold_critical=t.get("UpperThresholdCritical"),
            threshold_fatal=t.get("UpperThresholdFatal"),
            threshold_cold_warning=t.get("LowerThresholdNonCritical"),
            threshold_cold_critical=t.get("LowerThresholdCritical"),
            context=t.get("PhysicalContext", ""),
        ))
    return result


# ---------------------------------------------------------------------------
# Log normalization (Doc 08 §4.7, HPE column)
# ---------------------------------------------------------------------------


def normalize_log_entries(iml_json: dict) -> list[NormalizedLogEntry]:
    """Normalize HPE IML entries from /Systems/1/LogServices/IML/Entries."""
    members = iml_json.get("Members", [])
    result = []
    for entry in members:
        hpe_oem = _get(entry, "Oem", "Hpe", default={})
        categories = hpe_oem.get("Categories", [])
        result.append(NormalizedLogEntry(
            id=entry.get("Id", ""),
            timestamp=entry.get("Created", ""),
            severity=entry.get("Severity", ""),
            message=entry.get("Message", ""),
            message_id=entry.get("MessageId", ""),
            component_id=None,  # HPE does not have FQDD equivalent
            category=categories[0] if categories else "",
        ))
    return result
