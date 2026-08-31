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
                # Capability Registry: an SM that predates it sends "",
                # and a malformed blob must read as UNDECLARED rather
                # than as an empty capability set.
                capabilities = None
                if getattr(d, "capabilities_json", ""):
                    try:
                        parsed = json.loads(d.capabilities_json)
                        if isinstance(parsed, dict):
                            capabilities = parsed
                    except (json.JSONDecodeError, TypeError):
                        capabilities = None
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
                    "capabilities": capabilities,
                })
            incidents = []
            for inc in snap.incidents:
                # S4: the diagnosis now rides the snapshot. Decode defensively
                # — an older SM leaves these empty, and a malformed blob must
                # not cost us the whole fleet poll.
                explanation = {}
                if inc.explanation_json:
                    try:
                        explanation = json.loads(inc.explanation_json)
                    except (ValueError, TypeError):
                        explanation = {}
                correlation_meta = {}
                if inc.correlation_meta_json:
                    try:
                        correlation_meta = json.loads(inc.correlation_meta_json)
                    except (ValueError, TypeError):
                        correlation_meta = {}
                incidents.append({
                    "incident_id": inc.incident_id,
                    "kind": inc.kind,
                    "status": inc.status,
                    "title": inc.title,
                    "device_agent_id": inc.device_agent_id,
                    "subsystem": inc.subsystem,
                    "opened_at_unix": inc.opened_at_unix,
                    "parent_incident_id": inc.parent_incident_id,
                    "confidence": inc.confidence,
                    "inferred": inc.inferred,
                    "correlation_meta": correlation_meta,
                    "explanation": explanation,
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
            # QA-042: outcomes were never dictified here, so CC's fleet
            # learning intake (_ingest_outcomes) ran on an empty feed since
            # R3b-3 — snapshot.get("outcomes", []) always returned [].
            outcomes = []
            for oc in snap.outcomes:
                outcomes.append({
                    "action_id": oc.action_id,
                    "action_type": oc.action_type,
                    "device_agent_id": oc.device_agent_id,
                    "outcome": oc.outcome,
                    "fault_resolved": oc.fault_resolved,
                    "vendor": oc.vendor,
                    "model": oc.model,
                    "recorded_at_unix": oc.recorded_at_unix,
                    # A1: attribution survives the wire, so an outcome
                    # can still name the Operational Agent that caused it.
                    "actor": oc.actor,
                })
            candidate_skills = []
            for cand in snap.candidate_skills:
                candidate_skills.append({
                    "skill_id": cand.skill_id,
                    "yaml_text": cand.yaml_text,
                    "source_device": cand.source_device,
                    "source_component": cand.source_component,
                    "validation_state": cand.validation_state,
                    "generated_at_unix": cand.generated_at_unix,
                    "warnings_json": cand.warnings_json,
                    "dry_run_matches": cand.dry_run_matches,
                })
            # S5: live safety state. An SM that predates the field leaves
            # `reported` false, which CC stores and renders as UNKNOWN —
            # the same answer as a site that failed to assemble it. Never
            # synthesise a "safe" default here; that is the one direction
            # a governance input may not err. (QA-042's lesson: a field
            # decoded nowhere is a feed that silently carries nothing.)
            safety = {
                "reported": bool(snap.safety.reported),
                "as_of_unix": snap.safety.as_of_unix,
                "sm_stop_switch": bool(snap.safety.sm_stop_switch),
                "suppressions": [
                    {
                        "domain_id": d.domain_id,
                        "event_family": d.event_family,
                        "trigger_reason": d.trigger_reason,
                        "device_count": d.device_count,
                        "triggered_at_unix": d.triggered_at_unix,
                        "all_clear_at_unix": d.all_clear_at_unix,
                    }
                    for d in snap.safety.suppressions
                ],
                "error_budgets": [
                    {
                        "action_type": b.action_type,
                        "success_count": b.success_count,
                        "failure_count": b.failure_count,
                        "total_count": b.total_count,
                        "min_success_rate": b.min_success_rate,
                        "dropped_back": bool(b.dropped_back),
                        "dropped_back_at_unix": b.dropped_back_at_unix,
                    }
                    for b in snap.safety.error_budgets
                ],
                "site_budgets": {
                    sb.action_type: sb.remaining for sb in snap.safety.site_budgets
                },
            }
            return {
                "devices": devices,
                "incidents": incidents,
                "pending_actions": pending_actions,
                "snapshot_at_unix": snap.snapshot_at_unix,
                "outcomes": outcomes,
                "candidate_skills": candidate_skills,
                "safety": safety,
                # E0.2: did the Site Manager resolve the site we asked
                # for? An unresolved snapshot is EMPTY by design and must
                # never be mistaken for "this site has no devices" -- the
                # poller clears the fleet cache and infers incident
                # resolution by absence, so that mistake would erase a
                # site's fleet and close all of its incidents.
                "site_resolved": snap.site_resolved,
                "site_reason": snap.site_reason,
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
        device_agent_ids: Optional[list] = None,
    ) -> dict:
        """R5-2: push a marketplace skill to a Site Manager.

        A2: `device_agent_ids` names the devices to install onto. Empty
        keeps the site-wide behaviour marketplace installs rely on; an
        Operational Agent always names its devices, because installing
        onto a whole site from a rack-scoped agent is a scope escape.
        """
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
                    device_agent_ids=sorted(set(device_agent_ids or [])),
                ),
                metadata=_metadata(token),
            )
            return {
                "accepted": ack.accepted,
                "queued": ack.queued,
                "reason": ack.reason,
            }

    async def plan_campaign_waves(
        self,
        sm_endpoint: str,
        token: Optional[str],
        *,
        tenant_id: str,
        site_id: str,
        campaign_id: str,
        campaign_version: int,
        action_type: str,
        device_agent_ids: list[str],
        max_wave_size: int = 5,
    ) -> dict:
        """S6: ask the site how these devices must be batched. READ-ONLY.

        Central Command never learns the site's fault domains and never
        plans a wave itself; it asks the tier that owns that knowledge and
        stores the answer. `planned=False` means the site could not be
        resolved and MUST NOT be read as "this site has no devices" --
        the same distinction A16.3 draws for an unresolved snapshot.

        The device list is sorted here so the determinism contract holds
        from the caller's side too: the same eligible set must produce the
        same plan hash.
        """
        async with self._channel(sm_endpoint) as channel:
            stub = harkeniq_pb2_grpc.SiteManagerServiceStub(channel)
            plan = await stub.PlanCampaignWaves(
                harkeniq_pb2.CampaignPlanRequest(
                    tenant_id=tenant_id,
                    site_id=site_id,
                    campaign_id=campaign_id,
                    campaign_version=int(campaign_version),
                    action_type=action_type,
                    device_agent_ids=sorted(set(device_agent_ids)),
                    max_wave_size=int(max_wave_size),
                ),
                metadata=_metadata(token),
            )
        return {
            "planned": plan.planned,
            "reason": plan.reason,
            "site_id": plan.site_id,
            "campaign_id": plan.campaign_id,
            "campaign_version": plan.campaign_version,
            "action_type": plan.action_type,
            "waves": [
                {
                    "wave_index": w.wave_index,
                    "device_agent_ids": list(w.device_agent_ids),
                    "domain_span": w.domain_span,
                }
                for w in plan.waves
            ],
            "unplannable_device_ids": list(plan.unplannable_device_ids),
            "plan_hash": plan.plan_hash,
            "generated_at_unix": plan.generated_at_unix,
            "separation_rule": plan.separation_rule,
        }

    async def dispatch_action(
        self,
        sm_endpoint: str,
        token: Optional[str],
        *,
        tenant_id: str,
        site_id: str,
        device_agent_id: str,
        action_type: str,
        params_json: str = "{}",
        actor: str = "",
        authorization: str = "",
        decided_by: str = "",
        proposal_id: str = "",
    ) -> dict:
        """A1: hand one decided action to the site that owns the device.

        This delivers a decision Central Command already governed; it
        does not make one and it authorizes nothing. The Site Manager
        queues it on the existing directive transport and the node runs
        its unchanged gate funnel, which can still refuse.
        """
        async with self._channel(sm_endpoint) as channel:
            stub = harkeniq_pb2_grpc.SiteManagerServiceStub(channel)
            ack = await stub.DispatchAction(
                harkeniq_pb2.ActionDispatch(
                    tenant_id=tenant_id,
                    site_id=site_id,
                    device_agent_id=device_agent_id,
                    action_type=action_type,
                    params_json=params_json,
                    actor=actor,
                    authorization=authorization,
                    decided_by=decided_by,
                    proposal_id=proposal_id,
                ),
                metadata=_metadata(token),
            )
            return {
                "accepted": ack.accepted,
                "directive_id": ack.directive_id,
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
