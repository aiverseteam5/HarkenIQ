"""gRPC services: agent-initiated + Central-Command-initiated RPCs.

AgentServiceServicer: agent → SM (agent-initiated, SM never dials nodes).
SiteManagerServiceServicer: CC → SM (CC dials SM to register, poll fleet,
route approvals, collect usage, and push policy).

TLS server credentials + bearer-token interceptor unless the config is
explicitly insecure (lab / unit tests). Action RPCs delegate to the
approvals service once it is attached; until then they are politely
refused so agents keep retrying without error noise.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import grpc
from sqlalchemy import func, select

from harkeniq.proto import harkeniq_pb2, harkeniq_pb2_grpc
from harkeniq_sm.approvals import ApprovalService
from harkeniq_sm.auth import BearerTokenInterceptor
from harkeniq_sm.config import SMConfig
from harkeniq_sm.coverage import observation_state, worst_health
from harkeniq_sm.db.models import Device, DeviceSubsystemState
from harkeniq_sm.db.repos import ActionRepo, DeviceRepo, IncidentRepo, StatusRepo
from harkeniq_sm.ingest import IngestService

logger = logging.getLogger("harkeniq.sm.grpc")


class AgentServiceServicer(harkeniq_pb2_grpc.AgentServiceServicer):
    def __init__(self, ingest: IngestService, approvals=None, identity_service=None) -> None:
        self.ingest = ingest
        self.approvals = approvals  # attached by the approvals phase
        self.identity_service = identity_service  # R3a: AgentIdentityService

    async def RegisterAgent(self, request, context):
        site_name = await self.ingest.register(
            agent_id=request.agent_id,
            agent_name=request.agent_name,
            vendor=request.vendor,
            model=request.model,
            service_tag=request.service_tag,
            bmc_location_json=request.bmc_location_json,
            peers=list(request.peers),
        )

        # R3a: if agent sent a public key, register identity and issue cert
        sm_public_key_pem = b""
        agent_certificate = b""
        if self.identity_service and request.public_key_pem:
            try:
                sm_public_key_pem, agent_certificate = (
                    await self.identity_service.register_agent(
                        agent_id=request.agent_id,
                        public_key_pem=bytes(request.public_key_pem),
                        site_name=site_name,
                    )
                )
            except ValueError as e:
                logger.warning("Agent identity registration failed: %s", e)

        return harkeniq_pb2.RegistrationAck(
            accepted=True,
            site_name=site_name,
            sm_public_key_pem=sm_public_key_pem,
            agent_certificate=agent_certificate,
        )

    async def Heartbeat(self, request, context):
        accepted = await self.ingest.heartbeat(
            agent_id=request.agent_id,
            agent_name=request.agent_name,
            state=request.state,
            health_summary=dict(request.health_summary),
            peer_status=dict(request.peer_status),
        )

        # R3a: issue signed authorization lease if identity service is active
        lease_bytes = b""
        lease_expiry_unix = 0
        if self.identity_service and accepted:
            try:
                lease_bytes, lease_expiry_unix = (
                    await self.identity_service.issue_lease(
                        agent_id=request.agent_id,
                    )
                )
            except Exception as e:
                logger.warning("Lease issuance failed for %s: %s", request.agent_id, e)

        return harkeniq_pb2.HeartbeatAck(
            accepted=accepted,
            authorization_lease=lease_bytes,
            lease_expiry_unix=lease_expiry_unix,
        )

    async def ReportVerdict(self, request, context):
        accepted = await self.ingest.verdict(
            agent_id=request.agent_id,
            sensor_id=request.sensor_id,
            skill_name=request.skill_name,
            severity=request.verdict,
            evidence_json=request.evidence_json,
        )
        return harkeniq_pb2.VerdictAck(accepted=accepted)

    async def ReportAction(self, request, context):
        if self.approvals is None:
            return harkeniq_pb2.ActionAck(accepted=False)
        accepted = await self.approvals.report_action(request)
        return harkeniq_pb2.ActionAck(accepted=accepted)

    async def PollActionDecisions(self, request, context):
        if self.approvals is None:
            return harkeniq_pb2.DecisionList()
        decisions = await self.approvals.poll_decisions(request.agent_id)
        return harkeniq_pb2.DecisionList(decisions=decisions)


_SEVERITY_RANK = {"OK": 0, "TRENDING": 1, "UNKNOWN": 2, "WARNING": 3, "CRITICAL": 4}


def _worst_severity(severities: list[str]) -> str:
    """Return the worst severity from a list; defaults to 'ok'."""
    worst = "OK"
    for s in severities:
        if _SEVERITY_RANK.get(s, 2) > _SEVERITY_RANK.get(worst, 0):
            worst = s
    return worst.lower()


def _ts(dt: Optional[datetime]) -> int:
    """Convert a datetime to a unix timestamp; 0 if None."""
    if dt is None:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


class SiteManagerServiceServicer(harkeniq_pb2_grpc.SiteManagerServiceServicer):
    """RPCs from Central Command (CC dials SM)."""

    def __init__(
        self,
        sessionmaker,
        approvals: ApprovalService,
        config: SMConfig,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.approvals = approvals
        self.config = config

    async def RegisterSite(self, request, context):
        if not request.license_key_fingerprint:
            return harkeniq_pb2.SiteRegistrationAck(
                accepted=False,
                reason="license_key_fingerprint is required",
            )
        logger.info(
            "Site registration from CC: tenant=%s site=%s name=%s cc=%s",
            request.tenant_id, request.site_id, request.site_name,
            request.cc_endpoint,
        )
        return harkeniq_pb2.SiteRegistrationAck(
            accepted=True,
            site_token=self.config.site_token,
        )

    async def GetFleetSnapshot(self, request, context):
        async with self.sessionmaker() as session:
            devices = await DeviceRepo(session).list_for_site(request.site_id)
            # If no devices found by site_id, fall back to listing all devices
            # (lab/test environments may not have matching site_ids).
            if not devices:
                all_devs = (
                    await session.execute(select(Device).order_by(Device.agent_name))
                ).scalars().all()
                devices = all_devs

            fleet_devices = []
            for device in devices:
                status = await StatusRepo(session).get(device.id)
                obs = observation_state(
                    status.last_heartbeat_at if status else None, self.config,
                )
                # Subsystem rollup
                subs = (
                    await session.execute(
                        select(DeviceSubsystemState).where(
                            DeviceSubsystemState.device_id == device.id
                        )
                    )
                ).scalars().all()
                subsystem_map = {s.subsystem: s.severity for s in subs}
                if obs == "observed" and subsystem_map:
                    health = _worst_severity(list(subsystem_map.values()))
                elif obs == "observed":
                    health = worst_health(status.last_health if status else None)
                else:
                    health = "unknown"

                fleet_devices.append(
                    harkeniq_pb2.FleetDevice(
                        agent_id=device.agent_id,
                        agent_name=device.agent_name,
                        vendor=device.vendor,
                        model=device.model,
                        observation=obs,
                        health=health,
                        subsystems_json=json.dumps(subsystem_map) if subsystem_map else "{}",
                        last_seen_unix=_ts(device.last_seen_at),
                    )
                )

            # Open incidents
            open_incidents = await IncidentRepo(session).list_open()
            fleet_incidents = []
            for inc in open_incidents:
                # Resolve device_agent_id from device_id
                device_agent_id = ""
                if inc.device_id:
                    dev = await DeviceRepo(session).get(inc.device_id)
                    if dev:
                        device_agent_id = dev.agent_id
                fleet_incidents.append(
                    harkeniq_pb2.FleetIncident(
                        incident_id=inc.id,
                        kind=inc.kind,
                        status=inc.status,
                        title=inc.title,
                        device_agent_id=device_agent_id,
                        subsystem=inc.subsystem or "",
                        opened_at_unix=_ts(inc.opened_at),
                    )
                )

            # Pending actions
            pending_actions = await ActionRepo(session).list_by_status("pending")
            fleet_actions = []
            for act in pending_actions:
                dev = await DeviceRepo(session).get(act.device_id)
                device_agent_id = dev.agent_id if dev else ""
                proposed_at_unix = 0
                if act.proposed_at:
                    try:
                        dt = datetime.fromisoformat(act.proposed_at)
                        proposed_at_unix = _ts(dt)
                    except (ValueError, TypeError):
                        pass
                fleet_actions.append(
                    harkeniq_pb2.FleetAction(
                        action_id=act.id,
                        type=act.type,
                        device_agent_id=device_agent_id,
                        severity=act.verdict_severity,
                        skill_name=act.skill_name,
                        status=act.status,
                        proposed_at_unix=proposed_at_unix,
                    )
                )

        return harkeniq_pb2.FleetSnapshot(
            devices=fleet_devices,
            incidents=fleet_incidents,
            pending_actions=fleet_actions,
            snapshot_at_unix=int(time.time()),
        )

    async def RouteApproval(self, request, context):
        try:
            if request.decision == "approved":
                await self.approvals.approve(request.action_id, request.decided_by)
            elif request.decision == "denied":
                await self.approvals.deny(request.action_id, request.decided_by)
            else:
                return harkeniq_pb2.ApprovalRouteAck(
                    accepted=False,
                    delivered=False,
                    reason=f"unknown decision: {request.decision!r}",
                )
            return harkeniq_pb2.ApprovalRouteAck(accepted=True, delivered=True)
        except Exception as exc:
            logger.warning("RouteApproval failed: %s", exc)
            return harkeniq_pb2.ApprovalRouteAck(
                accepted=False, delivered=False, reason=str(exc),
            )

    async def GetUsageSnapshot(self, request, context):
        async with self.sessionmaker() as session:
            # Count unique devices
            node_count_result = await session.execute(
                select(func.count(Device.id))
            )
            node_count = node_count_result.scalar() or 0

            # Agent versions from AgentStatus.last_state (placeholder: version
            # info isn't yet tracked per-agent; return state distribution instead)
            agent_versions: dict[str, int] = {}
            statuses = await StatusRepo(session).list_all()
            for s in statuses:
                ver = s.last_state or "unknown"
                agent_versions[ver] = agent_versions.get(ver, 0) + 1

        return harkeniq_pb2.UsageSnapshot(
            tenant_id=request.tenant_id,
            site_id=request.site_id,
            date=request.date,
            node_count=node_count,
            agent_versions=agent_versions,
        )

    async def PushPolicy(self, request, context):
        logger.info(
            "PushPolicy from CC: tenant=%s site=%s policies=%d bytes budgets=%d bytes",
            request.tenant_id, request.site_id,
            len(request.approval_policies_json),
            len(request.autonomy_budgets_json),
        )
        # R3: store and enforce; for now, log and acknowledge.
        return harkeniq_pb2.PolicyAck(accepted=True)


def build_server(
    config: SMConfig,
    servicer: AgentServiceServicer,
    sm_servicer: Optional[SiteManagerServiceServicer] = None,
) -> tuple[grpc.aio.Server, int]:
    """Create the server and bind its port; returns (server, bound_port)."""
    interceptors = []
    if config.site_token:
        interceptors.append(BearerTokenInterceptor(config.site_token))
    elif not config.insecure:
        raise ValueError("site_token required unless insecure=true")

    server = grpc.aio.server(interceptors=interceptors)
    harkeniq_pb2_grpc.add_AgentServiceServicer_to_server(servicer, server)
    if sm_servicer is not None:
        harkeniq_pb2_grpc.add_SiteManagerServiceServicer_to_server(sm_servicer, server)
    address = f"{config.grpc_host}:{config.grpc_port}"

    if config.insecure and not (config.tls_cert and config.tls_key):
        port = server.add_insecure_port(address)
        logger.warning("gRPC listening INSECURE on %s (lab mode)", address)
    else:
        creds = grpc.ssl_server_credentials(
            [
                (
                    Path(config.tls_key).read_bytes(),
                    Path(config.tls_cert).read_bytes(),
                )
            ]
        )
        port = server.add_secure_port(address, creds)
        logger.info("gRPC listening (TLS) on %s", address)
    return server, port
