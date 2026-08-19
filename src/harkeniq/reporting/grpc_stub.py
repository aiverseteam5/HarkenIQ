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
from typing import Optional

import grpc

from harkeniq.models import Verdict
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

    async def send_heartbeat(self, state: str, health_summary: dict[str, str]) -> bool:
        """Send an agent heartbeat. Returns True when acked; False when dropped."""
        if not self.enabled:
            return False
        request = harkeniq_pb2.AgentHeartbeat(
            agent_id=self.agent_id,
            agent_name=self.agent_name,
            state=state,
            health_summary=dict(health_summary),
            timestamp_unix=int(time.time()),
        )
        return await self._call("Heartbeat", request)

    async def close(self) -> None:
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
            self._stub = None

    # -- internals ----------------------------------------------------------

    def _ensure_stub(self) -> harkeniq_pb2_grpc.AgentServiceStub:
        if self._stub is None:
            self._channel = grpc.aio.insecure_channel(self.target)
            self._stub = harkeniq_pb2_grpc.AgentServiceStub(self._channel)
        return self._stub

    async def _call(self, method: str, request) -> bool:
        now = self._clock()
        if now < self._next_attempt:
            self.dropped += 1
            return False
        stub = self._ensure_stub()
        try:
            ack = await getattr(stub, method)(request, timeout=self.request_timeout)
        except grpc.aio.AioRpcError as e:
            delay = BACKOFF_SCHEDULE[min(self._backoff_index, len(BACKOFF_SCHEDULE) - 1)]
            self._backoff_index = min(self._backoff_index + 1, len(BACKOFF_SCHEDULE) - 1)
            self._next_attempt = now + delay
            self.dropped += 1
            logger.warning(
                "Site Manager %s unreachable (%s), dropping %s; next attempt in %.0fs",
                self.target, e.code().name, method, delay,
            )
            return False
        self._backoff_index = 0
        self._next_attempt = 0.0
        return bool(ack.accepted)
