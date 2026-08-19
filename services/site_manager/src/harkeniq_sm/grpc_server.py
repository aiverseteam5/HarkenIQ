"""gRPC receiver: agent-initiated RPCs only (spec §7 — SM never dials).

TLS server credentials + bearer-token interceptor unless the config is
explicitly insecure (lab / unit tests). Action RPCs delegate to the
approvals service once it is attached; until then they are politely
refused so agents keep retrying without error noise.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import grpc

from harkeniq.proto import harkeniq_pb2, harkeniq_pb2_grpc
from harkeniq_sm.auth import BearerTokenInterceptor
from harkeniq_sm.config import SMConfig
from harkeniq_sm.ingest import IngestService

logger = logging.getLogger("harkeniq.sm.grpc")


class AgentServiceServicer(harkeniq_pb2_grpc.AgentServiceServicer):
    def __init__(self, ingest: IngestService, approvals=None) -> None:
        self.ingest = ingest
        self.approvals = approvals  # attached by the approvals phase

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
        return harkeniq_pb2.RegistrationAck(accepted=True, site_name=site_name)

    async def Heartbeat(self, request, context):
        accepted = await self.ingest.heartbeat(
            agent_id=request.agent_id,
            agent_name=request.agent_name,
            state=request.state,
            health_summary=dict(request.health_summary),
            peer_status=dict(request.peer_status),
        )
        return harkeniq_pb2.HeartbeatAck(accepted=accepted)

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


def build_server(
    config: SMConfig, servicer: AgentServiceServicer
) -> tuple[grpc.aio.Server, int]:
    """Create the server and bind its port; returns (server, bound_port)."""
    interceptors = []
    if config.site_token:
        interceptors.append(BearerTokenInterceptor(config.site_token))
    elif not config.insecure:
        raise ValueError("site_token required unless insecure=true")

    server = grpc.aio.server(interceptors=interceptors)
    harkeniq_pb2_grpc.add_AgentServiceServicer_to_server(servicer, server)
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
