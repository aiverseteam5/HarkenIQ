"""TUI console rendering + hotkey tests (Doc 06 §11B)."""

import asyncio

from rich.console import Console

from harkeniq.actions.queue import ActionQueue
from harkeniq.models import ActionStatus, ActionType, Peer, PeerStatus, VerdictSeverity
from harkeniq.redfish.normalize import (
    NormalizedDisk,
    NormalizedFan,
    NormalizedMemory,
    NormalizedPSU,
    NormalizedThermal,
)
from harkeniq.reporting.console import (
    ConsoleSnapshot,
    ConsoleUI,
    Event,
    SubsystemRow,
    render,
    render_dashboard,
    render_drilldown,
    render_help,
)


def render_text(renderable, width=100):
    console = Console(record=True, width=width, force_terminal=False)
    console.print(renderable)
    return console.export_text()


def make_snapshot(**overrides):
    snap = ConsoleSnapshot(
        agent_name="rack-12-server-04",
        device_label="Dell PowerEdge R750 (iDRAC9)",
        state="OBSERVING",
        uptime_seconds=8040,  # 2h 14m
        baseline_established=True,
        subsystems={
            "fan": SubsystemRow("HEALTHY", "8/8 fans, 9200-10400 RPM"),
            "disk": SubsystemRow("WARNING", "4 drives, min SSD life 18%"),
            "memory": SubsystemRow("HEALTHY", "16x 32GB DDR4, 0 ECC errors"),
            "psu": SubsystemRow("HEALTHY", "2x 1400W, redundant, 186W draw"),
            "thermal": SubsystemRow("HEALTHY", "10 sensors, 22-38°C"),
        },
        now=1_700_000_000.0,
    )
    for key, value in overrides.items():
        setattr(snap, key, value)
    return snap


def make_action(action_id="act-0001", sensor_id="disk:sda"):
    from harkeniq.models import Action

    return Action(
        id=action_id, type=ActionType.IDENTIFY_LED, params={"target": "sda"},
        status=ActionStatus.PENDING, sensor_id=sensor_id, skill_name="disk-health",
        verdict_severity=VerdictSeverity.CRITICAL, proposed_at="2026-08-19T10:00:00Z",
    )


class TestDashboard:
    def test_healthy_dashboard(self):
        text = render_text(render_dashboard(make_snapshot()))
        assert "HarkenIQ Agent" in text
        assert "rack-12-server-04" in text
        assert "Dell PowerEdge R750 (iDRAC9)" in text
        assert "OBSERVING" in text
        assert "2h 14m" in text
        assert "ESTABLISHED" in text
        assert "Fan Health" in text and "8/8 fans" in text
        assert "PSU Health" in text and "redundant" in text
        assert "✓ OK" in text

    def test_warning_row_and_learning_baseline(self):
        snap = make_snapshot(baseline_established=False)
        text = render_text(render_dashboard(snap))
        assert "LEARNING" in text
        assert "⚠ WARN" in text
        assert "min SSD life 18%" in text

    def test_peers_panel(self):
        snap = make_snapshot(peers=[
            Peer(peer_id="agent-b", host="10.0.0.3", port=5150,
                 name="rack-12-srv-03", status=PeerStatus.ALIVE,
                 last_heartbeat="2023-11-14T22:13:18Z"),
            Peer(peer_id="agent-c", host="10.0.0.5", port=5150,
                 name="rack-12-srv-05", status=PeerStatus.UNRESPONSIVE),
        ])
        text = render_text(render_dashboard(snap))
        assert "PEERS" in text
        assert "rack-12-srv-03" in text and "✓ alive" in text
        assert "2s ago" in text  # 1_700_000_000 - heartbeat ts
        assert "rack-12-srv-05" in text and "UNRESPONSIVE" in text

    def test_pending_actions_and_selection(self):
        snap = make_snapshot(pending_actions=[
            make_action("act-0001", "disk:sda"),
            make_action("act-0002", "disk:sdb"),
        ])
        text = render_text(render_dashboard(snap, selected=1))
        assert "PENDING ACTIONS" in text
        assert "[1] IDENTIFY_LED disk:sda" in text
        assert "▶ [2] IDENTIFY_LED disk:sdb" in text
        assert "[a]pprove [D]eny" in text

    def test_events_newest_first(self):
        snap = make_snapshot(events=[
            Event("14:28:01", "⚠", "Disk.Bay.2 SSD life dropped below 20%"),
            Event("12:01:33", "ℹ", "Agent started, baseline restored"),
        ])
        text = render_text(render_dashboard(snap))
        assert text.index("14:28:01") < text.index("12:01:33")
        assert "EVENTS (newest first)" in text

    def test_hotkey_bar(self):
        text = render_text(render_dashboard(make_snapshot()))
        assert "[f]ans [d]isks [m]emory [p]su [t]hermal [P]eers [h]elp" in text
        assert "[q]uit" in text


class TestDrilldowns:
    def test_fan_detail(self):
        snap = make_snapshot(fans=[
            NormalizedFan(name="System Board Fan1A", speed_rpm=9800, health="OK",
                          threshold_low_critical=480, redundancy_health="OK"),
            NormalizedFan(name="System Board Fan1B", speed_rpm=None, health="Critical"),
        ])
        text = render_text(render_drilldown(snap, "fan"))
        assert "Fan Health Detail" in text
        assert "System Board Fan1A" in text and "9800" in text and "480" in text
        assert "✗ CRIT" in text
        assert "Redundancy: OK" in text
        assert "[Esc] Back to dashboard" in text

    def test_disk_detail(self):
        snap = make_snapshot(disks=[
            NormalizedDisk(name="Solid State Disk 0:1:0", media_type="SSD",
                           protocol="SATA", health="Warning", life_left_pct=18,
                           smart_alert=True),
        ])
        text = render_text(render_drilldown(snap, "disk"))
        assert "Disk Health Detail" in text
        assert "Solid State Disk 0:1:0" in text
        assert "18%" in text and "yes" in text

    def test_memory_detail(self):
        snap = make_snapshot(memory=[
            NormalizedMemory(name="DIMM.Socket.A1", capacity_mib=32768, type="DDR4",
                             health="OK", ecc_correctable_lifetime=3,
                             ecc_uncorrectable_lifetime=0),
        ])
        text = render_text(render_drilldown(snap, "memory"))
        assert "Memory Health Detail" in text
        assert "DIMM.Socket.A1" in text and "32GB" in text and "DDR4" in text

    def test_psu_detail(self):
        snap = make_snapshot(psus=[
            NormalizedPSU(name="PS1", capacity_watts=1400, output_watts=93,
                          input_voltage=230, health="OK", redundancy_mode="Failover"),
        ])
        text = render_text(render_drilldown(snap, "psu"))
        assert "PSU Health Detail" in text
        assert "1400W" in text and "Failover" in text

    def test_thermal_detail(self):
        snap = make_snapshot(thermals=[
            NormalizedThermal(name="System Board Inlet Temp", reading_c=22.0,
                              health="OK", threshold_warning=42.0,
                              threshold_critical=47.0),
        ])
        text = render_text(render_drilldown(snap, "thermal"))
        assert "Thermal Detail" in text
        assert "Inlet" in text and "22.0°C" in text and "47.0°C" in text

    def test_peer_detail_with_prefailure_evidence(self):
        snap = make_snapshot(peers=[
            Peer(peer_id="agent-b", host="10.0.0.3", port=5150,
                 name="rack-12-srv-03", status=PeerStatus.UNRESPONSIVE,
                 last_heartbeat="2023-11-14T22:12:20Z",
                 last_known_health={"fan": "OK", "psu": "CRITICAL"},
                 health_buffer=[{"fan": "OK", "psu": "OK"},
                                {"fan": "OK", "psu": "CRITICAL"}]),
        ])
        text = render_text(render_drilldown(snap, "peers"))
        assert "Peer Detail" in text
        assert "rack-12-srv-03" in text and "UNRESPONSIVE" in text
        assert "pre-failure evidence (2 summaries" in text
        assert "psu=CRITICAL" in text

    def test_help_overlay(self):
        text = render_text(render_help())
        assert "Help — hotkeys" in text
        assert "Approve the highlighted pending action" in text
        assert "Back to dashboard" in text


class FakeAgent:
    """Minimal agent surface for ConsoleUI hotkey tests."""

    def __init__(self):
        from harkeniq.state.machine import StateMachine

        self.agent_id = "agent-test"
        self.agent_name = "test-agent"
        self.identity = None
        self.state_machine = StateMachine()
        self.action_queue = ActionQueue()
        self.tracker = None
        self.skill_engine = None
        self.checkpoint = None
        self._last_verdicts = []
        self._last_device = None
        self._shutdown = asyncio.Event()

    def request_shutdown(self):
        self._shutdown.set()


class TestConsoleUI:
    def _ui_with_actions(self, n=2):
        agent = FakeAgent()
        for i in range(n):
            agent.action_queue.enqueue(
                ActionType.IDENTIFY_LED, f"disk:sd{chr(97 + i)}", "disk-health",
                VerdictSeverity.CRITICAL, {"target": f"sd{chr(97 + i)}"},
            )
        return ConsoleUI(agent), agent

    def test_view_switching(self):
        ui, _ = self._ui_with_actions(0)
        ui.handle_key("f")
        assert ui.view == "fan"
        ui.handle_key("\x1b")
        assert ui.view == "dashboard"
        ui.handle_key("P")
        assert ui.view == "peers"
        ui.handle_key("h")
        assert ui.view == "help"

    def test_selection_navigation_bounds(self):
        ui, _ = self._ui_with_actions(2)
        assert ui.selected == 0
        ui.handle_key("up")
        assert ui.selected == 0  # clamped
        ui.handle_key("down")
        assert ui.selected == 1
        ui.handle_key("down")
        assert ui.selected == 1  # clamped at end

    def test_approve_selected_action(self):
        ui, agent = self._ui_with_actions(2)
        ui.handle_key("down")
        ui.handle_key("a")
        pending = agent.action_queue.pending()
        assert len(pending) == 1
        assert pending[0].sensor_id == "disk:sda"  # second one approved
        assert len(agent.action_queue.approved()) == 1
        assert any("Approved" in e.message for e in ui.events)

    def test_deny_selected_action(self):
        ui, agent = self._ui_with_actions(1)
        ui.handle_key("D")
        assert agent.action_queue.pending() == []
        assert agent.action_queue.get(
            agent.action_queue.all()[0].id
        ).status == ActionStatus.DENIED
        assert any("Denied" in e.message for e in ui.events)

    def test_quit_requests_shutdown(self):
        ui, agent = self._ui_with_actions(0)
        ui.handle_key("q")
        assert agent._shutdown.is_set()

    def test_snapshot_from_fake_agent_renders(self):
        ui, _ = self._ui_with_actions(1)
        text = render_text(ui.renderable())
        assert "test-agent" in text
        assert "IDENTIFY_LED" in text
        assert "Action proposed" in text  # derived event

    async def test_run_loop_exits_on_shutdown(self):
        ui, agent = self._ui_with_actions(0)
        ui.console = Console(record=True, width=100, force_terminal=False)
        task = asyncio.create_task(ui.run())
        await asyncio.sleep(0.3)
        assert not task.done()
        agent.request_shutdown()
        await asyncio.wait_for(task, timeout=2.0)


class TestLiveIntegration:
    """Snapshot building against a real agent + mock simulator."""

    async def test_build_snapshot_from_live_agent(self, tmp_path):
        from pathlib import Path

        from harkeniq.agent import Agent
        from harkeniq.mock.simulator import MockSimulator

        repo = Path(__file__).parents[2]
        sim = MockSimulator(device="dell-r750", port=0, no_auth=True)
        await sim.start()
        try:
            agent = Agent({
                "bmc": {"host": sim.url, "username": "admin",
                        "password": "password", "verify_ssl": False},
                "skills": {"directory": str(repo / "skills")},
            })
            await agent.start()
            await agent.poll_and_evaluate()
            ui = ConsoleUI(agent)
            text = render_text(ui.renderable())
            assert "PowerEdge" in text
            assert "OBSERVING" in text
            assert "Fan Health" in text
            assert "✓ OK" in text
            await agent.stop()
        finally:
            await sim.stop()
