"""Terminal console UI (Doc 06 §11B, decision D9).

Rendering is split into pure functions over a plain ``ConsoleSnapshot``
so the layout is testable without a TTY (``Console(record=True)``).
``ConsoleUI`` owns the event loop side: snapshot building from a live
Agent, hotkeys from a raw stdin reader, and a ``rich.live.Live`` display.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from harkeniq.models import Action, Peer, PeerStatus, VerdictSeverity

#: Display refresh cap (Doc 06 §11B.3).
REFRESH_PER_SECOND = 10

_SUBSYSTEMS = ("fan", "disk", "memory", "psu", "thermal")

_SUBSYSTEM_LABELS = {
    "fan": "Fan Health",
    "disk": "Disk Health",
    "memory": "Memory Health",
    "psu": "PSU Health",
    "thermal": "Thermal",
}

_SEVERITY_BADGE = {
    "HEALTHY": ("✓ OK", "green"),
    "OK": ("✓ OK", "green"),
    "TRENDING": ("↘ TREND", "yellow"),
    "WARNING": ("⚠ WARN", "yellow"),
    "CRITICAL": ("✗ CRIT", "red"),
    "UNKNOWN": ("? UNK", "dim"),
}

_PEER_BADGE = {
    PeerStatus.ALIVE: ("✓ alive", "green"),
    PeerStatus.UNRESPONSIVE: ("✗ UNRESPONSIVE", "red"),
    PeerStatus.UNKNOWN: ("? unknown", "dim"),
}

_SEVERITY_RANK = {
    VerdictSeverity.HEALTHY: 0,
    VerdictSeverity.UNKNOWN: 1,
    VerdictSeverity.TRENDING: 2,
    VerdictSeverity.WARNING: 3,
    VerdictSeverity.CRITICAL: 4,
}


# ---------------------------------------------------------------------------
# Snapshot (plain data handed to the pure render functions)
# ---------------------------------------------------------------------------


@dataclass
class Event:
    """One line of the events log."""

    at: str  # HH:MM:SS
    icon: str  # ✓ ⚠ ✗ ℹ
    message: str


@dataclass
class SubsystemRow:
    status: str  # HEALTHY/TRENDING/WARNING/CRITICAL/UNKNOWN
    detail: str


@dataclass
class ConsoleSnapshot:
    agent_name: str = ""
    device_label: str = ""
    state: str = "BOOTING"
    uptime_seconds: float = 0.0
    baseline_established: bool = False
    subsystems: dict[str, SubsystemRow] = field(default_factory=dict)
    fans: list[Any] = field(default_factory=list)
    disks: list[Any] = field(default_factory=list)
    memory: list[Any] = field(default_factory=list)
    psus: list[Any] = field(default_factory=list)
    thermals: list[Any] = field(default_factory=list)
    peers: list[Peer] = field(default_factory=list)
    pending_actions: list[Action] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)  # newest first
    now: float = 0.0


def _fmt(value: Any, suffix: str = "") -> str:
    return "—" if value is None else f"{value}{suffix}"


def _uptime_str(seconds: float) -> str:
    minutes = int(seconds) // 60
    return f"{minutes // 60}h {minutes % 60:02d}m"


def _ago(last_iso: Optional[str], now: float) -> str:
    if not last_iso:
        return "never"
    try:
        ts = datetime.strptime(last_iso, "%Y-%m-%dT%H:%M:%SZ")
        delta = max(0, int(now - ts.replace(tzinfo=timezone.utc).timestamp()))
    except ValueError:
        return last_iso
    return f"{delta}s ago"


# ---------------------------------------------------------------------------
# Snapshot construction from a live Agent
# ---------------------------------------------------------------------------


def _subsystem_detail(target: str, sensors: list[Any]) -> str:
    """Human summary line per Doc 06 §11B.1."""
    if not sensors:
        return "no sensors"
    if target == "fan":
        rpms = [f.speed_rpm for f in sensors if f.speed_rpm is not None]
        ok = sum(1 for f in sensors if f.health == "OK")
        rng = f", {min(rpms)}-{max(rpms)} RPM" if rpms else ""
        return f"{ok}/{len(sensors)} fans{rng}"
    if target == "disk":
        lives = [d.life_left_pct for d in sensors if d.life_left_pct is not None]
        life = f", min SSD life {min(lives)}%" if lives else ""
        return f"{len(sensors)} drives{life}"
    if target == "memory":
        gib = {s.capacity_mib // 1024 for s in sensors if s.capacity_mib}
        size = f"x {'/'.join(str(g) for g in sorted(gib))}GB" if gib else ""
        types = {s.type for s in sensors if s.type}
        ecc = sum(s.ecc_correctable_lifetime or 0 for s in sensors)
        return f"{len(sensors)}{size} {'/'.join(sorted(types))}, {ecc} ECC errors"
    if target == "psu":
        cap = {p.capacity_watts for p in sensors if p.capacity_watts}
        watts = sum(p.output_watts or 0 for p in sensors)
        redundant = any(p.redundancy_health == "OK" for p in sensors)
        cap_s = f"x {'/'.join(str(c) for c in sorted(cap))}W" if cap else ""
        return (f"{len(sensors)}{cap_s}, "
                f"{'redundant' if redundant else 'non-redundant'}, {watts}W draw")
    if target == "thermal":
        readings = {
            t.name: t.reading_c for t in sensors if t.reading_c is not None
        }
        if not readings:
            return f"{len(sensors)} temp sensors"
        low, high = min(readings.values()), max(readings.values())
        return f"{len(sensors)} sensors, {low:.0f}-{high:.0f}°C"
    return f"{len(sensors)} sensors"


def build_snapshot(agent: Any, started_at: float, events: list[Event]) -> ConsoleSnapshot:
    """Assemble a ConsoleSnapshot from a live Agent."""
    now = time.time()
    identity = agent.identity
    device_label = ""
    if identity is not None:
        vendor = {"dell": "Dell", "hpe": "HPE"}.get(identity.vendor, identity.vendor)
        version = identity.controller_version or ""
        device_label = f"{vendor} {identity.model} ({identity.controller_type}{version})"

    # Worst severity per subsystem from the last verdicts
    worst: dict[str, VerdictSeverity] = {t: VerdictSeverity.HEALTHY for t in _SUBSYSTEMS}
    for verdict in agent._last_verdicts:
        target = verdict.sensor_id.split(":", 1)[0]
        if target in worst and _SEVERITY_RANK[verdict.severity] > _SEVERITY_RANK[worst[target]]:
            worst[target] = verdict.severity

    device = agent._last_device
    collections = {
        "fan": list(getattr(device, "fans", []) or []),
        "disk": list(getattr(device, "disks", []) or []),
        "memory": list(getattr(device, "memory", []) or []),
        "psu": list(getattr(device, "psus", []) or []),
        "thermal": list(getattr(device, "thermals", []) or []),
    }
    subsystems = {
        t: SubsystemRow(
            status=worst[t].value if collections[t] else "UNKNOWN",
            detail=_subsystem_detail(t, collections[t]),
        )
        for t in _SUBSYSTEMS
    }

    trending = agent.skill_engine.trending if agent.skill_engine else None
    baselines = trending.get_all_baselines() if trending else {}
    established = bool(baselines) and all(
        trending.confidence(sensor_id) >= 1.0 for sensor_id in baselines
    )

    return ConsoleSnapshot(
        agent_name=agent.agent_name or agent.agent_id,
        device_label=device_label,
        state=agent.state_machine.current_state.value,
        uptime_seconds=now - started_at,
        baseline_established=established,
        subsystems=subsystems,
        fans=collections["fan"],
        disks=collections["disk"],
        memory=collections["memory"],
        psus=collections["psu"],
        thermals=collections["thermal"],
        peers=agent.tracker.get_peers() if agent.tracker else [],
        pending_actions=agent.action_queue.pending(),
        events=list(events),
        now=now,
    )


# ---------------------------------------------------------------------------
# Pure render functions
# ---------------------------------------------------------------------------


def _badge(status: str) -> Text:
    label, style = _SEVERITY_BADGE.get(status, ("? UNK", "dim"))
    return Text(label, style=style)


def render_dashboard(snap: ConsoleSnapshot, selected: int = 0) -> RenderableType:
    """Main dashboard per Doc 06 §11B.1."""
    header = Table.grid(expand=True)
    header.add_column(ratio=1)
    header.add_column(justify="right")
    header.add_row(
        f"Device: {snap.device_label or '—'}",
        f"Uptime: {_uptime_str(snap.uptime_seconds)}",
    )
    header.add_row(
        Text.assemble("State:  ", (snap.state, "bold")),
        Text.assemble(
            "Baseline: ",
            ("ESTABLISHED", "green") if snap.baseline_established
            else ("LEARNING", "yellow"),
        ),
    )

    subsystems = Table(box=None, pad_edge=False, expand=True, show_header=True)
    subsystems.add_column("SUBSYSTEM", width=16)
    subsystems.add_column("STATUS", width=10)
    subsystems.add_column("DETAIL", ratio=1)
    for target in _SUBSYSTEMS:
        row = snap.subsystems.get(target, SubsystemRow("UNKNOWN", "no data"))
        subsystems.add_row(_SUBSYSTEM_LABELS[target], _badge(row.status), row.detail)

    peers = Table(box=None, pad_edge=False, expand=True, show_header=False)
    peers.add_column(width=20)
    peers.add_column(width=16)
    peers.add_column(ratio=1)
    if snap.peers:
        for peer in snap.peers:
            label, style = _PEER_BADGE[peer.status]
            peers.add_row(
                peer.name or peer.peer_id or peer.host,
                Text(label, style=style),
                _ago(peer.last_heartbeat, snap.now),
            )
    else:
        peers.add_row(Text("no peers configured", style="dim"), "", "")

    actions = Table(box=None, pad_edge=False, expand=True, show_header=False)
    actions.add_column(ratio=1)
    if snap.pending_actions:
        for i, action in enumerate(snap.pending_actions):
            marker = "▶" if i == selected else " "
            style = "bold" if i == selected else ""
            actions.add_row(Text(
                f"{marker} [{i + 1}] {action.type.value} {action.sensor_id} "
                f"— {action.skill_name} ({action.verdict_severity.value})   "
                f"[a]pprove [D]eny",
                style=style,
            ))
    else:
        actions.add_row(Text("none", style="dim"))

    events = Table(box=None, pad_edge=False, expand=True, show_header=False)
    events.add_column(width=9)
    events.add_column(ratio=1)
    for event in snap.events[:8]:
        events.add_row(Text(event.at, style="dim"), f"{event.icon} {event.message}")
    if not snap.events:
        events.add_row("", Text("no events yet", style="dim"))

    hotkeys = Text(
        "[f]ans [d]isks [m]emory [p]su [t]hermal [P]eers [h]elp\n"
        "[a]pprove action [D]eny action  [↑/↓] select  [q]uit",
        style="dim",
    )

    body = Group(
        header,
        Text("─" * 40, style="dim"),
        subsystems,
        Panel(peers, title="PEERS", title_align="left", border_style="dim"),
        Panel(actions, title="PENDING ACTIONS", title_align="left", border_style="dim"),
        Panel(events, title="EVENTS (newest first)", title_align="left",
              border_style="dim"),
        hotkeys,
    )
    return Panel(body, title="HarkenIQ Agent", subtitle=snap.agent_name,
                 subtitle_align="right")


def _drill_table(columns: list[tuple[str, int]], rows: list[list[Any]]) -> Table:
    table = Table(box=None, pad_edge=False, expand=True)
    for name, width in columns:
        table.add_column(name, width=width or None, ratio=1 if not width else None)
    for row in rows:
        table.add_row(*[cell if isinstance(cell, Text) else str(cell) for cell in row])
    return table


def render_drilldown(snap: ConsoleSnapshot, view: str) -> RenderableType:
    """Subsystem/peer drill-down views (Doc 06 §11B.4)."""
    footer = Text("\n[Esc] Back to dashboard", style="dim")

    if view == "fan":
        rows = [
            [f.name, _fmt(f.speed_rpm), _badge(f.health.upper()),
             _fmt(f.threshold_low_critical)]
            for f in snap.fans
        ]
        table = _drill_table(
            [("NAME", 24), ("RPM", 8), ("HEALTH", 10), ("THRESHOLD", 10)], rows
        )
        redundancy = next(
            (f.redundancy_health for f in snap.fans if f.redundancy_health), None
        )
        extra = Text(f"\nRedundancy: {redundancy or '—'}")
        return Panel(Group(table, extra, footer), title="Fan Health Detail")

    if view == "disk":
        rows = [
            [d.name, d.media_type, d.protocol, _badge(d.health.upper()),
             _fmt(d.life_left_pct, "%"), "yes" if d.smart_alert else "no"]
            for d in snap.disks
        ]
        table = _drill_table(
            [("NAME", 26), ("TYPE", 5), ("PROTO", 6), ("HEALTH", 10),
             ("LIFE", 6), ("SMART ALERT", 11)], rows
        )
        return Panel(Group(table, footer), title="Disk Health Detail")

    if view == "memory":
        rows = [
            [m.name, _fmt(m.capacity_mib and m.capacity_mib // 1024, "GB"), m.type,
             _badge(m.health.upper()), _fmt(m.ecc_correctable_lifetime),
             _fmt(m.ecc_uncorrectable_lifetime)]
            for m in snap.memory
        ]
        table = _drill_table(
            [("DIMM", 12), ("SIZE", 7), ("TYPE", 6), ("HEALTH", 10),
             ("ECC CORR", 9), ("ECC UNCORR", 10)], rows
        )
        return Panel(Group(table, footer), title="Memory Health Detail")

    if view == "psu":
        rows = [
            [p.name or p.member_id, _fmt(p.capacity_watts, "W"),
             _fmt(p.output_watts, "W"), _fmt(p.input_voltage, "V"),
             _badge(p.health.upper()), p.redundancy_mode or "—"]
            for p in snap.psus
        ]
        table = _drill_table(
            [("PSU", 12), ("CAPACITY", 9), ("OUTPUT", 8), ("INPUT", 7),
             ("HEALTH", 10), ("REDUNDANCY", 12)], rows
        )
        return Panel(Group(table, footer), title="PSU Health Detail")

    if view == "thermal":
        rows = [
            [t.name, _fmt(t.reading_c, "°C"), _badge(t.health.upper()),
             _fmt(t.threshold_warning, "°C"), _fmt(t.threshold_critical, "°C")]
            for t in snap.thermals
        ]
        table = _drill_table(
            [("SENSOR", 26), ("READING", 9), ("HEALTH", 10),
             ("WARN", 8), ("CRIT", 8)], rows
        )
        return Panel(Group(table, footer), title="Thermal Detail")

    if view == "peers":
        parts: list[RenderableType] = []
        for peer in snap.peers:
            label, style = _PEER_BADGE[peer.status]
            lines = [Text.assemble(
                (peer.name or peer.peer_id or peer.host, "bold"),
                f"  {peer.host}:{peer.port}  ",
                (label, style),
                f"  last heartbeat {_ago(peer.last_heartbeat, snap.now)}",
            )]
            if peer.last_known_health:
                lines.append(Text(
                    "  last known health: "
                    + ", ".join(f"{k}={v}" for k, v in peer.last_known_health.items())
                ))
            if peer.status == PeerStatus.UNRESPONSIVE and peer.health_buffer:
                lines.append(Text(
                    f"  pre-failure evidence ({len(peer.health_buffer)} summaries "
                    "retained):", style="yellow",
                ))
                for summary in peer.health_buffer[-5:]:
                    lines.append(Text(
                        "    " + ", ".join(f"{k}={v}" for k, v in summary.items()),
                        style="dim",
                    ))
            parts.append(Group(*lines))
        if not parts:
            parts.append(Text("no peers configured", style="dim"))
        parts.append(footer)
        return Panel(Group(*parts), title="Peer Detail")

    return render_help()


def render_help() -> RenderableType:
    """Help overlay (Doc 06 §11B.2)."""
    table = Table(box=None, pad_edge=False)
    table.add_column("Key", width=8, style="bold")
    table.add_column("Action")
    for key, action in (
        ("f", "Fan details (RPM, thresholds)"),
        ("d", "Disk details (SMART, SSD life)"),
        ("m", "Memory details (DIMMs, ECC counts)"),
        ("p", "PSU details (power draw, redundancy)"),
        ("t", "Thermal details (temps, thresholds)"),
        ("P", "Peer details (heartbeat history, pre-failure evidence)"),
        ("a", "Approve the highlighted pending action"),
        ("D", "Deny the highlighted pending action"),
        ("↑/↓", "Navigate pending actions"),
        ("Esc", "Back to dashboard"),
        ("h", "This help overlay"),
        ("q", "Quit"),
    ):
        table.add_row(key, action)
    return Panel(table, title="Help — hotkeys")


def render(snap: ConsoleSnapshot, view: str = "dashboard", selected: int = 0) -> RenderableType:
    """Render whichever view is active."""
    if view == "dashboard":
        return render_dashboard(snap, selected)
    if view == "help":
        return render_help()
    return render_drilldown(snap, view)


# ---------------------------------------------------------------------------
# Interactive console
# ---------------------------------------------------------------------------

_VIEW_KEYS = {"f": "fan", "d": "disk", "m": "memory", "p": "psu",
              "t": "thermal", "P": "peers", "h": "help"}


class ConsoleUI:
    """Interactive TUI bound to a running Agent."""

    def __init__(self, agent: Any, console: Optional[Console] = None) -> None:
        self.agent = agent
        self.console = console or Console()
        self.view = "dashboard"
        self.selected = 0
        self.events: deque[Event] = deque(maxlen=50)
        self._started_at = time.time()
        self._prev_severity: dict[str, str] = {}
        self._prev_peer_status: dict[str, PeerStatus] = {}
        self._known_actions: set[str] = set()
        self._had_fault = False
        self.add_event("ℹ", "Console attached")

    # -- events -------------------------------------------------------------

    def add_event(self, icon: str, message: str) -> None:
        self.events.appendleft(
            Event(at=time.strftime("%H:%M:%S"), icon=icon, message=message)
        )

    def _derive_events(self, snap: ConsoleSnapshot) -> None:
        """Append events on verdict/peer/action changes (Doc 06 §11B.3)."""
        for target, row in snap.subsystems.items():
            prev = self._prev_severity.get(target)
            if prev is not None and prev != row.status:
                icon = {"CRITICAL": "✗", "WARNING": "⚠", "TRENDING": "↘"}.get(
                    row.status, "✓"
                )
                self.add_event(icon, f"{_SUBSYSTEM_LABELS[target]}: {prev} → {row.status}")
            self._prev_severity[target] = row.status
        all_ok = all(r.status in ("HEALTHY", "UNKNOWN") for r in snap.subsystems.values())
        if all_ok and self._had_fault:
            self.add_event("✓", "All subsystems healthy")
            self._had_fault = False
        elif not all_ok:
            self._had_fault = True

        for peer in snap.peers:
            prev = self._prev_peer_status.get(peer.host)
            if prev is not None and prev != peer.status:
                icon = "✗" if peer.status == PeerStatus.UNRESPONSIVE else "✓"
                self.add_event(
                    icon, f"Peer {peer.name or peer.host} {peer.status.value}"
                )
            self._prev_peer_status[peer.host] = peer.status

        for action in snap.pending_actions:
            if action.id not in self._known_actions:
                self._known_actions.add(action.id)
                self.add_event(
                    "ℹ", f"Action proposed: {action.type.value} on {action.sensor_id}"
                )

    # -- snapshot / render --------------------------------------------------

    def snapshot(self) -> ConsoleSnapshot:
        snap = build_snapshot(self.agent, self._started_at, list(self.events))
        self._derive_events(snap)
        snap.events = list(self.events)
        if snap.pending_actions:
            self.selected = min(self.selected, len(snap.pending_actions) - 1)
        else:
            self.selected = 0
        return snap

    def renderable(self) -> RenderableType:
        return render(self.snapshot(), self.view, self.selected)

    # -- hotkeys ------------------------------------------------------------

    def handle_key(self, key: str) -> None:
        """Process one hotkey (Doc 06 §11B.2)."""
        if key == "q":
            self.agent.request_shutdown()
            return
        if key == "\x1b":  # Esc
            self.view = "dashboard"
            return
        if key in _VIEW_KEYS:
            self.view = _VIEW_KEYS[key]
            return
        if key in ("up", "\x1b[A"):
            self.selected = max(0, self.selected - 1)
            return
        if key in ("down", "\x1b[B"):
            pending = self.agent.action_queue.pending()
            if pending:
                self.selected = min(len(pending) - 1, self.selected + 1)
            return
        if key in ("a", "D"):
            self._decide_selected(approve=(key == "a"))

    def _decide_selected(self, approve: bool) -> None:
        pending = self.agent.action_queue.pending()
        if not pending:
            return
        action = pending[min(self.selected, len(pending) - 1)]
        if approve:
            self.agent.action_queue.approve(action.id)
            self.add_event("✓", f"Approved {action.type.value} on {action.sensor_id}")
        else:
            self.agent.action_queue.deny(action.id)
            self.add_event("✗", f"Denied {action.type.value} on {action.sensor_id}")
        if self.agent.checkpoint:
            try:
                asyncio.get_running_loop().create_task(
                    self._persist_decision(action, approve)
                )
            except RuntimeError:  # no running loop (unit tests)
                asyncio.run(self._persist_decision(action, approve))

    async def _persist_decision(self, action: Action, approve: bool) -> None:
        await self.agent.checkpoint.save_actions([action])
        await self.agent.checkpoint.save_audit_entry(
            action=action.type.value,
            target=action.sensor_id,
            outcome="approved" if approve else "denied",
            authorization="console-operator",
        )

    # -- stdin reader -------------------------------------------------------

    def _install_stdin_reader(self, loop: asyncio.AbstractEventLoop) -> Optional[Any]:
        import os
        import sys

        if not sys.stdin.isatty():
            return None
        import termios
        import tty

        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        tty.setcbreak(fd)

        def _on_readable() -> None:
            data = os.read(fd, 8)
            if not data:
                return
            text = data.decode("utf-8", errors="ignore")
            if text.startswith("\x1b[A"):
                self.handle_key("up")
            elif text.startswith("\x1b[B"):
                self.handle_key("down")
            else:
                for ch in text:
                    self.handle_key(ch)

        loop.add_reader(fd, _on_readable)
        return (fd, saved)

    def _remove_stdin_reader(self, loop: asyncio.AbstractEventLoop, state: Any) -> None:
        import termios

        if state is None:
            return
        fd, saved = state
        loop.remove_reader(fd)
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)

    # -- main loop ----------------------------------------------------------

    async def run(self) -> None:
        """Refresh the display until the agent shuts down (or 'q')."""
        from rich.live import Live

        loop = asyncio.get_running_loop()
        stdin_state = self._install_stdin_reader(loop)
        try:
            with Live(
                self.renderable(),
                console=self.console,
                refresh_per_second=REFRESH_PER_SECOND,
                screen=self.console.is_terminal,
            ) as live:
                while not self.agent._shutdown.is_set():
                    live.update(self.renderable())
                    await asyncio.sleep(1 / REFRESH_PER_SECOND)
        finally:
            self._remove_stdin_reader(loop, stdin_state)
