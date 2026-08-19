"""Agent orchestrator (Doc 06 §2.2, Doc 10 §2.1) — Phase 2 subset.

Wires poller -> baseline -> skill evaluation -> trending -> debounce ->
verdicts, drives the 7-state machine, and checkpoints to SQLite.

Heartbeat, Site Manager reporting, TUI, and action execution are later
phases; the state machine's action path is traversed (AWAITING_AUTH ->
REPORTING) but no Redfish action is issued in Phase 2.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from harkeniq.errors import ConfigError
from harkeniq.models import Action, AgentState, Verdict
from harkeniq.poller import Poller
from harkeniq.redfish.client import RedfishClient
from harkeniq.skills.engine import _TARGET_COLLECTIONS, SkillEngine
from harkeniq.skills.loader import load_skills
from harkeniq.skills.trending import TrendingEngine
from harkeniq.state.checkpoint import CheckpointManager
from harkeniq.state.machine import StateMachine

logger = logging.getLogger("harkeniq.agent")

DEFAULT_CHECKPOINT_INTERVAL = 600


def _iso(ts_unix: float) -> str:
    return datetime.fromtimestamp(ts_unix, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


class Agent:
    """Top-level agent orchestrator (composition root)."""

    def __init__(self, config: dict) -> None:
        bmc = config.get("bmc") or {}
        if not bmc.get("host"):
            raise ConfigError("bmc.host is required")
        self.config = config
        self.agent_id: str = config.get("agent_id", "harkeniq-agent")
        self.skills_dir: str = config.get("skills_dir", "skills")
        checkpoint_cfg = config.get("checkpoint") or {}
        self._checkpoint_path: Optional[str] = checkpoint_cfg.get("path")
        self._checkpoint_interval: float = checkpoint_cfg.get(
            "interval_seconds", DEFAULT_CHECKPOINT_INTERVAL
        )

        self.state_machine = StateMachine()
        self.client: Optional[RedfishClient] = None
        self.poller: Optional[Poller] = None
        self.skill_engine: Optional[SkillEngine] = None
        self.checkpoint: Optional[CheckpointManager] = None

        self._last_device: Any = None
        self._last_verdicts: list[Verdict] = []
        self._last_checkpoint_at: float = 0.0
        self._running = False

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Startup sequence (Doc 06 §14, Phase 2 subset), ends in OBSERVING."""
        bmc = self.config["bmc"]

        # Connect to BMC and detect vendor
        self.client = RedfishClient(
            host=bmc["host"], verify_ssl=bmc.get("verify_ssl", False)
        )
        await self.client.connect(bmc.get("username", ""), bmc.get("password", ""))
        self.poller = Poller(self.client)
        identity = await self.poller.detect()

        # Load skills
        skills = load_skills(self.skills_dir)

        # Restore checkpoint (baselines survive restarts, Doc 13 §7)
        trending = TrendingEngine(self.config)
        if self._checkpoint_path:
            self.checkpoint = CheckpointManager(self._checkpoint_path)
            state = await self.checkpoint.load_checkpoint()
            if state["baselines"]:
                trending.restore_baselines(state["baselines"])
                logger.info("Restored %d baselines from checkpoint",
                            len(state["baselines"]))

        self.skill_engine = SkillEngine(
            list(skills.values()),
            self.config.get("debounce"),
            trending,
        )

        self.state_machine.transition(
            AgentState.OBSERVING,
            f"Startup complete: {identity.model}, {len(skills)} skills loaded",
        )
        self._running = True

    async def stop(self) -> None:
        """Graceful shutdown: final checkpoint, close connections."""
        self._running = False
        if self.checkpoint:
            await self._write_checkpoint(force=True)
            await self.checkpoint.close()
            self.checkpoint = None
        if self.client:
            await self.client.close()
            self.client = None
        logger.info("Agent stopped")

    # -- main loop ----------------------------------------------------------

    async def poll_and_evaluate(self, timestamp: Optional[float] = None) -> list[Verdict]:
        """One full cycle: poll -> evaluate -> decide -> report -> observe.

        Drives OBSERVING -> EVALUATING -> DECIDING -> (AWAITING_AUTH ->
        REPORTING ->) OBSERVING and returns the cycle's verdicts.
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

        pending = self.skill_engine.get_pending_actions()
        if pending:
            self.state_machine.transition(
                AgentState.AWAITING_AUTH,
                f"{len(pending)} action(s) require approval",
            )
            # Phase 2: no TUI/authorizer — record proposals and report
            for action in pending:
                if self.checkpoint:
                    await self.checkpoint.save_audit_entry(
                        action=action.type.value,
                        target=action.sensor_id,
                        outcome="proposed",
                    )
            self.state_machine.transition(
                AgentState.REPORTING, "no authorizer available (Phase 2)"
            )
            self.state_machine.transition(AgentState.OBSERVING, "report logged")
        else:
            self.state_machine.transition(AgentState.OBSERVING, "no action needed")

        if self.checkpoint and ts - self._last_checkpoint_at >= self._checkpoint_interval:
            await self._write_checkpoint(now=ts)

        return verdicts

    def get_pending_actions(self) -> list[Action]:
        return self.skill_engine.get_pending_actions() if self.skill_engine else []

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
            peers=[],
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
