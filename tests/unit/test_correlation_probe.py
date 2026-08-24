"""Tests for CorrelationProbe two-device fault diagnosis (R3b-2 Phase 6)."""

from __future__ import annotations

from harkeniq.autonomy.correlation_probe import (
    CorrelationProbe,
    FaultLocation,
)


def _probe():
    return CorrelationProbe(my_agent_id="agent-me")


class TestCorrelationProbe:
    def test_local_port_fault(self):
        """Local errors, no remote errors → LOCAL_PORT."""
        result = _probe().diagnose(
            "device-x",
            local_errors={"crc_errors": 15, "fcs_errors": 0},
            remote_errors={"crc_errors": 0, "fcs_errors": 0},
        )
        assert result.fault_location == FaultLocation.LOCAL_PORT

    def test_remote_port_fault(self):
        """No local errors, remote errors → REMOTE_PORT."""
        result = _probe().diagnose(
            "device-x",
            local_errors={"crc_errors": 0},
            remote_errors={"rx_errors": 42},
        )
        assert result.fault_location == FaultLocation.REMOTE_PORT

    def test_cable_fault(self):
        """Both sides see errors → CABLE."""
        result = _probe().diagnose(
            "device-x",
            local_errors={"crc_errors": 10},
            remote_errors={"fcs_errors": 8},
        )
        assert result.fault_location == FaultLocation.CABLE

    def test_inconclusive(self):
        """No errors on either side → INCONCLUSIVE."""
        result = _probe().diagnose(
            "device-x",
            local_errors={"crc_errors": 0, "fcs_errors": 0},
            remote_errors={"crc_errors": 0, "fcs_errors": 0},
        )
        assert result.fault_location == FaultLocation.INCONCLUSIVE

    def test_evidence_included(self):
        result = _probe().diagnose(
            "device-x",
            local_errors={"crc_errors": 5},
            remote_errors={"crc_errors": 0},
        )
        assert result.evidence["probe_type"] == "two_device_correlation"
        assert result.evidence["fault_location"] == "LOCAL_PORT"
        assert result.evidence["local_agent"] == "agent-me"
