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

    Each method opens a channel, calls the RPC, and returns a plain dict.
    QA-018: TLS when constructed with a CA bundle (``tls_ca`` — typically
    ``CCConfig.sm_tls_ca``); plaintext only when no CA is configured, which
    was previously the ONLY mode — site tokens and fleet telemetry crossed
    the wire in the clear with no way to turn TLS on.
    """

    def __init__(self, tls_ca: str = "") -> None:
        self.tls_ca = tls_ca

    def _channel(self, sm_endpoint: str):
        if self.tls_ca:
            with open(self.tls_ca, "rb") as ca_file:
                creds = grpc.ssl_channel_credentials(
                    root_certificates=ca_file.read()
                )
            return grpc.aio.secure_channel(sm_endpoint, creds)
        return grpc.aio.insecure_channel(sm_endpoint)

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
        async with self._channel(sm_endpoint) as channel:
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
        async with self._channel(sm_endpoint) as channel:
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
                inventory = {}
                if d.inventory_json:
                    try:
                        inventory = json.loads(d.inventory_json)
                    except (json.JSONDecodeError, TypeError):
                        pass
                devices.append({
                    "agent_id": d.agent_id,
                    "agent_name": d.agent_name,
                    "vendor": d.vendor,
                    "model": d.model,
                    "device_class": d.device_class or "server",
                    "observation": d.observation,
                    "health": d.health,
                    "subsystems": subsystems,
                    "last_seen_unix": d.last_seen_unix,
                    "service_tag": d.service_tag,
                    "firmware": inventory.get("firmware") or [],
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
        async with self._channel(sm_endpoint) as channel:
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
        async with self._channel(sm_endpoint) as channel:
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


    async def install_skill(
        self,
        sm_endpoint: str,
        token: Optional[str],
        tenant_id: str,
        site_id: str,
        skill_name: str,
        skill_version: str,
        yaml_content: str,
        tier: str = "community",
        validation_state: str = "tested",
        issued_by: str = "marketplace",
    ) -> dict:
        """R5-2: push a marketplace skill to a Site Manager."""
        async with self._channel(sm_endpoint) as channel:
            stub = harkeniq_pb2_grpc.SiteManagerServiceStub(channel)
            ack = await stub.InstallSkill(
                harkeniq_pb2.SiteSkillInstall(
                    tenant_id=tenant_id,
                    site_id=site_id,
                    skill_id=skill_name,
                    skill_version=skill_version,
                    yaml_content=yaml_content,
                    tier=tier,
                    validation_state=validation_state,
                    issued_by=issued_by,
                ),
                metadata=_metadata(token),
            )
            return {
                "accepted": ack.accepted,
                "queued": ack.queued,
                "reason": ack.reason,
            }

    async def push_policy(
        self,
        sm_endpoint: str,
        token: Optional[str],
        tenant_id: str,
        site_id: str,
        autonomy_budgets_json: str = "",
        approval_policies_json: str = "",
        learned_patterns_json: str = "",
    ) -> dict:
        """QA-022/033: push autonomy policy + fleet knowledge to an SM.

        ``autonomy_budgets_json`` shape is defined in
        harkeniq_cc.policy_push (stop_switch + policies list);
        ``learned_patterns_json`` is a list of fleet-pattern dicts
        (KnowledgeDistributor.prepare_payload). The SM applies budgets to
        its enforcer and upserts patterns for reasoning enrichment.
        """
        async with self._channel(sm_endpoint) as channel:
            stub = harkeniq_pb2_grpc.SiteManagerServiceStub(channel)
            ack = await stub.PushPolicy(
                harkeniq_pb2.PolicyUpdate(
                    tenant_id=tenant_id,
                    site_id=site_id,
                    approval_policies_json=approval_policies_json,
                    autonomy_budgets_json=autonomy_budgets_json,
                    learned_patterns_json=learned_patterns_json,
                ),
                metadata=_metadata(token),
            )
            return {"accepted": ack.accepted, "reason": ack.reason}
