"""Stub gRPC client for communicating with Site Managers."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("harkeniq.cc.sm_client")


@dataclass
class SiteRegistrationAck:
    site_id: str
    accepted: bool
    message: str = ""


@dataclass
class FleetSnapshot:
    site_id: str
    devices: list = None

    def __post_init__(self):
        if self.devices is None:
            self.devices = []


@dataclass
class ApprovalRouteAck:
    action_id: str
    delivered: bool
    message: str = ""


@dataclass
class UsageSnapshot:
    site_id: str
    date: str
    node_count: int = 0
    agent_versions: Optional[dict] = None


class SMClient:
    """gRPC client for Site Manager RPCs.

    Each method creates a gRPC channel, calls the RPC, and returns the
    result. Full implementation pending proto integration in R2b Phase 2.
    """

    async def register_site(
        self,
        sm_endpoint: str,
        tenant_id: str,
        site_name: str,
        license_fingerprint: str,
    ) -> SiteRegistrationAck:
        """Register this CC tenant with a Site Manager."""
        raise NotImplementedError("SMClient.register_site not yet implemented")

    async def get_fleet_snapshot(
        self,
        sm_endpoint: str,
        token: str,
        tenant_id: str,
        site_id: str,
    ) -> FleetSnapshot:
        """Pull the current fleet snapshot from a Site Manager."""
        raise NotImplementedError("SMClient.get_fleet_snapshot not yet implemented")

    async def route_approval(
        self,
        sm_endpoint: str,
        token: str,
        action_id: str,
        decision: str,
        decided_by: str,
    ) -> ApprovalRouteAck:
        """Push an approval decision to a Site Manager."""
        raise NotImplementedError("SMClient.route_approval not yet implemented")

    async def get_usage_snapshot(
        self,
        sm_endpoint: str,
        token: str,
        tenant_id: str,
        site_id: str,
        date: str,
    ) -> UsageSnapshot:
        """Pull usage data for a given date from a Site Manager."""
        raise NotImplementedError("SMClient.get_usage_snapshot not yet implemented")
