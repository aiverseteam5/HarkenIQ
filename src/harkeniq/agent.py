"""Agent orchestrator (Doc 06 §2.2, Doc 10 §2.1).

Wires poller -> baseline -> skill evaluation -> trending -> debounce ->
verdicts, drives the 7-state machine, checkpoints to SQLite, and runs
the continuous asyncio loop: sensor polling, peer heartbeats (UDP),
Site Manager reporting, and the action approval/execution pipeline.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import signal
import time
from datetime import datetime, timezone
from typing import Any, Optional

from harkeniq.actions.executor import ActionExecutor
from harkeniq.actions.queue import ActionQueue
from harkeniq.autonomy.identity import AgentIdentity
from harkeniq.autonomy.lease import AuthorizationLease, InvalidLease
from harkeniq.autonomy.tier import TierLevel, calculate_tier
from harkeniq.errors import ConfigError, HarkenIQError, HeartbeatError
from harkeniq.heartbeat.protocol import build_packet, parse_packet
from harkeniq.heartbeat.tracker import PeerTracker
from harkeniq.models import (
    Action,
    ActionStatus,
    AgentState,
    HeartbeatPacket,
    Verdict,
    VerdictSeverity,
)
from harkeniq.poller import Poller
from harkeniq.redfish.client import RedfishClient
from harkeniq.reporting.grpc_stub import SiteManagerReporter
from harkeniq.skills.engine import _TARGET_COLLECTIONS, SkillEngine
from harkeniq.skills.loader import load_skills
from harkeniq.skills.trending import TrendingEngine
from harkeniq.state.checkpoint import CheckpointManager
from harkeniq.state.machine import StateMachine

logger = logging.getLogger("harkeniq.agent")

DEFAULT_CHECKPOINT_INTERVAL = 600
DEFAULT_REPORT_INTERVAL = 60
#: Consecutive poll failures before escalating to ERROR (Doc 06 §15.1).
POLL_FAILURE_ERROR_THRESHOLD = 5

_SEVERITY_RANK = {
    VerdictSeverity.HEALTHY: 0,
    VerdictSeverity.UNKNOWN: 1,
    VerdictSeverity.TRENDING: 2,
    VerdictSeverity.WARNING: 3,
    VerdictSeverity.CRITICAL: 4,
}

#: Shortest legal path back to OBSERVING from any mid-cycle state.
_RECOVERY_PATH = {
    AgentState.EVALUATING: AgentState.DECIDING,
    AgentState.DECIDING: AgentState.OBSERVING,
    AgentState.AWAITING_AUTH: AgentState.REPORTING,
    AgentState.ACTING: AgentState.REPORTING,
    AgentState.REPORTING: AgentState.OBSERVING,
}


def _iso(ts_unix: float) -> str:
    return datetime.fromtimestamp(ts_unix, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


class _HeartbeatProtocol(asyncio.DatagramProtocol):
    """UDP endpoint: forwards received datagrams to the agent."""

    def __init__(self, agent: "Agent") -> None:
        self.agent = agent

    def datagram_received(self, data: bytes, addr) -> None:
        self.agent._on_heartbeat_datagram(data, addr)


class Agent:
    """Top-level agent orchestrator (composition root)."""

    def __init__(self, config: dict) -> None:
        bmc = config.get("bmc") or {}
        if not bmc.get("host"):
            raise ConfigError("bmc.host is required")
        self.config = config
        agent_cfg = config.get("agent") or {}
        self.agent_id: str = agent_cfg.get("id", "harkeniq-agent")
        self.agent_name: str = agent_cfg.get("name", "")
        self.skills_dir: str = (config.get("skills") or {}).get("directory", "skills")
        checkpoint_cfg = config.get("checkpoint") or {}
        self._checkpoint_path: Optional[str] = checkpoint_cfg.get("path") or None
        self._checkpoint_interval: float = checkpoint_cfg.get(
            "interval", DEFAULT_CHECKPOINT_INTERVAL
        )

        self.state_machine = StateMachine()
        self.client: Optional[RedfishClient] = None
        self.poller: Optional[Poller] = None
        self.skill_engine: Optional[SkillEngine] = None
        self.checkpoint: Optional[CheckpointManager] = None
        self.tracker: Optional[PeerTracker] = None
        self.action_queue = ActionQueue()
        self.executor: Optional[ActionExecutor] = None
        self.reporter: Optional[SiteManagerReporter] = None
        self.device_identity: Any = None  # Redfish device info (vendor, model, etc.)

        # R3a: agent cryptographic identity + authorization lease
        self.agent_identity: Optional[AgentIdentity] = None
        self.current_lease: Optional[AuthorizationLease] = None
        self.current_tier: TierLevel = TierLevel.T2
        self._sm_connected: bool = False
        self._sm_last_contact: float = 0.0

        self._last_device: Any = None
        self._last_verdicts: list[Verdict] = []
        self._last_checkpoint_at: float = 0.0
        self._running = False
        self._shutdown = asyncio.Event()
        self._hb_transport: Optional[asyncio.DatagramTransport] = None
        self._hb_seq = 0
        self._poll_failures = 0
        self._reported_severity: dict[str, VerdictSeverity] = {}
        self._reported_action_status: dict[str, str] = {}

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Startup sequence (Doc 06 §14), ends in OBSERVING."""
        bmc = self.config["bmc"]

        # Connect to BMC and detect vendor
        self.client = RedfishClient(
            host=bmc["host"], verify_ssl=bmc.get("verify_ssl", False)
        )
        await self.client.connect(bmc.get("username", ""), bmc.get("password", ""))
        self.poller = Poller(self.client)
        self.device_identity = await self.poller.detect()

        # Load skills
        skills = load_skills(self.skills_dir)

        # Peer tracker + Site Manager reporter + action executor
        self.tracker = PeerTracker(self.config)
        self.reporter = SiteManagerReporter(self.config)
        self.executor = ActionExecutor(
            self.client, self.device_identity.vendor, self.config, checkpoint=None
        )

        # Restore checkpoint (baselines, peers, actions survive restarts)
        trending = TrendingEngine(self.config)
        if self._checkpoint_path:
            self.checkpoint = CheckpointManager(self._checkpoint_path)
            self.executor.checkpoint = self.checkpoint
            state = await self.checkpoint.load_checkpoint()
            if state["baselines"]:
                trending.restore_baselines(state["baselines"])
                logger.info("Restored %d baselines from checkpoint",
                            len(state["baselines"]))
            if state["peers"]:
                self.tracker.restore_peers(state["peers"])
            self.action_queue.restore(await self.checkpoint.load_actions())

        self.skill_engine = SkillEngine(
            list(skills.values()),
            self.config.get("debounce"),
            trending,
        )

        # R3a: load or generate agent cryptographic identity.
        # Only activate key-derived agent_id when checkpoint is enabled
        # (persistent identity). Without checkpoint, keep config-based ID
        # for backward compatibility with standalone and test modes.
        if self.checkpoint:
            self.agent_identity = AgentIdentity.load(
                self.checkpoint.conn, self._checkpoint_path or ""
            )
            if self.agent_identity is None:
                self.agent_identity = AgentIdentity.generate()
                self.agent_identity.save(self.checkpoint.conn, self._checkpoint_path or "")
                logger.info("New agent identity: %s", self.agent_identity.agent_id)
            # Use key-derived agent_id for SM communication
            self.agent_id = self.agent_identity.agent_id
            if self.reporter:
                self.reporter.agent_id = self.agent_id

        # Best-effort Site Manager registration (R2a + R3a identity):
        # standalone Observe mode has no Site Manager at all.
        if self.reporter and self.reporter.enabled:
            peers = [
                f"{p.get('host', '')}:{p.get('port', 5150)}"
                for p in (self.config.get("peers") or [])
                if isinstance(p, dict) and p.get("host")
            ]
            reg_ack = await self.reporter.register_agent(
                vendor=self.device_identity.vendor,
                model=self.device_identity.model,
                service_tag=self.device_identity.service_tag,
                peers=peers,
                public_key_pem=(
                    self.agent_identity.public_key_pem
                    if self.agent_identity else b""
                ),
            )
            if reg_ack is None:
                logger.warning("Site Manager registration failed; continuing standalone")
            elif reg_ack.sm_public_key_pem and self.agent_identity:
                # R3a: pin SM public key and store certificate
                self.agent_identity.set_sm_public_key(bytes(reg_ack.sm_public_key_pem))
                self.agent_identity.sm_certificate = bytes(reg_ack.agent_certificate)
                if self.checkpoint:
                    self.agent_identity.save(
                        self.checkpoint.conn, self._checkpoint_path or ""
                    )
                logger.info("SM identity bootstrapped for agent %s", self.agent_id)

        self.state_machine.transition(
            AgentState.OBSERVING,
            f"Startup complete: {self.device_identity.model}, {len(skills)} skills loaded",
        )
        self._running = True

    async def stop(self) -> None:
        """Graceful shutdown: final checkpoint, close connections."""
        self._running = False
        if self.checkpoint:
            await self._write_checkpoint(force=True)
            await self.checkpoint.close()
            self.checkpoint = None
        if self.reporter:
            await self.reporter.close()
        if self.client:
            await self.client.close()
            self.client = None
        logger.info("Agent stopped")

    # -- continuous run loop (Doc 06 §2, §15) --------------------------------

    def request_shutdown(self) -> None:
        """Ask the run loop to stop (signal handlers, TUI 'q', tests)."""
        self._shutdown.set()

    async def run(self, install_signal_handlers: bool = True) -> None:
        """Run continuously until SIGTERM/SIGINT or request_shutdown()."""
        if not self._running:
            await self.start()
        self._shutdown = asyncio.Event()

        loop = asyncio.get_running_loop()
        if install_signal_handlers:
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, self.request_shutdown)
            loop.add_signal_handler(signal.SIGHUP, self._on_sighup)

        peers_configured = bool(self.config.get("peers"))
        if peers_configured:
            await self._open_heartbeat_endpoint()

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._poll_loop(), name="poller")
                if peers_configured:
                    tg.create_task(self._heartbeat_send_loop(), name="heartbeat")
                    tg.create_task(self._liveness_loop(), name="liveness")
                if self.reporter and self.reporter.enabled:
                    tg.create_task(self._report_loop(), name="reporter")
        finally:
            await self._shutdown_sequence(install_signal_handlers, loop)

    async def _shutdown_sequence(self, remove_handlers: bool, loop) -> None:
        logger.info("Agent shutting down")
        if self._hb_transport is not None:
            self._send_heartbeat(state="SHUTTING_DOWN")  # final heartbeat (Doc 06 §15.2)
            self._hb_transport.close()
            self._hb_transport = None
        if self.reporter and self.reporter.enabled:
            await self.reporter.send_heartbeat("SHUTTING_DOWN", self.health_summary())
        if remove_handlers:
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                loop.remove_signal_handler(sig)
        await self.stop()

    def _on_sighup(self) -> None:
        """SIGHUP: hot-reload skill files (Doc 06 §15.3)."""
        try:
            self.reload_skills()
        except HarkenIQError as e:
            logger.error("Skill reload failed, keeping previous skills: %s", e)

    async def _pause(self, seconds: float) -> bool:
        """Sleep unless shutdown is requested; True when shutting down."""
        try:
            await asyncio.wait_for(self._shutdown.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    # -- poller loop ---------------------------------------------------------

    async def _poll_loop(self) -> None:
        interval = (self.config.get("polling") or {}).get("sensor_interval", 60)
        while not self._shutdown.is_set():
            try:
                await self.poll_and_evaluate()
                self._poll_failures = 0
            except (HarkenIQError, RuntimeError) as e:
                self._poll_failures += 1
                self._recover_to_observing(f"poll failed: {e}")
                if self._poll_failures >= POLL_FAILURE_ERROR_THRESHOLD:
                    logger.error(
                        "Sensor polling failed %d consecutive times: %s",
                        self._poll_failures, e,
                    )
                else:
                    logger.warning(
                        "Sensor poll failed (%d consecutive): %s",
                        self._poll_failures, e,
                    )
            if await self._pause(interval):
                break

    def _recover_to_observing(self, reason: str) -> None:
        """Walk the state machine back to OBSERVING after a failed cycle."""
        sm = self.state_machine
        while sm.current_state in _RECOVERY_PATH:
            sm.transition(_RECOVERY_PATH[sm.current_state], reason)

    # -- heartbeat loops (Doc 06 §9) -----------------------------------------

    async def _open_heartbeat_endpoint(self) -> None:
        hb = self.config.get("heartbeat") or {}
        loop = asyncio.get_running_loop()
        self._hb_transport, _ = await loop.create_datagram_endpoint(
            lambda: _HeartbeatProtocol(self),
            local_addr=("0.0.0.0", hb.get("port", 5150)),
        )
        logger.info("Heartbeat UDP endpoint listening on port %d", hb.get("port", 5150))

    def _on_heartbeat_datagram(self, data: bytes, addr) -> None:
        secret = (self.config.get("heartbeat") or {}).get("secret", "")
        try:
            packet = parse_packet(data, secret)
        except HeartbeatError as e:
            logger.warning("Invalid heartbeat from %s: %s", addr[0], e)
            return
        if self.tracker:
            self.tracker.record_heartbeat(packet, addr[0], now=time.time())

    def _send_heartbeat(self, state: Optional[str] = None) -> None:
        if self._hb_transport is None or self._hb_transport.is_closing():
            return
        hb = self.config.get("heartbeat") or {}
        self._hb_seq += 1
        packet = HeartbeatPacket(
            v=1,
            agent_id=self.agent_id,
            name=self.agent_name,
            seq=self._hb_seq,
            ts=time.time(),
            state=state or self.state_machine.current_state.value,
            health_summary=self.health_summary(),
        )
        try:
            data = build_packet(packet, hb.get("secret", ""))
        except HeartbeatError as e:
            logger.warning("Cannot build heartbeat packet: %s", e)
            return
        for peer in self.tracker.get_peers():
            self._hb_transport.sendto(data, (peer.host, peer.port))

    async def _heartbeat_send_loop(self) -> None:
        interval = (self.config.get("heartbeat") or {}).get("interval", 10)
        while not self._shutdown.is_set():
            self._send_heartbeat()
            if await self._pause(interval):
                break

    async def _liveness_loop(self) -> None:
        interval = (self.config.get("heartbeat") or {}).get("interval", 10)
        while not self._shutdown.is_set():
            if await self._pause(interval):
                break
            self.tracker.check_liveness(now=time.time())

    # -- R3a: lease and tier management -----------------------------------------

    def _process_heartbeat_ack(self, ack) -> None:
        """Extract and verify authorization lease from HeartbeatAck."""
        if ack is None:
            self._sm_connected = False
            return

        self._sm_connected = True
        self._sm_last_contact = time.time()

        # Parse lease if present (R3a SM sends lease; old SM sends empty bytes)
        if not ack.authorization_lease:
            return
        if self.agent_identity is None or not self.agent_identity.is_valid():
            return

        try:
            self.current_lease = AuthorizationLease.parse(
                bytes(ack.authorization_lease), self.agent_identity
            )
            logger.debug(
                "Lease renewed: expiry=%s, actions=%s",
                self.current_lease.lease_expiry,
                self.current_lease.action_classes,
            )
        except InvalidLease as e:
            logger.warning("Invalid lease from SM: %s", e)

    def _update_tier(self) -> None:
        """Recalculate tier from current peer liveness state."""
        if self.tracker:
            self.current_tier = calculate_tier(self.tracker.get_peers())

    # -- Site Manager report loop (Doc 06 §10) -------------------------------

    async def _report_loop(self) -> None:
        sm = self.config.get("site_manager") or {}
        interval = sm.get("heartbeat_interval", DEFAULT_REPORT_INTERVAL)
        poll_interval = sm.get("action_poll_interval", 5)
        while not self._shutdown.is_set():
            hb_ack = await self.reporter.send_heartbeat(
                self.state_machine.current_state.value,
                self.health_summary(),
                self._peer_status(),
            )
            # R3a: process authorization lease from heartbeat ack
            self._process_heartbeat_ack(hb_ack)
            await self._report_changed_verdicts()
            await self._sync_actions()
            # Approvals must land "in seconds": poll fast while any action
            # is awaiting a decision or awaiting execution.
            pause = poll_interval if self._actions_in_flight() else interval
            if await self._pause(pause):
                break

    def _peer_status(self) -> dict[str, str]:
        """Peer liveness map for SM heartbeats (network-vs-device quorum)."""
        if self.tracker is None:
            return {}
        return {
            (p.peer_id or f"{p.host}:{p.port}"): p.status.value
            for p in self.tracker.get_peers()
        }

    async def _report_changed_verdicts(self) -> None:
        for verdict in self._last_verdicts:
            if self._reported_severity.get(verdict.sensor_id) == verdict.severity:
                continue
            if await self.reporter.report_verdict(verdict):
                self._reported_severity[verdict.sensor_id] = verdict.severity

    def _actions_in_flight(self) -> bool:
        return any(
            a.status in (ActionStatus.PENDING, ActionStatus.APPROVED)
            for a in self.action_queue.all()
        )

    async def _sync_actions(self) -> None:
        """Action lifecycle sync with the Site Manager (R-S6, D16).

        Reports status changes (deduplicated), then applies brokered
        decisions. First decider wins: if a CLI approval/denial already
        landed in the checkpoint, the SM decision is skipped and the SM
        learns the final status on the next report.
        """
        for action in self.action_queue.all():
            status = action.status.value
            if self._reported_action_status.get(action.id) == status:
                continue
            if await self.reporter.report_action(action):
                self._reported_action_status[action.id] = status

        decisions = await self.reporter.poll_decisions()
        if not decisions:
            return
        # Pick up any out-of-band CLI decisions before applying ours.
        if self.checkpoint:
            self.action_queue.restore(await self.checkpoint.load_actions())
        applied = False
        for decision in decisions:
            action = self.action_queue.get(decision.action_id)
            if action is None or action.status != ActionStatus.PENDING:
                continue  # CLI raced first; next report shows the final status
            if decision.decision == "approved":
                self.action_queue.approve(decision.action_id)
            else:
                self.action_queue.deny(decision.action_id)
            applied = True
            logger.info(
                "Site Manager decision applied: %s %s by %s",
                decision.action_id, decision.decision, decision.decided_by,
            )
            if self.checkpoint:
                await self.checkpoint.save_audit_entry(
                    action=action.type.value,
                    target=action.params.get("target", "") or action.sensor_id,
                    outcome=decision.decision,
                    authorization=f"sm:{decision.decided_by}",
                    evidence_json=json.dumps(
                        {"action_id": action.id, "sensor_id": action.sensor_id}
                    ),
                )
        if applied and self.checkpoint:
            await self.checkpoint.save_actions(self.action_queue.all())

    # -- main cycle ----------------------------------------------------------

    async def poll_and_evaluate(self, timestamp: Optional[float] = None) -> list[Verdict]:
        """One full cycle: poll -> evaluate -> decide -> act -> report -> observe.

        Drives OBSERVING -> EVALUATING -> DECIDING -> (AWAITING_AUTH ->
        [ACTING ->] REPORTING ->) OBSERVING and returns the cycle's verdicts.
        """
        if self.state_machine.current_state != AgentState.OBSERVING:
            raise RuntimeError(
                f"poll_and_evaluate requires OBSERVING state, "
                f"currently {self.state_machine.current_state.value}"
            )
        ts = timestamp if timestamp is not None else time.time()

        device = await self.poller.poll_sensors()
        self._last_device = device
        self.state_machine.transition(AgentState.EVALUATING, "sensor poll complete")

        verdicts = await self.skill_engine.evaluate(device, ts)
        self._last_verdicts = verdicts
        self.state_machine.transition(AgentState.DECIDING, "verdicts produced")

        # Pick up out-of-band approvals/denials (harken action approve/deny)
        if self.checkpoint:
            self.action_queue.restore(await self.checkpoint.load_actions())

        proposed = await self._propose_actions(self.skill_engine.get_pending_actions())
        approved = self.action_queue.approved()

        if proposed or approved:
            self.state_machine.transition(
                AgentState.AWAITING_AUTH,
                f"{len(proposed) + len(approved)} action(s) in approval pipeline",
            )
            if approved:
                self.state_machine.transition(
                    AgentState.ACTING, f"executing {len(approved)} approved action(s)"
                )
                for action in approved:
                    await self.executor.execute(action)
                self.state_machine.transition(AgentState.REPORTING, "actions executed")
            else:
                self.state_machine.transition(
                    AgentState.REPORTING, "awaiting operator approval"
                )
            if self.checkpoint:
                await self.checkpoint.save_actions(self.action_queue.all())
            self.state_machine.transition(AgentState.OBSERVING, "report logged")
        else:
            self.state_machine.transition(AgentState.OBSERVING, "no action needed")

        if self.checkpoint and ts - self._last_checkpoint_at >= self._checkpoint_interval:
            await self._write_checkpoint(now=ts)

        return verdicts

    async def _propose_actions(self, recommendations: list[Action]) -> list[Action]:
        """Enqueue newly recommended actions (deduplicated), audit as proposed."""
        proposed: list[Action] = []
        for rec in recommendations:
            action = self.action_queue.enqueue(
                rec.type, rec.sensor_id, rec.skill_name, rec.verdict_severity,
                rec.params,
            )
            if action is None:
                continue
            proposed.append(action)
            logger.info(
                "Action proposed: %s %s on %s (%s)",
                action.id, action.type.value, action.sensor_id, action.skill_name,
            )
            if self.checkpoint:
                await self.checkpoint.save_audit_entry(
                    action=action.type.value,
                    target=action.sensor_id,
                    outcome="proposed",
                )
        return proposed

    def get_pending_actions(self) -> list[Action]:
        """Actions awaiting operator approval."""
        return self.action_queue.pending()

    def health_summary(self) -> dict[str, str]:
        """Per-subsystem worst-verdict summary for heartbeats (Doc 06 §9.2)."""
        summary: dict[str, VerdictSeverity] = {
            t: VerdictSeverity.HEALTHY for t in _TARGET_COLLECTIONS
        }
        for verdict in self._last_verdicts:
            target = verdict.sensor_id.split(":", 1)[0]
            if target in summary and (
                _SEVERITY_RANK[verdict.severity] > _SEVERITY_RANK[summary[target]]
            ):
                summary[target] = verdict.severity
        return {
            t: "OK" if sev == VerdictSeverity.HEALTHY else sev.value
            for t, sev in summary.items()
        }

    def reload_skills(self) -> None:
        """Reload skill files from disk (SIGHUP semantics)."""
        skills = load_skills(self.skills_dir)
        self.skill_engine.reload_skills(list(skills.values()))

    # -- checkpointing ------------------------------------------------------

    async def checkpoint_now(self) -> None:
        """Force an immediate checkpoint write."""
        await self._write_checkpoint(force=True)

    async def _write_checkpoint(self, now: Optional[float] = None, force: bool = False) -> None:
        if not self.checkpoint:
            return
        ts = now if now is not None else time.time()
        await self.checkpoint.save_checkpoint(
            sensor_readings=self._readings_from_device(self._last_device, _iso(ts)),
            baselines=self.skill_engine.trending.get_all_baselines()
            if self.skill_engine else {},
            verdicts=self._last_verdicts,
            peers=self.tracker.get_peers() if self.tracker else [],
            agent_meta={
                "agent_id": self.agent_id,
                "state": self.state_machine.current_state.value,
            },
            log_cursors={},
        )
        self._last_checkpoint_at = ts

    @staticmethod
    def _readings_from_device(device: Any, collected_at: str) -> dict[str, dict]:
        if device is None:
            return {}
        readings: dict[str, dict] = {}
        for target, attr in _TARGET_COLLECTIONS.items():
            for sensor in getattr(device, attr, []):
                readings[f"{target}:{sensor.name}"] = {
                    "sensor_type": target,
                    "reading": dataclasses.asdict(sensor),
                    "health": getattr(sensor, "health", "Unknown"),
                    "collected_at": collected_at,
                }
        return readings
