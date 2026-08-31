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
from harkeniq.autonomy.blast_radius import BlastRadiusLimiter
from harkeniq.autonomy.budget import AgentBudgetEnforcer
from harkeniq.autonomy.claim import Claim, ClaimAck
from harkeniq.autonomy.identity import AgentIdentity
from harkeniq.autonomy.lease import AuthorizationLease, InvalidLease
from harkeniq.autonomy.partition_fence import PartitionFence
from harkeniq.autonomy.peer_keyring import PeerKeyRing
from harkeniq.autonomy.peer_protocol import PeerProtocol
from harkeniq.autonomy.preconditions import ACTION_RISK, check_preconditions
from harkeniq.autonomy.tier import TierLevel, calculate_tier
from harkeniq.autonomy.verification import (
    VERIFICATION_CHECKS,
    VERIFICATION_WINDOWS,
    OutcomeStatus,
    evaluate_verification,
)
from harkeniq.errors import ConfigError, HarkenIQError, HeartbeatError
from harkeniq.heartbeat.protocol import (
    MSG_CLAIM,
    MSG_CLAIM_ACK,
    MSG_HEARTBEAT,
    MSG_SUSPICION,
    build_envelope,
    build_packet,
    parse_envelope,
    parse_packet,
)
from harkeniq.heartbeat.tracker import PeerTracker
from harkeniq.models import (
    Action,
    ActionStatus,
    ActionType,
    AgentState,
    Evidence,
    HeartbeatPacket,
    Verdict,
    VerdictSeverity,
)
from harkeniq.poller import Poller
from harkeniq.protocols.device import create_device_protocol
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
        self.protocol: Any = None  # DeviceProtocol (R4-1)
        self.client: Optional[RedfishClient] = None  # legacy Redfish accessor
        self.poller: Optional[Poller] = None  # legacy Redfish accessor
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
        # QA-020: the R3a enforcement chain, finally instantiated. The
        # budget enforcer mirrors the lease (updated every heartbeat ack);
        # the blast-radius limiter rate-limits disruptive actions locally.
        self.budget: AgentBudgetEnforcer = AgentBudgetEnforcer()
        self.blast_radius: BlastRadiusLimiter = BlastRadiusLimiter()
        self._verification_tasks: set[asyncio.Task] = set()
        # R3b-2: mesh protocol components
        self.peer_keyring: PeerKeyRing = PeerKeyRing()
        self.peer_protocol: Optional[PeerProtocol] = None
        self.partition_fence: Optional[PartitionFence] = None

        # QA-025 / A2.5 / D12: resource monitor, profile from config
        # (HARKENIQ_RESOURCES_PROFILE finally does something).
        from harkeniq.autonomy.resources import ResourceMonitor

        profile = (config.get("resources") or {}).get("profile", "standard")
        self.resource_monitor: Optional[ResourceMonitor] = ResourceMonitor(profile)

        # R4-2 P13: config compliance state
        self.config_policies: dict[str, Any] = {}
        self._last_drift_findings: list[Any] = []
        # R4-2 P14: firmware inventory (R-AGENT-17)
        self.firmware_inventory: list[dict] = []
        # R5: directed directives from SM (in-flight dedup + task refs)
        self._directives_in_flight: set[str] = set()
        self._directive_tasks: set[asyncio.Task] = set()
        # QA-031: checkpoint-recovered playbook executions (id -> state)
        self.playbook_executions: dict[str, Any] = {}
        # QA-024 / A2.7: OS signals + BMC log poll (both finally wired)
        self.os_collector: Any = None
        self._os_signal_verdicts: dict[str, Verdict] = {}
        self._log_cursors: dict[str, str] = {}
        self._sel_events_forwarded = False

        self._last_device: Any = None
        self._last_verdicts: list[Verdict] = []
        self._last_checkpoint_at: float = 0.0
        self._running = False
        self._sm_registered = False  # QA-041: _report_loop retries until True
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

        # Connect to BMC via DeviceProtocol and detect vendor (R4-1).
        # Redfish is the default; bmc.protocol selects alternatives (ipmi).
        protocol_name = (bmc.get("protocol") or "redfish").lower()
        proto_kwargs: dict[str, Any] = {}
        if protocol_name == "redfish":
            proto_kwargs["verify_ssl"] = bmc.get("verify_ssl", False)
            # QA-023: the protocol's internal executor must enforce the
            # agent's configured allow list, not the full ActionType surface.
            actions_cfg = self.config.get("actions") or {}
            if actions_cfg.get("allow_list") is not None:
                proto_kwargs["allow_list"] = list(actions_cfg["allow_list"])
        elif protocol_name == "ipmi":
            # bmc.port defaults to 443 (Redfish); treat that as unset for IPMI.
            port = bmc.get("port")
            proto_kwargs["port"] = 623 if port in (None, 0, 443) else port
        elif protocol_name == "gnmi":
            # R6: bmc.port defaults to 443 (Redfish); gNMI default is 8080.
            port = bmc.get("port")
            proto_kwargs["port"] = 8080 if port in (None, 0, 443) else port
            proto_kwargs["plaintext"] = bool(bmc.get("plaintext", False))
        self.protocol = create_device_protocol(
            protocol_name, host=bmc["host"], **proto_kwargs
        )
        await self.protocol.connect(
            {"username": bmc.get("username", ""), "password": bmc.get("password", "")}
        )
        self.device_identity = await self.protocol.detect_identity()
        if protocol_name == "redfish":
            # Legacy accessors: existing code and tests reach the raw client.
            self.client = self.protocol.client
            self.poller = self.protocol.poller

        # R4-2 P14: collect firmware inventory at startup (R-AGENT-17)
        try:
            self.firmware_inventory = await self.protocol.collect_firmware_inventory()
        except Exception as e:
            logger.warning("Firmware inventory collection failed: %s", e)
            self.firmware_inventory = []

        # Load skills
        skills = load_skills(self.skills_dir)

        # R4-2 P13: load config compliance policies when enabled
        compliance_cfg = self.config.get("compliance") or {}
        if compliance_cfg.get("enabled"):
            from harkeniq.compliance.config_policy import load_config_policies
            self.config_policies = load_config_policies(
                compliance_cfg.get("policy_directory", "/etc/harkeniq/policies")
            )
            logger.info("Loaded %d config compliance policies",
                        len(self.config_policies))

        # QA-024 / A2.7: OS signal sources, auto-registered only when
        # their backing exists on this host (quiet in containers).
        os_cfg = self.config.get("os_signals") or {}
        if os_cfg.get("enabled", True):
            self.os_collector = self._build_os_collector()

        # Peer tracker + Site Manager reporter + action executor
        self.tracker = PeerTracker(self.config)
        self.reporter = SiteManagerReporter(self.config)
        self.executor = ActionExecutor(
            self.client, self.device_identity.vendor, self.config,
            checkpoint=None, protocol=self.protocol,
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
            # QA-024: SEL/IML cursors survive restarts (log_cursors was a
            # dead table until R7)
            self._log_cursors = dict(state.get("log_cursors", {}))
            self.action_queue.restore(await self.checkpoint.load_actions())
            # QA-031: playbook executions survive restarts. In-flight ones
            # come back PAUSED (unknowable step outcome), awaiting a
            # human-driven resume.
            from harkeniq.models import PlaybookStatus
            self.playbook_executions = {
                e.execution_id: e
                for e in await self.checkpoint.load_playbook_executions()
            }
            interrupted = [
                e.execution_id
                for e in self.playbook_executions.values()
                if e.status == PlaybookStatus.PAUSED
            ]
            if interrupted:
                logger.warning(
                    "Recovered %d paused playbook execution(s) from "
                    "checkpoint: %s", len(interrupted), interrupted,
                )

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
        # standalone Observe mode has no Site Manager at all. QA-041: a
        # startup failure is retried by _report_loop until it succeeds.
        if self.reporter and self.reporter.enabled:
            if not await self._register_with_sm():
                logger.warning("Site Manager registration failed; continuing standalone")

        self.state_machine.transition(
            AgentState.OBSERVING,
            f"Startup complete: {self.device_identity.model}, {len(skills)} skills loaded",
        )
        self._running = True

    def declare_capabilities(self) -> dict:
        """This node's capability declaration (Capability Registry).

        The AUTHORITATIVE statement of what this device can actually do,
        and the only one: the Site Manager stores it, Central Command
        caches it, /api/capabilities composes it and the Operational
        Agent validates against it -- none of them may declare a
        capability this node did not.

        Reach comes from the protocol's own code; the allow list comes
        from this node's config. Both are reported, because "no code for
        it" and "this node does not permit it" are different problems
        with different fixes, and an operator staring at a device that
        will not act needs to know which one they have.

        This declaration changes NOTHING about execution: the allow list
        consulted by ActionExecutor remains the final authority, exactly
        as before. Connecting Registry truth to the runtime
        authorization path is the named capability-execution-gate
        follow-up, deliberately not attempted here.
        """
        from harkeniq.capabilities import declare, protocol_reach_of

        actions_cfg = self.config.get("actions") or {}
        allow_list = actions_cfg.get("allow_list")
        if allow_list is None:
            from harkeniq.actions.executor import DEFAULT_ALLOW_LIST

            allow_list = list(DEFAULT_ALLOW_LIST)
        protocol_name = (
            getattr(self.protocol, "name", None)
            or (self.config.get("bmc") or {}).get("protocol")
            or "redfish"
        )
        declaration = declare(
            protocol_name,
            allow_list,
            device_class=getattr(
                self.device_identity, "device_class", "server"
            ),
        )
        # Prefer the LIVE protocol object's own declaration when there is
        # one: the instance is what will actually execute, and a build
        # where the factory and the instance disagree must report the
        # instance rather than a name lookup.
        if self.protocol is not None:
            reach = protocol_reach_of(self.protocol)
            if reach is not None:
                declaration["implemented"] = sorted(reach)
                declaration["effective"] = sorted(reach & set(declaration["allow_list"]))
                declaration["reach_known"] = True
        return declaration

    async def _register_with_sm(self) -> bool:
        """Register with the Site Manager; returns True on success.

        Sets ``_sm_registered`` so _report_loop stops retrying (QA-041:
        registration used to be fire-once — a transient SM/DB hiccup at
        boot left the agent lease-less until restart).
        """
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
            firmware=self.firmware_inventory,
            device_class=getattr(
                self.device_identity, "device_class", "server"
            ),
            capabilities=self.declare_capabilities(),
        )
        if reg_ack is None:
            return False
        self._sm_registered = True
        if reg_ack.sm_public_key_pem and self.agent_identity:
            # R3a: pin SM public key and store certificate
            self.agent_identity.set_sm_public_key(bytes(reg_ack.sm_public_key_pem))
            self.agent_identity.sm_certificate = bytes(reg_ack.agent_certificate)
            if self.checkpoint:
                self.agent_identity.save(
                    self.checkpoint.conn, self._checkpoint_path or ""
                )
            logger.info("SM identity bootstrapped for agent %s", self.agent_id)

            # R3b-2: load peer public keys from SM-signed bundle
            if reg_ack.peer_keys and reg_ack.peer_keys_signature:
                try:
                    from cryptography.hazmat.primitives import serialization as _ser
                    sm_pub = _ser.load_pem_public_key(
                        bytes(reg_ack.sm_public_key_pem)
                    )
                    peer_keys = {
                        k: bytes(v) for k, v in reg_ack.peer_keys.items()
                    }
                    loaded = self.peer_keyring.load_from_bundle(
                        peer_keys,
                        bytes(reg_ack.peer_keys_signature),
                        sm_pub,
                        exclude_self=self.agent_id,
                    )
                    logger.info("Loaded %d peer keys from SM", loaded)
                except Exception as e:
                    logger.warning("Peer key loading failed: %s", e)
        return True

    async def stop(self) -> None:
        """Graceful shutdown: final checkpoint, close connections."""
        self._running = False
        if self.checkpoint:
            await self._write_checkpoint(force=True)
            await self.checkpoint.close()
            self.checkpoint = None
        if self.reporter:
            await self.reporter.close()
        if self.protocol:
            await self.protocol.disconnect()
            self.protocol = None
            self.client = None
            self.poller = None
        elif self.client:
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

        compliance_cfg = self.config.get("compliance") or {}
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._poll_loop(), name="poller")
                if self.resource_monitor is not None:
                    tg.create_task(self._resource_loop(), name="resources")
                if peers_configured:
                    tg.create_task(self._heartbeat_send_loop(), name="heartbeat")
                    tg.create_task(self._liveness_loop(), name="liveness")
                if self.reporter and self.reporter.enabled:
                    tg.create_task(self._report_loop(), name="reporter")
                    tg.create_task(self._inventory_loop(), name="inventory")
                if compliance_cfg.get("enabled"):
                    tg.create_task(self._compliance_loop(), name="compliance")
                # QA-024: OS signals + BMC log poll (SEL/IML -> verdicts)
                if self.os_collector is not None or self.poller is not None:
                    tg.create_task(self._log_signals_loop(), name="os_signals")
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
            # QA-025 / A2.5: the resource monitor's degradation ladder
            # stretches the poll cadence under pressure (THROTTLED 2x,
            # DEGRADED 3x, OBSERVE_ONLY 4x); NORMAL is 1x.
            multiplier = (
                self.resource_monitor.poll_interval_multiplier
                if self.resource_monitor is not None else 1.0
            )
            if await self._pause(interval * multiplier):
                break

    async def _resource_loop(self) -> None:
        """A2.5 inner enforcement layer (QA-025: existed since R3a, wired
        now): sample RSS/CPU, walk the degradation ladder, surface level
        changes in the log. The outer layer stays systemd/container caps."""
        monitor = self.resource_monitor
        check_interval = (self.config.get("resources") or {}).get(
            "check_interval", 30
        )
        last_level = None
        while not self._shutdown.is_set():
            try:
                snapshot = monitor.measure()
                level = monitor.evaluate(snapshot)
                if level != last_level:
                    if last_level is None:
                        logger.info(
                            "Resource monitor active: profile=%s rss=%.1fMB",
                            monitor.profile.name, snapshot.memory_rss_mb,
                        )
                    else:
                        logger.warning(
                            "Resource level %s (rss=%.1fMB cpu=%.1f%%, profile=%s)",
                            level.name, snapshot.memory_rss_mb,
                            snapshot.cpu_percent, monitor.profile.name,
                        )
                    last_level = level
            except Exception as e:  # noqa: BLE001 — monitoring must not kill the agent
                logger.warning("Resource sampling failed: %s", e)
            if await self._pause(check_interval):
                break

    # -- firmware inventory (R4-2 P14, R-AGENT-17) ---------------------------

    async def _inventory_loop(self) -> None:
        """Re-collect firmware inventory on polling.inventory_interval and
        re-register with SM when it changed (registration is an upsert)."""
        interval = (self.config.get("polling") or {}).get("inventory_interval", 300)
        while not self._shutdown.is_set():
            if await self._pause(interval):
                break
            try:
                inventory = await self.protocol.collect_firmware_inventory()
            except Exception as e:
                logger.warning("Firmware inventory refresh failed: %s", e)
                continue
            if inventory != self.firmware_inventory:
                logger.info("Firmware inventory changed (%d components)",
                            len(inventory))
                self.firmware_inventory = inventory
                if self.reporter and self.reporter.enabled:
                    await self.reporter.register_agent(
                        vendor=self.device_identity.vendor,
                        model=self.device_identity.model,
                        service_tag=self.device_identity.service_tag,
                        firmware=inventory,
                    )

    # -- config compliance (R4-2 P13) ----------------------------------------

    async def _compliance_loop(self) -> None:
        compliance_cfg = self.config.get("compliance") or {}
        interval = compliance_cfg.get("interval", 3600)
        while not self._shutdown.is_set():
            try:
                await self.check_compliance()
            except (HarkenIQError, RuntimeError) as e:
                logger.warning("Compliance check failed: %s", e)
            if await self._pause(interval):
                break

    async def check_compliance(self) -> list:
        """Collect config, detect drift, propose CONFIG_RESTORE for approval.

        Returns the drift findings from this cycle. Proposals go through
        the normal approval queue -- config writes always need approval
        (R4 risk register), and dedup in ActionQueue prevents re-proposing
        while a remediation is already pending.
        """
        from harkeniq.compliance.config_policy import detect_drift

        snapshot = await self.protocol.collect_config()
        vendor = self.device_identity.vendor if self.device_identity else ""
        findings = []
        for policy in self.config_policies.values():
            if not policy.matches_device(vendor):
                continue
            policy_findings = detect_drift(snapshot, policy)
            findings.extend(policy_findings)
            drifted = [f for f in policy_findings if f.status == "DRIFT"]
            if not drifted:
                continue
            logger.warning(
                "Config drift on policy %s: %d attribute(s) off baseline",
                policy.policy_id, len(drifted),
            )
            attributes = {f.key: f.expected for f in drifted}
            rec = Action(
                id="",  # assigned by the queue
                type=ActionType.CONFIG_RESTORE,
                params={"attributes_json": json.dumps(attributes, sort_keys=True)},
                sensor_id=f"config:{policy.policy_id}",
                skill_name=f"config-policy:{policy.policy_id}",
                verdict_severity=VerdictSeverity[policy.severity],
            )
            await self._propose_actions([rec])
        self._last_drift_findings = findings
        return findings

    async def _execute_config_restore_playbook(self, action: Action) -> None:
        """Run an approved CONFIG_RESTORE through the playbook pipeline.

        The playbook's single step executes via the real ActionExecutor
        (allow list + audit apply) and is verified by re-reading each
        restored attribute. dry_run comes from compliance config.
        """
        from harkeniq.actions.playbook import Playbook, PlaybookStep
        from harkeniq.actions.playbook_executor import PlaybookExecutor
        from harkeniq.autonomy.verification import VerificationCheck
        from harkeniq.models import ActionOutcome, PlaybookStatus

        compliance_cfg = self.config.get("compliance") or {}
        try:
            attributes = json.loads(action.params.get("attributes_json", "{}"))
        except json.JSONDecodeError:
            attributes = {}
        checks = [
            VerificationCheck(
                description=f"{key} restored to {expected!r}",
                field_path=key,
                operator="equals",
                expected=expected,
            )
            for key, expected in attributes.items()
        ]
        playbook = Playbook(
            playbook_id=f"config-drift-{action.sensor_id}",
            name="Config drift remediation",
            description=action.skill_name,
            device_types=["*"],
            steps=[PlaybookStep(
                step_index=0,
                action_type=ActionType.CONFIG_RESTORE,
                description=f"Restore {len(attributes)} drifted attribute(s)",
                params=dict(action.params),
                verification_checks=checks,
                verification_wait_seconds=1.0,
            )],
            risk_level="medium",
        )

        async def _config_state(device_id: str) -> dict:
            return await self.protocol.collect_config()

        executor = PlaybookExecutor(
            action_executor=self.executor,
            get_device_state=_config_state,
            verification_wait_scale=compliance_cfg.get(
                "verification_wait_scale", 1.0
            ),
            dry_run=compliance_cfg.get("dry_run", True),
            checkpoint=self.checkpoint,  # QA-031: crash-safe state
        )
        execution = await executor.execute_playbook(playbook, self.agent_id)

        success = execution.status == PlaybookStatus.COMPLETED
        action.outcome = ActionOutcome(
            action_id=action.id,
            type=action.type,
            target=action.sensor_id,
            success=success,
            error_message=None if success else (
                execution.error_message
                or (execution.step_outcomes[-1].error_message
                    if execution.step_outcomes else "playbook failed")
            ),
            timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        action.status = (
            ActionStatus.COMPLETED if success else ActionStatus.FAILED
        )
        action.completed_at = action.outcome.timestamp
        if self.checkpoint:
            await self.checkpoint.save_audit_entry(
                action="CONFIG_RESTORE_PLAYBOOK",
                target=action.sensor_id,
                outcome="success" if success else "failed",
                evidence_json=json.dumps({
                    "playbook_id": playbook.playbook_id,
                    "dry_run": executor.dry_run,
                    "status": execution.status.value,
                }),
            )

    def _recover_to_observing(self, reason: str) -> None:
        """Walk the state machine back to OBSERVING after a failed cycle."""
        sm = self.state_machine
        while sm.current_state in _RECOVERY_PATH:
            sm.transition(_RECOVERY_PATH[sm.current_state], reason)

    # -- heartbeat loops (Doc 06 §9) -----------------------------------------

    async def _open_heartbeat_endpoint(self) -> None:
        hb = self.config.get("heartbeat") or {}
        bind_addr = hb.get("bind", "0.0.0.0")
        port = hb.get("port", 5150)
        loop = asyncio.get_running_loop()
        self._hb_transport, _ = await loop.create_datagram_endpoint(
            lambda: _HeartbeatProtocol(self),
            local_addr=(bind_addr, port),
        )
        logger.info("Heartbeat UDP endpoint listening on %s:%d", bind_addr, port)

    def _on_heartbeat_datagram(self, data: bytes, addr) -> None:
        # R3b-2: dispatch by message type envelope
        try:
            msg_type, payload = parse_envelope(data)
        except HeartbeatError:
            # Backward compat: raw JSON (no envelope) is a heartbeat
            msg_type, payload = MSG_HEARTBEAT, data

        if msg_type == MSG_HEARTBEAT:
            self._on_heartbeat_payload(payload, addr)
        elif msg_type == MSG_CLAIM:
            self._on_claim_payload(payload, addr)
        elif msg_type == MSG_CLAIM_ACK:
            self._on_claim_ack_payload(payload, addr)
        elif msg_type == MSG_SUSPICION:
            self._on_suspicion_payload(payload, addr)
        else:
            logger.warning("Unknown message type %#x from %s", msg_type, addr[0])

    def _on_heartbeat_payload(self, payload: bytes, addr) -> None:
        secret = (self.config.get("heartbeat") or {}).get("secret", "")
        try:
            packet = parse_packet(payload, secret)
        except HeartbeatError as e:
            logger.warning("Invalid heartbeat from %s: %s", addr[0], e)
            return
        if self.tracker:
            self.tracker.record_heartbeat(packet, addr[0], now=time.time())

    def _on_claim_payload(self, payload: bytes, addr) -> None:
        """Process an inbound claim from a peer (R3b-2)."""
        if self.peer_protocol is None or self.peer_protocol.claim_exchange is None:
            return
        try:
            claim = Claim.deserialize(payload)
        except (ValueError, KeyError) as e:
            logger.warning("Invalid claim from %s: %s", addr[0], e)
            return
        self.peer_protocol.claim_exchange.receive_claim(claim)

    def _on_claim_ack_payload(self, payload: bytes, addr) -> None:
        """Process an inbound claim ack from a peer (R3b-2)."""
        if self.peer_protocol is None or self.peer_protocol.claim_exchange is None:
            return
        try:
            ack = ClaimAck.deserialize(payload)
        except (ValueError, KeyError) as e:
            logger.warning("Invalid claim ack from %s: %s", addr[0], e)
            return
        self.peer_protocol.claim_exchange.receive_ack(ack)

    def _on_suspicion_payload(self, payload: bytes, addr) -> None:
        """Process inbound suspicion exchange (R3b-2 Phase 5)."""
        # Suspicion messages are JSON with HMAC (same auth as heartbeats)
        # Full implementation uses SuspicionTracker.receive_peer()
        pass

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
            payload = build_packet(packet, hb.get("secret", ""))
            data = build_envelope(MSG_HEARTBEAT, payload)
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
            # QA-020: mirror the lease into the local budget enforcer
            # (budget.py documents this call "on every heartbeat ack";
            # it never happened until now).
            self.budget.update_from_lease(
                self.current_lease.budget_remaining,
                self.current_lease.stop_switch,
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

    # -- QA-024 / A2.7: OS signals + BMC log poll ---------------------------

    def _build_os_collector(self):
        """Register only the signal sources whose backing exists here."""
        import shutil as _shutil

        from harkeniq.os_signals.collector import OSSignalCollector
        from harkeniq.os_signals.dmesg import DmesgSource
        from harkeniq.os_signals.journal import JournalSource
        from harkeniq.os_signals.smartctl import SmartctlSource
        from harkeniq.os_signals.syslog import SyslogSource

        collector = OSSignalCollector()
        syslog = SyslogSource()
        if syslog._log_path:
            collector.register(syslog)
        if _shutil.which("dmesg"):
            collector.register(DmesgSource())
        if _shutil.which("journalctl"):
            collector.register(JournalSource())
        if _shutil.which("smartctl"):
            collector.register(SmartctlSource())
        if not collector.active_sources:
            logger.info("No OS signal sources available on this host")
            return None
        logger.info(
            "OS signal sources active: %s", ", ".join(collector.active_sources)
        )
        return collector

    async def _log_signals_loop(self) -> None:
        """Collect OS events and BMC log entries into verdicts (QA-024)."""
        os_cfg = self.config.get("os_signals") or {}
        interval = os_cfg.get("interval", 60)
        log_interval = (self.config.get("polling") or {}).get("log_interval", 300)
        last_log_poll = 0.0
        while not self._shutdown.is_set():
            if await self._pause(interval):
                break
            if self.os_collector is not None:
                try:
                    events = await asyncio.to_thread(
                        self.os_collector.collect_all
                    )
                    self._ingest_os_events(events)
                except Exception as e:
                    logger.warning("OS signal collection failed: %s", e)
            now = time.time()
            if self.poller is not None and now - last_log_poll >= log_interval:
                last_log_poll = now
                try:
                    await self._poll_bmc_logs()
                except Exception as e:
                    logger.warning("BMC log poll failed: %s", e)

    def _ingest_os_events(self, events: list) -> None:
        """Map error/warning OS events onto reportable verdicts."""
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for event in events:
            if event.severity not in ("error", "warning"):
                continue
            # Corroborating-signal posture: OS logs point at hardware but
            # are not the hardware authority — error caps at WARNING
            # unless the category is a direct hardware fault channel.
            severity = (
                VerdictSeverity.CRITICAL
                if event.severity == "error"
                and event.category in ("mce", "nvme", "disk_io")
                else VerdictSeverity.WARNING
            )
            sensor_id = f"os:{event.category}"
            self._os_signal_verdicts[sensor_id] = Verdict(
                sensor_id=sensor_id,
                skill_name=f"os-signals:{event.source.value}",
                severity=severity,
                message=event.message[:512],
                evidence=[Evidence(
                    sensor_id=sensor_id,
                    skill_name=f"os-signals:{event.source.value}",
                    rule_index=-1,
                    condition=f"os_event:{event.category}",
                    fields={
                        "raw": event.raw_line[:512],
                        "device_path": event.device_path,
                        "component_hint": event.component_hint,
                    },
                    timestamp=now_iso,
                )],
                timestamp=now_iso,
            )

    async def _poll_bmc_logs(self) -> None:
        """SEL/IML entries -> verdicts, cursored so only NEW entries alert."""
        entries = await self.poller.poll_logs()
        if not entries:
            return
        cursor = self._log_cursors.get("bmc_sel", "")
        seen_ids = set(cursor.split(",")) if cursor else set()
        fresh = [
            e for e in entries
            if e.id and e.id not in seen_ids
            and e.severity.lower() in ("critical", "warning")
        ]
        # Cursor = the full current id set (SEL ids restart after a clear,
        # so a high-water mark would suppress post-clear entries).
        self._log_cursors["bmc_sel"] = ",".join(
            e.id for e in entries if e.id
        )[:4000]
        if not fresh:
            return
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        worst = (
            VerdictSeverity.CRITICAL
            if any(e.severity.lower() == "critical" for e in fresh)
            else VerdictSeverity.WARNING
        )
        self._os_signal_verdicts["log:sel"] = Verdict(
            sensor_id="log:sel",
            skill_name="bmc-log-poll",
            severity=worst,
            message=(
                f"{len(fresh)} new BMC log entr"
                f"{'y' if len(fresh) == 1 else 'ies'}: "
                + "; ".join(e.message[:120] for e in fresh[:3])
            ),
            evidence=[Evidence(
                sensor_id="log:sel",
                skill_name="bmc-log-poll",
                rule_index=-1,
                condition="new_log_entries",
                fields={
                    "entries": [
                        {"id": e.id, "severity": e.severity,
                         "message": e.message[:200], "timestamp": e.timestamp}
                        for e in fresh[:10]
                    ],
                },
                timestamp=now_iso,
            )],
            timestamp=now_iso,
        )
        # A2.1: SEL_CLEAR's precondition needs the events to have reached
        # the SM first; the report loop forwards these verdicts.
        if self.reporter and self.reporter.enabled:
            self._sel_events_forwarded = True
        logger.warning(
            "BMC log poll: %d new %s entr%s",
            len(fresh), worst.value, "y" if len(fresh) == 1 else "ies",
        )

    # -- Site Manager report loop (Doc 06 §10) -------------------------------

    async def _report_loop(self) -> None:
        sm = self.config.get("site_manager") or {}
        interval = sm.get("heartbeat_interval", DEFAULT_REPORT_INTERVAL)
        poll_interval = sm.get("action_poll_interval", 5)
        while not self._shutdown.is_set():
            # QA-041: keep retrying registration until it lands — the
            # reporter's own backoff paces the attempts.
            if not self._sm_registered:
                if await self._register_with_sm():
                    logger.info("Site Manager registration recovered")
            hb_ack = await self.reporter.send_heartbeat(
                self.state_machine.current_state.value,
                self.health_summary(),
                self._peer_status(),
            )
            # R3a: process authorization lease from heartbeat ack
            self._process_heartbeat_ack(hb_ack)
            await self._report_changed_verdicts()
            await self._sync_actions()
            await self._process_directives()
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
        # QA-024: OS-signal and BMC-log verdicts ride the same channel
        verdicts = list(self._last_verdicts) + list(
            self._os_signal_verdicts.values()
        )
        for verdict in verdicts:
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

    # -- directed directives (R5) --------------------------------------------

    async def _process_directives(self) -> None:
        """Pick up SM-initiated directives and execute them (R5).

        Execution runs in background tasks so a slow firmware flash never
        stalls heartbeats. The agent's own allow list, preconditions, and
        audit apply through the normal executor path -- a directive is
        delivery, not a policy bypass.
        """
        directives = await self.reporter.poll_directives()
        for directive in directives:
            if directive.directive_id in self._directives_in_flight:
                continue
            self._directives_in_flight.add(directive.directive_id)
            task = asyncio.create_task(self._execute_directive(directive))
            self._directive_tasks.add(task)
            task.add_done_callback(self._directive_tasks.discard)

    async def _execute_directive(self, directive) -> None:
        try:
            if directive.kind == "action":
                success, detail = await self._run_directed_action(directive)
            elif directive.kind == "skill_install":
                success, detail = self._install_directed_skill(directive)
            else:
                success, detail = False, f"unknown directive kind {directive.kind!r}"
        except Exception as e:  # never leave a directive unsettled
            success, detail = False, str(e)
        logger.info(
            "Directive %s (%s) %s%s",
            directive.directive_id, directive.kind,
            "completed" if success else "failed",
            f": {detail}" if detail and not success else "",
        )
        await self.reporter.report_directive_result(
            directive.directive_id, success, detail
        )
        if self.checkpoint:
            await self.checkpoint.save_audit_entry(
                action=f"DIRECTIVE_{directive.kind.upper()}",
                target=directive.action_type or directive.skill_id,
                outcome="success" if success else "failed",
                authorization=f"sm:{directive.issued_by}",
                evidence_json=json.dumps(
                    {"directive_id": directive.directive_id,
                     "detail": detail[:200]}
                ),
            )

    async def _run_directed_action(self, directive) -> tuple[bool, str]:
        try:
            action_type = ActionType(directive.action_type)
        except ValueError:
            return False, f"unknown action type {directive.action_type!r}"
        try:
            params = json.loads(directive.params_json or "{}")
        except json.JSONDecodeError:
            return False, "unparseable params_json"
        action = Action(
            id=f"dir-{directive.directive_id[:8]}",
            type=action_type,
            params={k: str(v) for k, v in params.items()},
            status=ActionStatus.APPROVED,  # SM authority; audited at SM
            sensor_id=f"directive:{directive.directive_id}",
            skill_name=directive.issued_by or "sm-directive",
        )
        # QA-020: SM delivery is not a policy bypass — the same gate
        # chain (preconditions, stop switch, blast radius) applies here.
        # A1: a directive carrying only an autonomy grant must still
        # satisfy the lease; one carrying a named human's approval need
        # not re-earn what the human already decided. An empty basis is
        # legacy SM-authority work (firmware campaigns) and keeps its
        # pre-A1 behaviour.
        basis = getattr(directive, "authorization", "") or "human_approval"
        await self._execute_gated(action, basis)
        outcome = action.outcome
        if outcome is None:
            return False, "no outcome recorded"
        return outcome.success, outcome.error_message or ""

    def _install_directed_skill(self, directive) -> tuple[bool, str]:
        from harkeniq.autonomy.skill_receiver import SkillReceiver

        receiver = SkillReceiver(self.skills_dir)
        accepted, reason = receiver.receive(
            skill_id=directive.skill_id,
            version=directive.skill_version,
            yaml_content=directive.yaml_content,
            tier=directive.tier,
            validation_state=directive.validation_state,
        )
        if accepted:
            try:
                self.reload_skills()
            except HarkenIQError as e:
                return False, f"installed but reload failed: {e}"
        return accepted, reason

    # -- QA-020: autonomy gate chain (A2.1/A2.2, R7-P2) ----------------------
    #
    # preconditions -> lease.allows_action -> stop switch -> blast radius
    # -> execute -> budget/blast accounting -> deferred verification.
    #
    # Hard gates (refuse even a human-approved action — approval does not
    # make an unsafe action safe): preconditions, stop switch, fully
    # expired lease, blast-radius rate limit. Authorization-shaped lease
    # verdicts ("propose", class-membership deny) are satisfied by the
    # approval the action already carries; they gate autonomous
    # initiative, which does not exist yet (T3 loop is future work).

    def _authorize_execution(
        self, action: Action, authorization: str = "human_approval",
    ) -> tuple[bool, str]:
        """Run the gate chain for one decided action. (allowed, reason).

        `authorization` names what the decision rests on. A1 makes this
        load-bearing: work that carries a named human's approval may
        proceed past an authorization-shaped lease verdict, because that
        verdict gates autonomous INITIATIVE and a human already took the
        initiative. Work that carries only the tenant's autonomy grant
        may not: that is exactly the case the S5 error-budget drop-back
        exists to stop.
        """
        # Actions outside the configured allow list fall through to the
        # executor, whose refusal is the canonical R-X6 audit event.
        if (
            self.executor is not None
            and action.type.value not in self.executor.allow_list
        ):
            return True, ""

        device_state, agent_state = self._precondition_states(action)
        pre = check_preconditions(action.type, device_state, agent_state)
        if not pre.passed:
            return False, f"preconditions failed: {pre.reason}"

        if (
            self.current_lease is not None and self.current_lease.stop_switch
        ) or self.budget.stop_switch_active:
            return False, "stop switch active"

        if self.current_lease is not None:
            risk = ACTION_RISK.get(action.type, "low")
            verdict = self.current_lease.allows_action(
                action.type.value, risk, self._sm_connected
            )
            if verdict == "deny" and self.current_lease.is_fully_expired():
                return False, "authorization lease fully expired"
            if verdict != "execute":
                if authorization == "autonomous_grant":
                    # No human decided this one. The lease is the whole
                    # authorization, so its refusal is final: a class
                    # dropped back by the error budget must stop running
                    # unattended, which is the point of dropping it back.
                    return False, (
                        f"lease refuses autonomous {action.type.value}: "
                        f"{verdict}"
                    )
                logger.info(
                    "Lease gate returned %r for %s; carried approval "
                    "satisfies it", verdict, action.type.value,
                )

        if not self.blast_radius.allows(action.type):
            return False, (
                f"blast radius: rate limit reached for {action.type.value}"
            )
        return True, ""

    def _precondition_states(self, action: Action) -> tuple[dict, dict]:
        """Assemble the A2.1 precondition inputs from what the agent can
        honestly observe. Unobservable facts stay at their fail-closed
        defaults (e.g. SEL fill and OS heartbeat until QA-024 lands)."""
        health = self.health_summary() if self._last_verdicts else {}
        actions_cfg = self.config.get("actions") or {}
        if health:
            overall = (
                "ok" if all(v == "OK" for v in health.values()) else "degraded"
            )
        else:
            # No verdicts yet (e.g. a directive before the first skill
            # cycle): fall back to the device's own health rollup.
            rollup = getattr(self._last_device, "health_rollup", None)
            raw = (getattr(rollup, "overall", "") or "").upper()
            overall = {"OK": "ok", "": "unknown", "UNKNOWN": "unknown"}.get(
                raw, "degraded"
            )
        device_state: dict[str, Any] = {
            "thermal_event_active": health.get("thermal") in ("WARNING", "CRITICAL"),
            "power_event_active": health.get("psu") in ("WARNING", "CRITICAL"),
            "overall_health": overall,
            "firmware_update_in_progress": False,
        }
        cap_policy = actions_cfg.get("power_cap_policy") or {}
        if cap_policy:
            device_state["power_cap_policy_min_watts"] = cap_policy.get("min_watts", 0)
            device_state["power_cap_policy_max_watts"] = cap_policy.get("max_watts", 0)
        target_watts = action.params.get("target_watts")
        if target_watts is not None:
            try:
                device_state["power_cap_target_watts"] = int(target_watts)
            except (TypeError, ValueError):
                pass

        alive_peers = 0
        if self.tracker is not None:
            alive_peers = sum(
                1 for p in self.tracker.get_peers()
                if p.status.value == "ALIVE"
            )
        agent_state: dict[str, Any] = {
            "bmc_consecutive_poll_failures": self._poll_failures,
            "alive_peer_count": alive_peers,
            # QA-024: true once the log loop has forwarded SEL/IML
            # verdicts to the SM. OS-heartbeat absence still has no
            # observer — stays fail-closed.
            "sel_events_forwarded": self._sel_events_forwarded,
            "os_heartbeat_absent_seconds": 0,
        }
        return device_state, agent_state

    async def _refuse_action(self, action: Action, reason: str) -> None:
        """Gate refusal: terminal FAILED outcome + audit, never executed."""
        from harkeniq.models import ActionOutcome

        target = action.params.get("target", "") or action.sensor_id
        action.status = ActionStatus.FAILED
        outcome = ActionOutcome(
            action_id=action.id,
            type=action.type,
            target=target,
            success=False,
            error_message=f"refused by autonomy gate: {reason}",
            duration_ms=0.0,
            timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        action.completed_at = outcome.timestamp
        action.outcome = outcome
        logger.warning(
            "Action %s (%s) refused by autonomy gate: %s",
            action.id, action.type.value, reason,
        )
        if self.checkpoint:
            await self.checkpoint.save_audit_entry(
                action=action.type.value,
                target=target,
                outcome="refused",
                evidence_json=json.dumps({"reason": reason, "gate": "autonomy"}),
            )

    async def _execute_gated(
        self, action: Action, authorization: str = "human_approval",
    ) -> None:
        """Gate chain -> execute -> accounting -> deferred verification.

        `authorization` is the basis the caller claims (A1). It never
        relaxes a gate; it only decides whether an authorization-shaped
        lease verdict is already satisfied. Every hard gate
        (preconditions, stop switch, expired lease, blast radius)
        refuses regardless of who asked.
        """
        if self._last_device is None:
            # Preconditions need device state; a directive can arrive
            # before the first skill cycle. Best effort, fail-closed.
            try:
                self._last_device = await self.protocol.poll_sensors()
            except Exception as e:
                logger.warning("Pre-gate sensor poll failed: %s", e)
        allowed, reason = self._authorize_execution(action, authorization)
        if not allowed:
            await self._refuse_action(action, reason)
            return
        if action.type == ActionType.CONFIG_RESTORE:
            # R4-2 P13: config writes run through the playbook pipeline
            # (precondition/verify/rollback machinery).
            await self._execute_config_restore_playbook(action)
        else:
            await self.executor.execute(action)
        if action.outcome is not None and action.outcome.success:
            self.budget.consume(action.type)
            self.blast_radius.record(action.type)
            self._schedule_verification(action)

    def _schedule_verification(self, action: Action) -> None:
        """R3a outcome verification, deferred by the action's window."""
        window = VERIFICATION_WINDOWS.get(action.type)
        if window is None:
            return
        actions_cfg = self.config.get("actions") or {}
        scale = float(actions_cfg.get("verification_window_scale", 1.0))
        task = asyncio.create_task(self._verify_action(action, window * scale))
        self._verification_tasks.add(task)
        task.add_done_callback(self._verification_tasks.discard)

    async def _verify_action(self, action: Action, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            post_state = await self._collect_post_state()
            checks = VERIFICATION_CHECKS.get(action.type, [])
            if checks and not all(c.field_path in post_state for c in checks):
                # Cannot honestly observe every check input -> UNKNOWN,
                # never a fabricated FAILURE (R7-P2: UNKNOWN producible).
                status = OutcomeStatus.UNKNOWN
            else:
                status = evaluate_verification(action.type, post_state)
            logger.info(
                "Verification for %s (%s): %s",
                action.id, action.type.value, status.value,
            )
            if self.checkpoint:
                await self.checkpoint.save_audit_entry(
                    action=action.type.value,
                    target=action.params.get("target", "") or action.sensor_id,
                    outcome=f"verified:{status.value}",
                    evidence_json=json.dumps({"post_state": post_state}),
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # verification must never crash the agent
            logger.warning(
                "Verification failed for %s: %s", action.id, e,
            )

    async def _collect_post_state(self) -> dict[str, Any]:
        """Post-action device state for verification — only fields the
        agent can actually observe; absent fields yield UNKNOWN."""
        post: dict[str, Any] = {}
        try:
            device = await self.protocol.poll_sensors()
            post["bmc_responsive"] = True
            fans = getattr(device, "fans", None) or []
            if fans:
                post["fan_rpm_healthy"] = all(
                    (getattr(f, "health", "") or "OK").upper() == "OK"
                    for f in fans
                )
        except Exception:
            post["bmc_responsive"] = False
        post["agent_registered"] = self._sm_connected
        return post

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
        # Two clocks (QA-008): `ts` (wall) drives checkpoint cadence below;
        # the EVALUATION timestamp defaults inside the engine so an
        # injected clock (the demo's narrative clock) is honored. An
        # explicit `timestamp` argument still wins for both.
        ts = timestamp if timestamp is not None else time.time()

        device = await self.protocol.poll_sensors()
        self._last_device = device
        self.state_machine.transition(AgentState.EVALUATING, "sensor poll complete")

        verdicts = await self.skill_engine.evaluate(device, timestamp)
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
                    # QA-020: every execution runs the autonomy gate chain
                    await self._execute_gated(action)
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
        """Per-subsystem worst-verdict summary for heartbeats (Doc 06 §9.2).

        The interface subsystem (R6) appears only when interface verdicts
        exist: a server with no ports must not report interface "OK" —
        that would claim an observation nothing made (OQ-12: silent is
        unobserved, never healthy). Server heartbeats stay byte-identical
        to pre-R6.
        """
        summary: dict[str, VerdictSeverity] = {
            t: VerdictSeverity.HEALTHY
            for t in _TARGET_COLLECTIONS
            if t != "interface"
        }
        for verdict in self._last_verdicts:
            if verdict.sensor_id.split(":", 1)[0] == "interface":
                summary["interface"] = VerdictSeverity.HEALTHY
                break
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
            log_cursors=dict(self._log_cursors),
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
