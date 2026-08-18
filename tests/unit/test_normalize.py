"""Unit tests for vendor normalization (Dell + health rollup)."""

from harkeniq.redfish.dell import (
    normalize_disk,
    normalize_fans,
    normalize_log_entries,
    normalize_memory,
    normalize_psus,
    normalize_thermals,
)
from harkeniq.redfish.normalize import (
    NormalizedDevice,
    NormalizedFan,
    NormalizedPSU,
    NormalizedThermal,
    compute_health_rollup,
    worst_health,
)


# ---------------------------------------------------------------------------
# Dell Fan Normalization
# ---------------------------------------------------------------------------


class TestDellFanNormalization:
    THERMAL_DATA = {
        "Fans": [
            {
                "MemberId": "0",
                "Name": "System Board Fan1A",
                "FanName": "System Board Fan1A",
                "Reading": 9800,
                "ReadingUnits": "RPM",
                "Status": {"State": "Enabled", "Health": "OK"},
                "LowerThresholdCritical": 480,
                "PhysicalContext": "SystemBoard",
                "Oem": {"Dell": {"DellFan": {"FanPWM": 29, "HardwareType": "System Board Fan"}}},
            }
        ],
        "Redundancy": [
            {"MemberId": "0", "Status": {"State": "Enabled", "Health": "OK"}, "Mode": "N+1"}
        ],
    }

    def test_normalize_fan_basic(self):
        fans = normalize_fans(self.THERMAL_DATA)
        assert len(fans) == 1
        fan = fans[0]
        assert fan.name == "System Board Fan1A"
        assert fan.speed_rpm == 9800
        assert fan.speed_pct == 29
        assert fan.health == "OK"
        assert fan.state == "Enabled"
        assert fan.threshold_low_critical == 480
        assert fan.redundancy_health == "OK"
        assert fan.location == "SystemBoard"

    def test_fan_oem_data(self):
        fans = normalize_fans(self.THERMAL_DATA)
        assert fans[0].oem_data["fan_pwm"] == 29
        assert fans[0].oem_data["hardware_type"] == "System Board Fan"

    def test_fan_critical(self):
        data = {
            "Fans": [
                {
                    "Name": "Fan1A",
                    "Reading": 0,
                    "Status": {"State": "Enabled", "Health": "Critical"},
                    "LowerThresholdCritical": 480,
                }
            ],
            "Redundancy": [{"Status": {"Health": "Warning"}}],
        }
        fans = normalize_fans(data)
        assert fans[0].health == "Critical"
        assert fans[0].speed_rpm == 0
        assert fans[0].redundancy_health == "Warning"

    def test_fan_absent(self):
        data = {"Fans": [{"Name": "Fan1A", "Status": {"State": "Absent"}}], "Redundancy": []}
        fans = normalize_fans(data)
        assert fans[0].state == "Absent"
        assert fans[0].redundancy_health == "Unknown"

    def test_empty_fans(self):
        fans = normalize_fans({"Fans": [], "Redundancy": []})
        assert fans == []


# ---------------------------------------------------------------------------
# Dell Disk Normalization
# ---------------------------------------------------------------------------


class TestDellDiskNormalization:
    DRIVE_DATA = {
        "Id": "Disk.Bay.0:Enclosure.Internal.0-1:RAID.Slot.1-1",
        "Name": "Solid State Disk 0:1:0",
        "Model": "SAMSUNG MZ7LH960HAJR-00005",
        "SerialNumber": "S45NNA0M100001",
        "MediaType": "SSD",
        "Protocol": "SATA",
        "CapacityBytes": 960197124096,
        "PredictedMediaLifeLeftPercent": 98,
        "FailurePredicted": False,
        "Status": {"State": "Enabled", "Health": "OK"},
        "Oem": {
            "Dell": {
                "DellPhysicalDisk": {
                    "RaidStatus": "Online",
                    "RemainingRatedWriteEndurance": 98,
                    "SmartAlertIndication": "No",
                    "Slot": 0,
                }
            }
        },
    }

    def test_normalize_disk_basic(self):
        disk = normalize_disk(self.DRIVE_DATA)
        assert disk.name == "SAMSUNG MZ7LH960HAJR-00005"
        assert disk.serial == "S45NNA0M100001"
        assert disk.media_type == "SSD"
        assert disk.protocol == "SATA"
        assert disk.capacity_bytes == 960197124096
        assert disk.health == "OK"
        assert disk.life_left_pct == 98
        assert disk.smart_alert is False
        assert disk.raid_status == "Online"
        assert disk.slot == "0"

    def test_disk_smart_alert_failure_predicted(self):
        data = {**self.DRIVE_DATA, "FailurePredicted": True}
        disk = normalize_disk(data)
        assert disk.smart_alert is True

    def test_disk_smart_alert_dell_oem(self):
        data = dict(self.DRIVE_DATA)
        data["Oem"] = {"Dell": {"DellPhysicalDisk": {"SmartAlertIndication": "Yes", "RaidStatus": "Online"}}}
        disk = normalize_disk(data)
        assert disk.smart_alert is True

    def test_disk_oem_data(self):
        disk = normalize_disk(self.DRIVE_DATA)
        assert disk.oem_data["raid_status"] == "Online"
        assert disk.oem_data["remaining_rated_write_endurance"] == 98

    def test_disk_hdd_no_life_left(self):
        data = {**self.DRIVE_DATA, "MediaType": "HDD", "PredictedMediaLifeLeftPercent": None}
        disk = normalize_disk(data)
        assert disk.media_type == "HDD"
        assert disk.life_left_pct is None


# ---------------------------------------------------------------------------
# Dell Memory Normalization
# ---------------------------------------------------------------------------


class TestDellMemoryNormalization:
    DIMM_DATA = {
        "Id": "DIMM.Socket.A1",
        "CapacityMiB": 32768,
        "MemoryDeviceType": "DDR4",
        "OperatingSpeedMhz": 3200,
        "Status": {"State": "Enabled", "Health": "OK"},
        "MemoryLocation": {"Socket": 1, "Channel": 0, "Slot": 0},
        "Oem": {"Dell": {"DellMemory": {"BankLabel": "A", "ManufactureDate": "2023-Wk42"}}},
    }

    METRICS_DATA = {
        "HealthData": {
            "AlarmTrips": {
                "CorrectableECCError": False,
                "UncorrectableECCError": False,
                "Temperature": False,
            }
        },
        "CurrentPeriod": {"CorrectableECCErrorCount": 0, "UncorrectableECCErrorCount": 0},
        "LifeTime": {"CorrectableECCErrorCount": 3, "UncorrectableECCErrorCount": 0},
    }

    def test_normalize_memory_basic(self):
        mem = normalize_memory(self.DIMM_DATA, self.METRICS_DATA)
        assert mem.name == "DIMM.Socket.A1"
        assert mem.capacity_mib == 32768
        assert mem.type == "DDR4"
        assert mem.speed_mhz == 3200
        assert mem.health == "OK"
        assert mem.state == "Enabled"
        assert mem.socket == 1
        assert mem.channel == 0
        assert mem.slot == 0
        assert mem.ecc_correctable_lifetime == 3
        assert mem.ecc_uncorrectable_lifetime == 0
        assert mem.alarm_ecc_correctable is False

    def test_memory_ecc_alarm(self):
        metrics = dict(self.METRICS_DATA)
        metrics["HealthData"] = {
            "AlarmTrips": {"CorrectableECCError": True, "UncorrectableECCError": True, "Temperature": False}
        }
        mem = normalize_memory(self.DIMM_DATA, metrics)
        assert mem.alarm_ecc_correctable is True
        assert mem.alarm_ecc_uncorrectable is True

    def test_memory_no_metrics(self):
        mem = normalize_memory(self.DIMM_DATA, None)
        assert mem.ecc_correctable_lifetime == 0
        assert mem.alarm_ecc_correctable is False

    def test_memory_absent_slot(self):
        dimm = {"Id": "DIMM.Socket.A5", "Status": {"State": "Absent", "Health": "Unknown"}}
        mem = normalize_memory(dimm, None)
        assert mem.state == "Absent"
        assert mem.health == "Unknown"

    def test_memory_oem_data(self):
        mem = normalize_memory(self.DIMM_DATA, self.METRICS_DATA)
        assert mem.oem_data["bank_label"] == "A"
        assert mem.oem_data["manufacture_date"] == "2023-Wk42"


# ---------------------------------------------------------------------------
# Dell PSU Normalization
# ---------------------------------------------------------------------------


class TestDellPSUNormalization:
    POWER_DATA = {
        "PowerSupplies": [
            {
                "MemberId": "0",
                "Name": "PS1 Status",
                "PowerSupplyType": "AC",
                "LineInputVoltage": 208,
                "PowerCapacityWatts": 1400,
                "LastPowerOutputWatts": 186,
                "Status": {"State": "Enabled", "Health": "OK"},
                "Model": "PWR SPLY,1400W",
                "SerialNumber": "CNLOD001",
            },
            {
                "MemberId": "1",
                "Name": "PS2 Status",
                "PowerSupplyType": "AC",
                "Status": {"State": "Enabled", "Health": "OK"},
            },
        ],
        "PowerControl": [
            {
                "PowerConsumedWatts": 186,
                "PowerMetrics": {"AverageConsumedWatts": 192, "MaxConsumedWatts": 245, "MinConsumedWatts": 178, "IntervalInMin": 1},
            }
        ],
        "Redundancy": [{"Mode": "Failover", "Status": {"State": "Enabled", "Health": "OK"}}],
    }

    def test_normalize_psus(self):
        psus, metrics = normalize_psus(self.POWER_DATA)
        assert len(psus) == 2
        assert psus[0].name == "PS1 Status"
        assert psus[0].capacity_watts == 1400
        assert psus[0].output_watts == 186
        assert psus[0].input_voltage == 208
        assert psus[0].health == "OK"
        assert psus[0].redundancy_health == "OK"
        assert psus[0].redundancy_mode == "Failover"

    def test_psu_power_metrics(self):
        _, metrics = normalize_psus(self.POWER_DATA)
        assert metrics.system_power_watts == 186
        assert metrics.system_power_avg == 192
        assert metrics.system_power_peak == 245

    def test_psu_absent(self):
        data = dict(self.POWER_DATA)
        data["PowerSupplies"] = [
            {"MemberId": "0", "Name": "PS1", "Status": {"State": "Enabled", "Health": "OK"}},
            {"MemberId": "1", "Name": "PS2", "Status": {"State": "Absent", "Health": "Critical"}},
        ]
        data["Redundancy"] = [{"Mode": "Failover", "Status": {"Health": "Warning"}}]
        psus, _ = normalize_psus(data)
        assert psus[1].state == "Absent"
        assert psus[1].health == "Critical"
        assert psus[0].redundancy_health == "Warning"


# ---------------------------------------------------------------------------
# Dell Thermal Normalization
# ---------------------------------------------------------------------------


class TestDellThermalNormalization:
    THERMAL_DATA = {
        "Temperatures": [
            {
                "Name": "System Board Inlet Temp",
                "ReadingCelsius": 22,
                "Status": {"State": "Enabled", "Health": "OK"},
                "UpperThresholdNonCritical": 42,
                "UpperThresholdCritical": 47,
                "UpperThresholdFatal": None,
                "LowerThresholdNonCritical": 3,
                "LowerThresholdCritical": -7,
                "PhysicalContext": "Intake",
            },
            {
                "Name": "CPU1 Temp",
                "ReadingCelsius": 54,
                "Status": {"State": "Enabled", "Health": "OK"},
                "UpperThresholdNonCritical": 90,
                "UpperThresholdCritical": 98,
                "PhysicalContext": "CPU",
            },
        ]
    }

    def test_normalize_thermals(self):
        thermals = normalize_thermals(self.THERMAL_DATA)
        assert len(thermals) == 2
        inlet = thermals[0]
        assert inlet.name == "System Board Inlet Temp"
        assert inlet.reading_c == 22
        assert inlet.health == "OK"
        assert inlet.threshold_warning == 42
        assert inlet.threshold_critical == 47
        assert inlet.threshold_fatal is None
        assert inlet.threshold_cold_warning == 3
        assert inlet.threshold_cold_critical == -7
        assert inlet.context == "Intake"

    def test_thermal_cpu(self):
        thermals = normalize_thermals(self.THERMAL_DATA)
        cpu = thermals[1]
        assert cpu.name == "CPU1 Temp"
        assert cpu.reading_c == 54
        assert cpu.context == "CPU"


# ---------------------------------------------------------------------------
# Dell Log Normalization
# ---------------------------------------------------------------------------


class TestDellLogNormalization:
    SEL_DATA = {
        "Members": [
            {
                "Id": "1",
                "Created": "2026-09-15T14:30:45Z",
                "Severity": "Critical",
                "Message": "Fan1A has failed",
                "MessageId": "FAN0001",
                "Oem": {"Dell": {"DellSELEntry": {"Category": "System Health", "FQDD": "Fan.Embedded.1A"}}},
            }
        ]
    }

    def test_normalize_log_entries(self):
        entries = normalize_log_entries(self.SEL_DATA)
        assert len(entries) == 1
        e = entries[0]
        assert e.id == "1"
        assert e.severity == "Critical"
        assert e.message == "Fan1A has failed"
        assert e.component_id == "Fan.Embedded.1A"
        assert e.category == "System Health"

    def test_empty_log(self):
        entries = normalize_log_entries({"Members": []})
        assert entries == []
