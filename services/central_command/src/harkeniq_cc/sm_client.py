"""gRPC client for communicating with Site Managers.

Each method opens an async channel, calls the RPC, and returns a plain
dict.  Bearer-token metadata is attached when a token is provided.
Channels are short-lived (one per call) to keep connection management
simple at this stage; a pool can be added later if latency matters.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

import grpc

from harkeniq.proto import harkeniq_pb2, harkeniq_pb2_grpc

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


def _metadata(token: Optional[str]) -> list[tuple[str, str]]:
    """Build gRPC call metadata with bearer token if provided."""
    if token:
        return [("authorization", f"Bearer {token}")]
    return []


class SMClient:
    """gRPC client for Site Manager RPCs.

    Each method creates an async insecure channel, calls the RPC, and
    returns a plain dict for easy JSON serialization.
    """

    async def register_site(
        self,
        sm_endpoint: str,
        tenant_id: str,
        site_name: str,
        license_fingerprint: str,
        site_id: str = "",
        cc_endpoint: str = "",
    ) -> dict:
        """Register this CC tenant with a Site Manager."""
        async with grpc.aio.insecure_channel(sm_endpoint) as channel:
            stub = harkeniq_pb2_grpc.SiteManagerServiceStub(channel)
            ack = await stub.RegisterSite(
                harkeniq_pb2.SiteRegistration(
                    tenant_id=tenant_id,
                    site_id=site_id,
                    site_name=site_name,
                    cc_endpoint=cc_endpoint,
                    license_key_fingerprint=license_fingerprint,
                ),
            )
            return {
                "accepted": ack.accepted,
                "site_token": ack.site_token,
                "reason": ack.reason,
            }

    async def get_fleet_snapshot(
        self,
        sm_endpoint: str,
        token: str,
        tenant_id: str,
        site_id: str,
    ) -> dict:
        """Pull the current fleet snapshot from a Site Manager."""
        async with grpc.aio.insecure_channel(sm_endpoint) as channel:
            stub = harkeniq_pb2_grpc.SiteManagerServiceStub(channel)
            snap = await stub.GetFleetSnapshot(
                harkeniq_pb2.FleetSnapshotRequest(
                    tenant_id=tenant_id,
                    site_id=site_id,
                ),
                metadata=_metadata(token),
            )
            devices = []
            for d in snap.devices:
                subsystems = {}
                if d.subsystems_json:
                    try:
                        subsystems = json.loads(d.subsystems_json)
                    except (json.JSONDecodeError, TypeError):
                        pass
                devices.append({
                    "agent_id": d.agent_id,
                    "agent_name": d.agent_name,
                    "vendor": d.vendor,
                    "model": d.model,
                    "observation": d.observation,
                    "health": d.health,
                    "subsystems": subsystems,
                    "last_seen_unix": d.last_seen_unix,
                })
            incidents = []
            for inc in snap.incidents:
                incidents.append({
                    "incident_id": inc.incident_id,
                    "kind": inc.kind,
                    "status": inc.status,
                    "title": inc.title,
                    "device_agent_id": inc.device_agent_id,
                    "subsystem": inc.subsystem,
                    "opened_at_unix": inc.opened_at_unix,
                })
            pending_actions = []
            for act in snap.pending_actions:
                pending_actions.append({
                    "action_id": act.action_id,
                    "type": act.type,
                    "device_agent_id": act.device_agent_id,
                    "severity": act.severity,
                    "skill_name": act.skill_name,
                    "status": act.status,
                    "proposed_at_unix": act.proposed_at_unix,
                })
            return {
                "devices": devices,
                "incidents": incidents,
                "pending_actions": pending_actions,
                "snapshot_at_unix": snap.snapshot_at_unix,
            }

    async def route_approval(
        self,
        sm_endpoint: str,
        token: str,
        action_id: str,
        decision: str,
        decided_by: str,
        tenant_id: str = "",
    ) -> dict:
        """Push an approval decision to a Site Manager."""
        async with grpc.aio.insecure_channel(sm_endpoint) as channel:
            stub = harkeniq_pb2_grpc.SiteManagerServiceStub(channel)
            ack = await stub.RouteApproval(
                harkeniq_pb2.ApprovalRouteRequest(
                    action_id=action_id,
                    decision=decision,
                    decided_by=decided_by,
                    tenant_id=tenant_id,
                ),
                metadata=_metadata(token),
            )
            return {
                "accepted": ack.accepted,
                "delivered": ack.delivered,
                "reason": ack.reason,
            }

    async def get_usage_snapshot(
        self,
        sm_endpoint: str,
        token: str,
        tenant_id: str,
        site_id: str,
        date: str,
    ) -> dict:
        """Pull usage data for a given date from a Site Manager."""
        async with grpc.aio.insecure_channel(sm_endpoint) as channel:
            stub = harkeniq_pb2_grpc.SiteManagerServiceStub(channel)
            snap = await stub.GetUsageSnapshot(
                harkeniq_pb2.UsageSnapshotRequest(
                    tenant_id=tenant_id,
                    site_id=site_id,
                    date=date,
                ),
                metadata=_metadata(token),
            )
            return {
                "node_count": snap.node_count,
                "agent_versions": dict(snap.agent_versions),
            }
