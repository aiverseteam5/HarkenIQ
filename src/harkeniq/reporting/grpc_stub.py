"""Site Manager gRPC reporter stub (Doc 06 §10.2).

Standalone mode (empty ``site_manager.host``) makes every call a no-op.
When configured, calls are non-blocking best-effort: on failure the
report is dropped (R1 tradeoff) and further attempts are suppressed
until an exponential backoff delay (5, 10, 30, 60, max 300s) elapses.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import grpc

from harkeniq.models import Action, Verdict
from harkeniq.proto import harkeniq_pb2, harkeniq_pb2_grpc

logger = logging.getLogger("harkeniq.reporting.grpc")

BACKOFF_SCHEDULE = (5.0, 10.0, 30.0, 60.0, 300.0)


def _iso_to_unix(ts: str) -> int:
    try:
        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (TypeError, ValueError):
        return int(time.time())


class SiteManagerReporter:
    """gRPC client for the Site Manager AgentService."""

    def __init__(self, config: Optional[dict] = None, request_timeout: float = 5.0) -> None:
        config = config or {}
        sm = config.get("site_manager") or {}
        agent_cfg = config.get("agent") or {}
        self.host: str = sm.get("host", "")
        self.port: int = sm.get("port", 50051)
        self.agent_id: str = agent_cfg.get("id", "")
        self.agent_name: str = agent_cfg.get("name", "")
        self.token: str = sm.get("token", "")
        # E1.3: the SITE-BOUND enrollment credential. The Site Manager
        # resolves it to exactly one site; this agent never names a site
        # and cannot choose one. Absent is legitimate on a Site Manager
        # serving a single site, which is how existing deployments keep
        # working without re-enrolling their fleets.
        self.enrollment_token: str = sm.get("enrollment_token", "")
        # TLS on by default when a CA is provided; bare test configs
        # (no tls/tls_ca keys) keep the R1 insecure-channel behavior.
        # QA-017: tls requested without a CA used to silently downgrade to
        # plaintext. Fail closed — config validation catches the normal
        # path; this guards direct constructions.
        tls_requested = bool(sm.get("tls", True))
        self.tls_ca: str = sm.get("tls_ca", "")
        if sm.get("host") and tls_requested and not self.tls_ca:
            raise ValueError(
                "site_manager.tls is true but tls_ca is empty — refusing to "
                "silently downgrade to plaintext (set tls_ca, or tls: false "
                "for lab use)"
            )
        self.tls: bool = tls_requested and bool(self.tls_ca)
        self.request_timeout = request_timeout
        self.enabled: bool = bool(self.host)
        self.dropped: int = 0

        self._channel: Optional[grpc.aio.Channel] = None
        self._stub: Optional[harkeniq_pb2_grpc.AgentServiceStub] = None
        self._backoff_index = 0
        self._next_attempt = 0.0
        self._clock = time.monotonic  # patchable in tests

    @property
    def target(self) -> str:
        return f"{self.host}:{self.port}"

    # -- public API ---------------------------------------------------------

    async def report_verdict(self, verdict: Verdict) -> bool:
        """Send a verdict report. Returns True when acked; False when dropped."""
        if not self.enabled:
            return False
        request = harkeniq_pb2.VerdictReport(
            agent_id=self.agent_id,
            sensor_id=verdict.sensor_id,
            skill_name=verdict.skill_name,
            verdict=verdict.severity.value,
            evidence_json=json.dumps({
                "message": verdict.message,
                "evidence": [dataclasses.asdict(e) for e in verdict.evidence],
            }),
            timestamp_unix=_iso_to_unix(verdict.timestamp),
        )
        return await self._call("ReportVerdict", request)

    async def send_heartbeat(
        self,
        state: str,
        health_summary: dict[str, str],
        peer_status: Optional[dict[str, str]] = None,
    ) -> Optional["harkeniq_pb2.HeartbeatAck"]:
        """Send an agent heartbeat.

        Returns the HeartbeatAck if successful (contains R3a lease),
        or None when dropped.
        """
        if not self.enabled:
            return None
        request = harkeniq_pb2.AgentHeartbeat(
            agent_id=self.agent_id,
            agent_name=self.agent_name,
            state=state,
            health_summary=dict(health_summary),
            timestamp_unix=int(time.time()),
            peer_status=dict(peer_status or {}),
        )
        return await self._call("Heartbeat", request, want_response=True)

    async def register_agent(
        self,
        vendor: str = "",
        model: str = "",
        service_tag: str = "",
        bmc_location: Optional[dict] = None,
        peers: Optional[list[str]] = None,
        public_key_pem: Optional[bytes] = None,
        firmware: Optional[list[dict]] = None,
        device_class: str = "",
    ) -> Optional["harkeniq_pb2.RegistrationAck"]:
        """Register this agent with the Site Manager (best-effort).

        Returns the RegistrationAck if successful (needed for R3a identity
        bootstrapping), or None on failure.
        """
        if not self.enabled:
            return None
        request = harkeniq_pb2.AgentRegistration(
            agent_id=self.agent_id,
            agent_name=self.agent_name,
            vendor=vendor,
            model=model,
            service_tag=service_tag,
            bmc_location_json=json.dumps(bmc_location) if bmc_location else "",
            peers=list(peers or []),
            public_key_pem=public_key_pem or b"",
            firmware_json=json.dumps(firmware) if firmware else "",
            device_class=device_class,
            enrollment_token=self.enrollment_token,
        )
        ack = await self._call("RegisterAgent", request, want_response=True)
        if ack is not None and not ack.accepted:
            # E1.3: a refusal is a configuration problem an operator has
            # to see, not something to retry silently forever.
            logger.error(
                "Site Manager refused this agent's registration: %s",
                ack.reason or "no reason given",
            )
        return ack

    async def report_action(self, action: Action) -> bool:
        """Report an action's current state to the Site Manager."""
        if not self.enabled:
            return False
        request = harkeniq_pb2.ActionReport(
            agent_id=self.agent_id,
            action_id=action.id,
            type=action.type.value,
            sensor_id=action.sensor_id,
            skill_name=action.skill_name,
            verdict_severity=action.verdict_severity.value,
            params_json=json.dumps(action.params or {}),
            status=action.status.value,
            proposed_at=action.proposed_at or "",
            outcome_json=(
                # ActionOutcome carries enums (e.g. ActionType) — use .value
                json.dumps(
                    dataclasses.asdict(action.outcome),
                    default=lambda o: getattr(o, "value", str(o)),
                )
                if action.outcome else ""
            ),
        )
        return await self._call("ReportAction", request)

    async def poll_decisions(self) -> list:
        """Fetch pending approval decisions. Returns [] when unavailable."""
        if not self.enabled:
            return []
        request = harkeniq_pb2.DecisionPoll(agent_id=self.agent_id)
        response = await self._call("PollActionDecisions", request, want_response=True)
        if response is None:
            return []
        return list(response.decisions)

    async def poll_directives(self) -> list:
        """Fetch SM-initiated directives (R5). Returns [] when unavailable."""
        if not self.enabled:
            return []
        request = harkeniq_pb2.DirectivePoll(agent_id=self.agent_id)
        response = await self._call("PollDirectives", request, want_response=True)
        if response is None:
            return []
        return list(response.directives)

    async def report_directive_result(
        self, directive_id: str, success: bool, detail: str = ""
    ) -> bool:
        """Settle an executed directive with the SM (R5)."""
        if not self.enabled:
            return False
        request = harkeniq_pb2.DirectiveResult(
            agent_id=self.agent_id,
            directive_id=directive_id,
            success=success,
            detail=detail[:512],
        )
        return await self._call("ReportDirectiveResult", request)

    async def close(self) -> None:
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
            self._stub = None

    # -- internals ----------------------------------------------------------

    def _metadata(self) -> tuple:
        if self.token:
            return (("authorization", f"Bearer {self.token}"),)
        return ()

    def _ensure_stub(self) -> harkeniq_pb2_grpc.AgentServiceStub:
        if self._stub is None:
            if self.tls:
                ca_pem = Path(self.tls_ca).read_bytes()
                credentials = grpc.ssl_channel_credentials(root_certificates=ca_pem)
                self._channel = grpc.aio.secure_channel(self.target, credentials)
            else:
                self._channel = grpc.aio.insecure_channel(self.target)
            self._stub = harkeniq_pb2_grpc.AgentServiceStub(self._channel)
        return self._stub

    async def _call(self, method: str, request, want_response: bool = False):
        """Invoke ``method``; returns ack.accepted (bool) or, when
        ``want_response``, the full response object (None on failure)."""
        failure = None if want_response else False
        now = self._clock()
        if now < self._next_attempt:
            self.dropped += 1
            return failure
        try:
            stub = self._ensure_stub()
            response = await getattr(stub, method)(
                request, timeout=self.request_timeout, metadata=self._metadata()
            )
        except (grpc.aio.AioRpcError, OSError) as e:
            delay = BACKOFF_SCHEDULE[min(self._backoff_index, len(BACKOFF_SCHEDULE) - 1)]
            self._backoff_index = min(self._backoff_index + 1, len(BACKOFF_SCHEDULE) - 1)
            self._next_attempt = now + delay
            self.dropped += 1
            detail = e.code().name if isinstance(e, grpc.aio.AioRpcError) else str(e)
            logger.warning(
                "Site Manager %s unreachable (%s), dropping %s; next attempt in %.0fs",
                self.target, detail, method, delay,
            )
            return failure
        self._backoff_index = 0
        self._next_attempt = 0.0
        if want_response:
            return response
        return bool(response.accepted)
