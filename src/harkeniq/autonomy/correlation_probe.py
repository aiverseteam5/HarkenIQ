"""Two-device correlation probe (R3b-2 Phase 6, OQ-13).

When the quorum engine reports LINK_DOWN between this agent and a suspect
device, the correlation probe collects receive-side error counters from
both ends to diagnose whether the fault is:
  - Local port fault (our side sees errors, remote doesn't)
  - Remote port fault (remote sees errors, we don't)
  - Cable fault (both sides see errors)
  - Inconclusive (no errors on either side)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger("harkeniq.autonomy.correlation")


class FaultLocation(Enum):
    """Diagnosed fault location from two-ended correlation."""

    LOCAL_PORT = "LOCAL_PORT"
    REMOTE_PORT = "REMOTE_PORT"
    CABLE = "CABLE"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass
class ProbeResult:
    """Result of a two-device correlation probe."""

    suspect_device_id: str
    local_errors: dict    # our receive-side error counters
    remote_errors: dict   # their receive-side error counters
    fault_location: FaultLocation
    evidence: dict        # combined evidence for claim


class CorrelationProbe:
    """Two-ended correlation probe for cable/port fault diagnosis."""

    # Error counter fields to check
    ERROR_FIELDS = ("crc_errors", "fcs_errors", "interface_resets", "rx_errors")

    def __init__(self, my_agent_id: str) -> None:
        self._my_id = my_agent_id

    def diagnose(
        self,
        suspect_device_id: str,
        local_errors: dict,
        remote_errors: dict,
    ) -> ProbeResult:
        """Diagnose fault location from both-sides error counters.

        Args:
            suspect_device_id: The device at the other end of the link.
            local_errors: Our receive-side error counters
                         (e.g., {"crc_errors": 12, "fcs_errors": 0}).
            remote_errors: Their receive-side error counters.

        Returns:
            ProbeResult with diagnosed fault location.
        """
        local_has_errors = self._has_significant_errors(local_errors)
        remote_has_errors = self._has_significant_errors(remote_errors)

        if local_has_errors and not remote_has_errors:
            location = FaultLocation.LOCAL_PORT
        elif remote_has_errors and not local_has_errors:
            location = FaultLocation.REMOTE_PORT
        elif local_has_errors and remote_has_errors:
            location = FaultLocation.CABLE
        else:
            location = FaultLocation.INCONCLUSIVE

        evidence = {
            "probe_type": "two_device_correlation",
            "local_agent": self._my_id,
            "remote_device": suspect_device_id,
            "local_errors": local_errors,
            "remote_errors": remote_errors,
            "fault_location": location.value,
        }

        return ProbeResult(
            suspect_device_id=suspect_device_id,
            local_errors=local_errors,
            remote_errors=remote_errors,
            fault_location=location,
            evidence=evidence,
        )

    def _has_significant_errors(self, errors: dict) -> bool:
        """Check if error counters indicate a real problem."""
        for field in self.ERROR_FIELDS:
            val = errors.get(field, 0)
            if isinstance(val, (int, float)) and val > 0:
                return True
        return False
