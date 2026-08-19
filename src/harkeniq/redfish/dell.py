"""Dell iDRAC Redfish normalization (Doc 08 §4, Dell column).

Transforms raw Dell Redfish JSON into normalized dataclasses.
Each function takes a raw JSON dict and returns normalized objects.
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
# Fan normalization (Doc 08 §4.1)
# ---------------------------------------------------------------------------


def normalize_fans(thermal_json: dict) -> list[NormalizedFan]:
    """Normalize Dell fan data from /Chassis/{id}/Thermal response."""
    fans = thermal_json.get("Fans", [])
    redundancy = thermal_json.get("Redundancy", [])
    redundancy_health = _get(redundancy[0], "Status", "Health", default="Unknown") if redundancy else "Unknown"

    result = []
    for fan in fans:
        result.append(NormalizedFan(
            name=fan.get("FanName", fan.get("Name", "")),
            speed_rpm=fan.get("Reading"),
            speed_pct=_get(fan, "Oem", "Dell", "DellFan", "FanPWM"),
            health=_get(fan, "Status", "Health", default="Unknown"),
            state=_get(fan, "Status", "State", default="Unknown"),
            threshold_low_critical=fan.get("LowerThresholdCritical"),
            redundancy_health=redundancy_health,
            location=fan.get("PhysicalContext", ""),
            oem_data={
                k: v for k, v in {
                    "fan_pwm": _get(fan, "Oem", "Dell", "DellFan", "FanPWM"),
                    "hardware_type": _get(fan, "Oem", "Dell", "DellFan", "HardwareType"),
                }.items() if v is not None
            },
        ))
    return result


# ---------------------------------------------------------------------------
# Disk normalization (Doc 08 §4.2)
# ---------------------------------------------------------------------------


def normalize_disk(drive_json: dict) -> NormalizedDisk:
    """Normalize a single Dell drive from /Storage/{cid}/Drives/{did} response."""
    dell_oem = _get(drive_json, "Oem", "Dell", "DellPhysicalDisk", default={})

    # SMART alert: true if either FailurePredicted or SmartAlertIndication
    smart_alert = bool(drive_json.get("FailurePredicted", False))
    if not smart_alert and dell_oem.get("SmartAlertIndication") == "Yes":
        smart_alert = True

    return NormalizedDisk(
        name=drive_json.get("Model", drive_json.get("Name", "")),
        serial=drive_json.get("SerialNumber", ""),
        media_type=drive_json.get("MediaType", ""),
        protocol=drive_json.get("Protocol", ""),
        capacity_bytes=drive_json.get("CapacityBytes"),
        health=_get(drive_json, "Status", "Health", default="Unknown"),
        life_left_pct=drive_json.get("PredictedMediaLifeLeftPercent"),
        smart_alert=smart_alert,
        raid_status=dell_oem.get("RaidStatus"),
        temperature_c=None,  # Dell doesn't expose drive temp in standard path
        slot=str(dell_oem.get("Slot", "")),
        oem_data={
            k: v for k, v in {
                "raid_status": dell_oem.get("RaidStatus"),
                "remaining_rated_write_endurance": dell_oem.get("RemainingRatedWriteEndurance"),
                "smart_alert_indication": dell_oem.get("SmartAlertIndication"),
                "dell_slot": dell_oem.get("Slot"),
            }.items() if v is not None
        },
    )


# ---------------------------------------------------------------------------
# Memory normalization (Doc 08 §4.3)
# ---------------------------------------------------------------------------


def normalize_memory(
    dimm_json: dict,
    metrics_json: Optional[dict] = None,
) -> NormalizedMemory:
    """Normalize a single Dell DIMM from /Memory/{id} + /MemoryMetrics."""
    mem_loc = dimm_json.get("MemoryLocation", {})

    # ECC metrics from MemoryMetrics endpoint
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
        ecc_correctable_lifetime=lifetime.get("CorrectableECCErrorCount"),
        ecc_uncorrectable_lifetime=lifetime.get("UncorrectableECCErrorCount"),
        ecc_correctable_current=current_period.get("CorrectableECCErrorCount"),
        alarm_ecc_correctable=alarm_trips.get("CorrectableECCError", False),
        alarm_ecc_uncorrectable=alarm_trips.get("UncorrectableECCError", False),
        alarm_temperature=alarm_trips.get("Temperature", False),
        oem_data={
            k: v for k, v in {
                "bank_label": _get(dimm_json, "Oem", "Dell", "DellMemory", "BankLabel"),
                "manufacture_date": _get(dimm_json, "Oem", "Dell", "DellMemory", "ManufactureDate"),
            }.items() if v is not None
        },
    )


# ---------------------------------------------------------------------------
# PSU normalization (Doc 08 §4.4)
# ---------------------------------------------------------------------------


def normalize_psus(power_json: dict) -> tuple[list[NormalizedPSU], NormalizedPowerMetrics]:
    """Normalize Dell PSUs + power metrics from /Chassis/{id}/Power response."""
    supplies = power_json.get("PowerSupplies", [])
    redundancy = power_json.get("Redundancy", [])
    redundancy_health = _get(redundancy[0], "Status", "Health", default="Unknown") if redundancy else "Unknown"
    redundancy_mode = redundancy[0].get("Mode", "") if redundancy else ""

    psus = []
    for psu in supplies:
        dell_oem = _get(psu, "Oem", "Dell", "DellPowerSupply", default={})
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
                    "detailed_state": dell_oem.get("DetailedState"),
                    "max_input_power_watts": dell_oem.get("Range1MaxInputPowerWatts"),
                }.items() if v is not None
            },
        ))

    # Power metrics from PowerControl
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
# Thermal normalization (Doc 08 §4.5)
# ---------------------------------------------------------------------------


def normalize_thermals(thermal_json: dict) -> list[NormalizedThermal]:
    """Normalize Dell temperature sensors from /Chassis/{id}/Thermal."""
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
# Log normalization (Doc 08 §4.7)
# ---------------------------------------------------------------------------


def normalize_log_entries(sel_json: dict) -> list[NormalizedLogEntry]:
    """Normalize Dell SEL entries from /Managers/{id}/LogServices/Sel/Entries."""
    members = sel_json.get("Members", [])
    result = []
    for entry in members:
        dell_oem = _get(entry, "Oem", "Dell", "DellSELEntry", default={})
        result.append(NormalizedLogEntry(
            id=entry.get("Id", ""),
            timestamp=entry.get("Created", ""),
            severity=entry.get("Severity", ""),
            message=entry.get("Message", ""),
            message_id=entry.get("MessageId", ""),
            component_id=dell_oem.get("FQDD"),
            category=dell_oem.get("Category", ""),
        ))
    return result
