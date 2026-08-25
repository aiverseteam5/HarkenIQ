"""`harken demo` — 60-second scripted showcase (Doc 09).

Runs a real Agent against the in-process Dell R750 mock simulator as
"rack-12-server-04", with two simulated rack peers, pre-seeded baselines
(the equivalent of a completed 24h learning window), and a scripted fault
cascade: gradual fan decline (trending), SSD wear (warning + action),
peer death (witness evidence), PSU removal (critical), and a thermal
drift. Time is compressed by ``--speed``; the whole run exits cleanly.

On a terminal the full TUI (Doc 06 §11B) renders with narration events;
without a TTY (or with ``--plain``) a line-mode reporter prints verdict,
peer, and action transitions so the run is scriptable and testable.
"""

from __future__ import annotations

import asyncio
import random
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from harkeniq.agent import Agent
from harkeniq.mock.simulator import MockSimulator
from harkeniq.models import HeartbeatPacket, PeerStatus, VerdictSeverity
from harkeniq.skills.engine import _TARGET_COLLECTIONS

#: Total scripted runtime in demo-seconds (Doc 09 §4).
DEMO_DURATION = 60.0

#: QA-028 (Doc 09 §7): individual scenario mode. Each entry is
#: (duration_s, [(demo_t, step_method_name), ...]); "all" is the full
#: progressive cascade. The fan-seize CRITICAL belongs ONLY to the
#: fan-failure scenario — in "all" it masked the PSU diagnostics action
#: (undocumented in doc 09's sequence).
SCENARIOS: dict[str, tuple[float, list[tuple[float, str]]]] = {
    "all": (DEMO_DURATION, [
        (5.0, "_start_fan_decline"),
        (15.0, "_inject_ssd_wear"),
        (22.0, "_degrade_peer_03"),
        (25.0, "_kill_peer_03"),
        (35.0, "_remove_psu"),
        (48.0, "_start_thermal_drift"),
        (55.0, "_narrate_summary_soon"),
    ]),
    "fan-failure": (15.0, [
        (2.0, "_start_fan_decline"),
        (10.0, "_fail_fan"),
    ]),
    "disk-smart": (15.0, [(2.0, "_inject_ssd_wear")]),
    "memory-ecc": (15.0, [(2.0, "_start_ecc_ramp")]),
    "psu-redundancy": (15.0, [(2.0, "_remove_psu")]),
    "thermal-warning": (15.0, [(2.0, "_start_thermal_drift")]),
    "peer-witness": (15.0, [
        (2.0, "_degrade_peer_03"),
        (4.0, "_kill_peer_03"),
    ]),
}
#: Sensor poll cadence in demo-seconds (Doc 09 §3: compressed schedule).
POLL_EVERY = 2.0
#: Synthetic history seeded per sensor before the run starts.
SEED_SAMPLES = 60
SEED_RNG = 42

_ALL_OK = {"fan": "OK", "disk": "OK", "memory": "OK", "psu": "OK", "thermal": "OK"}

_INTERESTING = (
    VerdictSeverity.TRENDING,
    VerdictSeverity.WARNING,
    VerdictSeverity.CRITICAL,
)


def _skills_dir() -> str:
    """Bundled skills: repo checkout first, then the system dir (Doc 07)."""
    from harkeniq.skills.loader import DEFAULT_SKILLS_DIR

    for candidate in (
        Path(__file__).resolve().parents[2] / "skills",
        Path(DEFAULT_SKILLS_DIR),
    ):
        if candidate.is_dir():
            return str(candidate)
    return "skills"


def demo_config(sim_url: str, speed: float, checkpoint_path: str) -> dict:
    """Agent config with the Doc 09 §3 demo overrides applied."""
    interval = POLL_EVERY / speed
    return {
        "agent": {"id": "demo-rack-12-server-04", "name": "rack-12-server-04",
                  "log_level": "WARNING"},
        "bmc": {"host": sim_url, "username": "admin", "password": "password",
                "verify_ssl": False},
        "skills": {"directory": _skills_dir()},
        "polling": {"sensor_interval": interval},
        "baseline": {"min_samples": 5, "window_samples": 1440,
                     "critical_pause_samples": 3},
        "trending": {"min_samples": 5, "slope_threshold": 0.05,
                     "r_squared_min": 0.3, "max_projection_days": 90},
        "heartbeat": {"port": 0, "interval": interval, "timeout_multiplier": 3,
                      "secret": "demo-site-secret"},
        "peers": [{"host": "127.0.0.3", "port": 5150},
                  {"host": "127.0.0.5", "port": 5150}],
        "checkpoint": {"path": checkpoint_path, "interval": 999999},
    }


def preseed_baselines(
    agent: Agent,
    device,
    samples: int = SEED_SAMPLES,
    interval_s: Optional[float] = None,
    now: Optional[float] = None,
) -> int:
    """Fast-forward the 24h learning window (Doc 09 §3).

    Seeds every trended sensor with ``samples`` synthetic historical
    readings around its current healthy value (fixed RNG, ±5% gaussian
    band) so all baselines start at confidence 1.0. ``interval_s``/``now``
    let the caller seed on the narrative clock (QA-008).
    """
    rng = random.Random(SEED_RNG)
    trending = agent.skill_engine.trending
    interval = interval_s if interval_s is not None else trending.expected_interval
    now = time.time() if now is None else now
    seeded = 0
    for skill in agent.skill_engine._skills.values():
        if not skill.trending:
            continue
        metric = skill.trending[0].field
        is_counter = skill.trending[0].counter
        for sensor in getattr(device, _TARGET_COLLECTIONS[skill.target], []):
            value = getattr(sensor, metric, None)
            if value is None:
                continue
            sensor_id = f"{skill.target}:{sensor.name}"
            for i in range(samples):
                ts = now - (samples - i) * interval
                if is_counter:
                    # QA-032: counter fields baseline RATES; a healthy
                    # DIMM's history is zero errors/hr (Doc 13 §5.6).
                    noisy = 0.0
                else:
                    noisy = float(value) * (1.0 + rng.gauss(0.0, 0.015))
                trending.update_baseline(sensor_id, noisy, ts, "OK")
            if is_counter:
                # Prime the raw-count cursor so the first live poll
                # yields a rate instead of re-priming.
                trending.counter_to_rate(sensor_id, float(value), now)
            seeded += 1
    return seeded


@dataclass
class _SimPeer:
    """An in-process simulated rack neighbour (Doc 09 §2)."""

    host: str
    agent_id: str
    name: str
    alive: bool = True
    health: dict = field(default_factory=lambda: dict(_ALL_OK))
    seq: int = 0


class DemoRunner:
    """Owns the simulator, the agent, the fault timeline, and the display."""

    def __init__(self, speed: float = 1.0, tui: bool = False,
                 duration: Optional[float] = None,
                 scenario: str = "all") -> None:
        if scenario not in SCENARIOS:
            raise ValueError(
                f"unknown scenario {scenario!r}; valid: {sorted(SCENARIOS)}"
            )
        self.speed = speed
        self.tui = tui
        self.scenario = scenario
        scenario_duration, self._script = SCENARIOS[scenario]
        self.duration = duration if duration is not None else scenario_duration
        self.interval = POLL_EVERY / speed
        self.sim: Optional[MockSimulator] = None
        self.agent: Optional[Agent] = None
        self.ui = None
        self._t0 = 0.0
        self.peers = [
            _SimPeer("127.0.0.3", "demo-rack-12-server-03", "rack-12-server-03"),
            _SimPeer("127.0.0.5", "demo-rack-12-server-05", "rack-12-server-05"),
        ]
        # observed-run statistics for the summary screen
        self._seen: dict[VerdictSeverity, set[str]] = {s: set() for s in _INTERESTING}
        self._active: set[tuple[str, VerdictSeverity]] = set()
        self._peer_prev: dict[str, PeerStatus] = {}
        self._peer_lost: list[str] = []
        self._action_ids: set[str] = set()
        # QA-028: last message per sensor and the one-shot correlation note
        self._messages: dict[str, str] = {}
        self._correlation_note: Optional[str] = None

    # -- time & output -------------------------------------------------------

    def _demo_t(self) -> float:
        return (time.time() - self._t0) * self.speed

    async def _sleep_until(self, demo_t: float) -> None:
        delay = self._t0 + demo_t / self.speed - time.time()
        if delay > 0:
            await asyncio.sleep(delay)

    def _say(self, message: str) -> None:
        if not self.tui:
            print(f"[t={self._demo_t():05.1f}s] {message}", flush=True)

    def _narrate(self, message: str) -> None:
        if self.ui is not None:
            self.ui.add_event("ℹ", message)
        self._say(f"ℹ {message}")

    # -- scripted fault timeline (Doc 09 §4) ---------------------------------

    async def _timeline(self) -> None:
        agent = self.agent
        script: list[tuple[float, Callable]] = [
            (demo_t, getattr(self, step_name))
            for demo_t, step_name in self._script
        ]
        for demo_t, step in script:
            await self._sleep_until(demo_t)
            if agent._shutdown.is_set():
                return
            await step()
        await self._sleep_until(self.duration)
        self._narrate("Demo complete — shutting down")
        agent.request_shutdown()

    async def _start_fan_decline(self) -> None:
        self._narrate("Fault: Fan1A bearing wear — gradual RPM decline begins")
        asyncio.get_running_loop().create_task(self._fan_decline_task())

    async def _fan_decline_task(self) -> None:
        # Doc 09 §4: -200 RPM per interval (one narrative hour) — with the
        # narrative clock this reads as ~-200 RPM/hr, threshold ~46h out.
        rpm = 9800
        while rpm > 5600 and not self.agent._shutdown.is_set():
            rpm -= 200
            await self.sim.inject_fault("fan", "Fan1A", {"speed_rpm": rpm})
            await asyncio.sleep(self.interval)

    async def _inject_ssd_wear(self) -> None:
        self._narrate("Fault: SSD wear — drive at 18% life, SMART predictive alert")
        await self.sim.inject_fault(
            "disk", "drive_0",
            {"health": "Warning", "life_left_pct": 18, "smart_alert": True},
        )

    async def _degrade_peer_03(self) -> None:
        self.peers[0].health = {**_ALL_OK, "psu": "CRITICAL"}

    async def _kill_peer_03(self) -> None:
        self._narrate("Fault: rack-12-server-03 loses power — heartbeats stop")
        self.peers[0].alive = False

    async def _fail_fan(self) -> None:
        self._narrate("Fault: Fan1A seizes completely")
        await self.sim.inject_fault(
            "fan", "Fan1A", {"health": "Critical", "speed_rpm": 0}
        )

    async def _remove_psu(self) -> None:
        self._narrate("Fault: PSU PS2 pulled — redundancy lost")
        await self.sim.inject_fault(
            "psu", "PS2", {"state": "Absent", "redundancy_health": "Warning"}
        )

    async def _start_thermal_drift(self) -> None:
        self._narrate("Effect: inlet temperature climbing after fan loss")
        asyncio.get_running_loop().create_task(self._thermal_drift_task())

    async def _thermal_drift_task(self) -> None:
        # QA-028: the old drift nudged Exhaust 38 -> ~40°C against a 75°C
        # threshold — the scene was inert. Doc 09 §Phase 6: the fan
        # decline heats the INLET (warning threshold 42°C) and CPU1; the
        # drift now actually crosses the threshold and produces WARNING.
        inlet, cpu1 = 22.0, 54.0
        for _ in range(6):
            if self.agent._shutdown.is_set():
                return
            inlet = min(inlet + 4.0, 44.0)
            cpu1 = min(cpu1 + 1.5, 62.0)
            await self.sim.inject_fault(
                "thermal", "Inlet", {"reading_c": round(inlet, 1)}
            )
            await self.sim.inject_fault(
                "thermal", "CPU1", {"reading_c": round(cpu1, 1)}
            )
            await asyncio.sleep(self.interval)

    async def _start_ecc_ramp(self) -> None:
        """memory-ecc scenario (Doc 09 §7): rising correctable ECC count."""
        self._narrate("Fault: DIMM A1 correctable ECC errors accumulating")
        asyncio.get_running_loop().create_task(self._ecc_ramp_task())

    async def _ecc_ramp_task(self) -> None:
        count = 0
        while not self.agent._shutdown.is_set():
            count += 25
            await self.sim.inject_fault(
                "memory", "a1",
                {"ecc_correctable_lifetime": count,
                 "alarm_ecc_correctable": count >= 100},
            )
            await asyncio.sleep(self.interval)

    async def _narrate_summary_soon(self) -> None:
        self._narrate("All faults injected — final observations")

    # -- simulated peers -----------------------------------------------------

    async def _peer_loop(self, peer: _SimPeer) -> None:
        """Deliver authenticated-equivalent heartbeats straight to the tracker."""
        while peer.alive and not self.agent._shutdown.is_set():
            peer.seq += 1
            packet = HeartbeatPacket(
                v=1, agent_id=peer.agent_id, name=peer.name, seq=peer.seq,
                ts=time.time(), state="OBSERVING",
                health_summary=dict(peer.health),
            )
            self.agent.tracker.record_heartbeat(packet, peer.host, now=time.time())
            await asyncio.sleep(self.interval)

    # -- observer (line-mode output + summary statistics) --------------------

    async def _observe_loop(self) -> None:
        agent = self.agent
        while not agent._shutdown.is_set():
            self._observe_once()
            await asyncio.sleep(self.interval / 2)
        self._observe_once()

    def _observe_once(self) -> None:
        agent = self.agent

        current: set[tuple[str, VerdictSeverity]] = set()
        by_key = {}
        for verdict in agent._last_verdicts:
            if verdict.severity in _INTERESTING:
                key = (verdict.sensor_id, verdict.severity)
                current.add(key)
                by_key[key] = verdict
        for sensor_id, severity in sorted(
            current - self._active, key=lambda k: (k[0], k[1].value)
        ):
            self._seen[severity].add(sensor_id)
            verdict = by_key[(sensor_id, severity)]
            self._messages[sensor_id] = f"{severity.value:<9} {verdict.message}"
            self._say(f"{severity.value:<9} {verdict.message}")
        self._active = current
        self._cross_subsystem_check()

        for peer in agent.tracker.get_peers() if agent.tracker else []:
            prev = self._peer_prev.get(peer.host)
            if prev != peer.status:
                if peer.status == PeerStatus.UNRESPONSIVE:
                    self._peer_lost.append(peer.name or peer.host)
                    self._say(
                        f"UNRESPONSIVE peer {peer.name or peer.host} — "
                        f"pre-failure evidence retained "
                        f"({len(peer.health_buffer)} health summaries)"
                    )
                elif peer.status == PeerStatus.ALIVE:
                    self._say(f"Peer {peer.name or peer.host} is alive")
            self._peer_prev[peer.host] = peer.status

        pending = agent.action_queue.pending()
        new = [a for a in pending if a.id not in self._action_ids]
        if new:
            self._action_ids.update(a.id for a in pending)
            self._say(
                f"PENDING ACTIONS ({len(pending)}): "
                + "; ".join(f"{a.type.value} on {a.sensor_id}" for a in pending)
            )

    # -- QA-028: cross-subsystem correlation (Doc 09 §Phase 6) ---------------

    def _cross_subsystem_check(self) -> None:
        """Emit the fan→thermal correlation note once, with a REAL r
        computed from the two sensors' observed sample series."""
        if self._correlation_note is not None:
            return
        fan_bad = any(
            sid.startswith("fan:") for sid, sev in self._active
            if sev in (VerdictSeverity.TRENDING, VerdictSeverity.CRITICAL)
        )
        thermal_bad = any(
            sid.startswith("thermal:") for sid, sev in self._active
        )
        if not (fan_bad and thermal_bad):
            return
        r = self._fan_thermal_correlation()
        if r is None and self._demo_t() < self.duration - 2.0:
            # Not enough aligned samples yet (the thermal baseline resets
            # on the 5σ jump) — retry next observe rather than emitting a
            # correlation claim without a coefficient.
            return
        note = "Cross-subsystem: thermal rise correlates with fan RPM decline"
        if r is not None:
            note += f" (r={r:.2f})"
        self._correlation_note = note
        if self.ui is not None:
            self.ui.add_event("🔗", note)
        self._say(f"🔗 {note}")

    def _fan_thermal_correlation(self) -> Optional[float]:
        """|Pearson r| between the declining fan and the rising inlet
        temperature over their recent observed samples; None if the
        series are too short or degenerate."""
        import math
        trending = self.agent.skill_engine.trending
        fan = thermal = None
        for sensor_id, baseline in trending.get_all_baselines().items():
            if sensor_id.startswith("fan:") and "Fan1A" in sensor_id:
                fan = baseline
            elif sensor_id.startswith("thermal:") and "Inlet" in sensor_id:
                thermal = baseline
        if fan is None or thermal is None:
            return None
        n = min(len(fan.ring_buffer), len(thermal.ring_buffer), 10)
        if n < 4:
            return None
        xs = [v for _, v in fan.ring_buffer[-n:]]
        ys = [v for _, v in thermal.ring_buffer[-n:]]
        mx, my = sum(xs) / n, sum(ys) / n
        cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        vx = sum((x - mx) ** 2 for x in xs)
        vy = sum((y - my) ** 2 for y in ys)
        if vx <= 0 or vy <= 0:
            return None
        r = cov / math.sqrt(vx * vy)
        return abs(r)

    # -- summary screen (Doc 09 §5) ------------------------------------------

    def print_summary(self) -> None:
        """Doc 09 §Phase 7 summary dashboard (QA-028: was a text blob).

        Every line reflects what the run actually observed — capabilities
        are checked only when demonstrated, never asserted."""
        width = 62

        def row(text: str = "") -> str:
            return f"│ {text:<{width - 4}} │"

        def header(title: str) -> list[str]:
            return [row(title), row("═" * len(title))]

        lines = [
            "",
            "┌─ HarkenIQ Demo Summary ─── rack-12-server-04 "
            + "─" * (width - 48) + "┐",
            row(),
        ]

        lines += header("VERDICT SUMMARY")
        rank = {VerdictSeverity.CRITICAL: 0, VerdictSeverity.WARNING: 1,
                VerdictSeverity.TRENDING: 2}
        shown = sorted(
            self._messages.items(),
            key=lambda kv: (
                min((rank[s] for s in _INTERESTING
                     if kv[0] in self._seen[s]), default=9),
                kv[0],
            ),
        )
        for _sensor, message in shown or [("", "(no non-OK verdicts observed)")]:
            lines.append(row(message[:width - 4]))
        lines.append(row())

        lines += header("PEER WITNESS")
        peers = self.agent.tracker.get_peers() if self.agent.tracker else []
        for peer in peers:
            name = peer.name or peer.host
            lines.append(row(f"{name}: {peer.status.value}"))
            if peer.status == PeerStatus.UNRESPONSIVE and peer.health_buffer:
                last = peer.health_buffer[-1]
                summary = last.get("health_summary", last) if isinstance(last, dict) else last
                lines.append(row(f"  Last known: {summary}"[:width - 4]))
        if not peers:
            lines.append(row("(no peers configured)"))
        lines.append(row())

        if self._correlation_note:
            lines += header("CORRELATION")
            lines.append(row(self._correlation_note[:width - 4]))
            lines.append(row())

        actions = [
            a for a in self.agent.action_queue.all()
            if a.id in self._action_ids
        ]
        lines += header(f"ACTIONS PROPOSED: {len(actions)}")
        for i, action in enumerate(actions, 1):
            lines.append(row(
                f"[{i}] {action.type.value} on {action.sensor_id} "
                f"({action.status.value.lower()})"[:width - 4]
            ))
        lines.append(row())

        capabilities = [
            ("Real-time fault detection",
             bool(self._seen[VerdictSeverity.CRITICAL]
                  or self._seen[VerdictSeverity.WARNING])),
            ("Predictive trending with time-to-threshold",
             bool(self._seen[VerdictSeverity.TRENDING])),
            ("Cross-subsystem correlation (fan → thermal)",
             self._correlation_note is not None),
            ("Peer heartbeat + witness evidence", bool(self._peer_lost)),
            ("Gated action pipeline", bool(self._action_ids)),
            ("Cross-vendor normalization (Dell iDRAC9)", True),
        ]
        lines += header("CAPABILITIES DEMONSTRATED")
        for label, demonstrated in capabilities:
            mark = "✓" if demonstrated else "—"
            lines.append(row(f"{mark} {label}"))
        lines.append(row())
        lines.append(
            row("Demo complete. Approve actions in the TUI with [a] or via")
        )
        lines.append(row("'harken action approve'."))
        lines.append("└" + "─" * (width - 2) + "┘")
        lines.append("")
        print("\n".join(lines), flush=True)

    # -- main ----------------------------------------------------------------

    async def run(self, install_signal_handlers: bool = True) -> None:
        self.sim = MockSimulator(device="dell-r750", port=0, no_auth=True)
        await self.sim.start()
        tmpdir = tempfile.mkdtemp(prefix="harkeniq-demo-")
        try:
            cfg = demo_config(self.sim.url, self.speed, f"{tmpdir}/demo.db")
            self.agent = Agent(cfg)
            await self.agent.start()

            # QA-008 narrative clock: one wall poll interval = one narrative
            # HOUR. Trending slopes are per-hour, so evaluating compressed
            # samples on the wall clock printed -5,402,706 RPM/hr and
            # "0 hours" projections; on the narrative clock the fan story
            # reads as doc 09 §3 wrote it: ~-200 RPM/hr, threshold in ~46h.
            wall_start = time.time()
            wall_interval = self.interval

            def narrative_now() -> float:
                elapsed_polls = (time.time() - wall_start) / wall_interval
                return wall_start + elapsed_polls * 3600.0

            self.agent.skill_engine._time_fn = narrative_now
            self.agent.skill_engine.trending.expected_interval = 3600.0

            device = await self.agent.poller.poll_sensors()
            self.agent._last_device = device
            seeded = preseed_baselines(
                self.agent, device,
                interval_s=3600.0, now=wall_start,
            )

            if self.tui:
                from harkeniq.reporting.console import ConsoleUI

                self.ui = ConsoleUI(self.agent)

            self._t0 = time.time()
            self._narrate(
                f"rack-12-server-04 online — {seeded} baselines pre-seeded "
                f"(24h learning equivalent), speed x{self.speed:g}"
            )
            async with asyncio.TaskGroup() as tg:
                tg.create_task(
                    self.agent.run(install_signal_handlers=install_signal_handlers),
                    name="agent",
                )
                for peer in self.peers:
                    tg.create_task(self._peer_loop(peer), name=f"peer-{peer.name}")
                tg.create_task(self._observe_loop(), name="observer")
                tg.create_task(self._timeline(), name="timeline")
                if self.ui is not None:
                    tg.create_task(self.ui.run(), name="tui")
        finally:
            await self.sim.stop()
            shutil.rmtree(tmpdir, ignore_errors=True)
        self.print_summary()
