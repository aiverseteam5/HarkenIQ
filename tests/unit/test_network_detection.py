"""R6-P4 unit tests: per-port baselines, R-M5 classification, probe wiring.

The R-M5 split is the milestone's diagnostic heart: load-correlated
congestion must NEVER convict hardware, and physical-class errors at low
load must. Both branches are first-class here (review 8A: the negative
branch is a named test).
"""

import pytest
import pytest_asyncio

from harkeniq.autonomy.correlation_probe import (
    CorrelationProbe,
    FaultLocation,
    error_counters_from_interface,
)
from harkeniq.mock.switch_sim import SwitchSimulator
from harkeniq.models import VerdictSeverity
from harkeniq.protocols.gnmi import GNMIProtocol
from harkeniq.protocols.model import NormalizedDevice, NormalizedInterface
from harkeniq.skills.engine import SkillEngine
from harkeniq.skills.loader import load_skill_file
from harkeniq.skills.trending import TrendingEngine

PERMISSIVE_DEBOUNCE = {"critical": [1, 1], "warning": [1, 1], "recovery": [1, 1]}


def interface_skill():
    return load_skill_file("skills/interface-health.yaml")


def primed_engine(*port_names, min_samples=1):
    trending = TrendingEngine({"baseline": {"min_samples": min_samples}})
    for name in port_names:
        trending.update_baseline(f"interface:{name}", 0.0, 0.0, "OK")
    return SkillEngine(
        [interface_skill()], PERMISSIVE_DEBOUNCE, trending_engine=trending
    )


def iface(**overrides) -> NormalizedInterface:
    data = {
        "name": "Ethernet0", "admin_state": "Up", "oper_state": "Up",
        "health": "OK",
    }
    data.update(overrides)
    return NormalizedInterface(**data)


def severities(verdicts):
    return {v.severity for v in verdicts}


class TestRM5Classification:
    """Hardware degradation vs load-correlated congestion (R-M5)."""

    @pytest.mark.asyncio
    async def test_crc_at_low_load_convicts_hardware(self):
        engine = primed_engine("Ethernet0")
        device = NormalizedDevice(interfaces=[iface(
            crc_error_rate=25.0, in_octet_rate=1_000_000.0,
        )])
        verdicts = await engine.evaluate(device)
        critical = [v for v in verdicts if v.severity == VerdictSeverity.CRITICAL]
        assert len(critical) == 1
        assert "hardware degradation" in critical[0].message

    @pytest.mark.asyncio
    async def test_congestion_is_load_correlated_no_action(self):
        # The negative branch: discards + full queues = congestion.
        # WARNING only, no CRITICAL, and no action ever proposed.
        engine = primed_engine("Ethernet0")
        device = NormalizedDevice(interfaces=[iface(
            in_discard_rate=500.0, queue_occupancy_max_pct=95.0,
            in_octet_rate=9_000_000_000.0,
        )])
        verdicts = await engine.evaluate(device)
        assert VerdictSeverity.CRITICAL not in severities(verdicts)
        warnings = [v for v in verdicts if v.severity == VerdictSeverity.WARNING]
        assert len(warnings) == 1
        assert "load-correlated" in warnings[0].message
        assert engine._pending_actions == []

    @pytest.mark.asyncio
    async def test_optic_rx_floor_critical(self):
        engine = primed_engine("Ethernet0")
        device = NormalizedDevice(interfaces=[iface(optics_rx_power_dbm=-15.2)])
        verdicts = await engine.evaluate(device)
        assert VerdictSeverity.CRITICAL in severities(verdicts)

    @pytest.mark.asyncio
    async def test_prefec_ber_leading_indicator_tiers(self):
        engine = primed_engine("Ethernet0")
        warn = await engine.evaluate(
            NormalizedDevice(interfaces=[iface(pre_fec_ber=1e-5)])
        )
        assert VerdictSeverity.WARNING in severities(warn)
        assert VerdictSeverity.CRITICAL not in severities(warn)
        crit = await engine.evaluate(
            NormalizedDevice(interfaces=[iface(pre_fec_ber=1e-3)])
        )
        assert VerdictSeverity.CRITICAL in severities(crit)

    @pytest.mark.asyncio
    async def test_unobservable_fields_never_fire(self):
        # A virtual switch exports no CRC/optics/BER: every field None.
        # Doc 07 §3.6 — None never triggers a verdict.
        engine = primed_engine("Ethernet0")
        device = NormalizedDevice(interfaces=[iface()])
        verdicts = await engine.evaluate(device)
        assert severities(verdicts) <= {VerdictSeverity.HEALTHY}


class TestPerPortBaselines:
    @pytest.mark.asyncio
    async def test_ports_learn_independently(self):
        # Per-entity baselines (R-M4): Ethernet0 primed, Ethernet4 not.
        # Same device, same skill: one port evaluates expressions, the
        # other stays in learning pass-through.
        engine = primed_engine("Ethernet0")
        device = NormalizedDevice(interfaces=[
            iface(name="Ethernet0", crc_error_rate=25.0, in_octet_rate=1.0),
            iface(name="Ethernet4", crc_error_rate=25.0, in_octet_rate=1.0),
        ])
        verdicts = await engine.evaluate(device)
        by_port = {v.sensor_id: v.severity for v in verdicts}
        assert by_port["interface:Ethernet0"] == VerdictSeverity.CRITICAL
        assert by_port["interface:Ethernet4"] == VerdictSeverity.HEALTHY  # learning


class TestEndToEndSimulator:
    """Sim → GNMIProtocol → skill engine: injected faults classified."""

    @pytest_asyncio.fixture
    async def sim_proto(self):
        sim = SwitchSimulator(num_ports=2)
        await sim.start()
        proto = GNMIProtocol(host="127.0.0.1", port=sim.port, plaintext=True)
        await proto.connect({})
        yield sim, proto
        await proto.disconnect()
        await sim.stop()

    @pytest.mark.asyncio
    async def test_injected_optic_decay_classified_hardware(self, sim_proto):
        sim, proto = sim_proto
        sim.state.inject_optic_rx_decay("Ethernet0", db_per_s=0.5)
        sim.state.tick(30)  # -3.0 -> -18.0 dBm
        device = await proto.poll_sensors()
        engine = primed_engine("Ethernet0", "Ethernet4")
        verdicts = await engine.evaluate(device)
        critical = [v for v in verdicts if v.severity == VerdictSeverity.CRITICAL]
        assert critical and "Rx power" in critical[0].message
        # The healthy port stays exonerated (R-M21 spirit: clean paths count).
        assert all(v.sensor_id != "interface:Ethernet4"
                   or v.severity == VerdictSeverity.HEALTHY for v in verdicts)

    @pytest.mark.asyncio
    async def test_injected_crc_ramp_with_synthetic_stream(self, sim_proto):
        import time as _time

        sim, proto = sim_proto
        # Deterministic stream: stop the live subscribe task so it cannot
        # overwrite the synthetic samples, then ingest two samples 10s
        # apart with a CRC delta at low traffic.
        proto._stream_task.cancel()
        now = _time.time()
        proto._ingest_sample("Ethernet0", {
            "SAI_PORT_STAT_ETHER_STATS_CRC_ALIGN_ERRORS": 0,
            "SAI_PORT_STAT_IF_IN_OCTETS": 0,
        }, now=now - 10.0)
        proto._ingest_sample("Ethernet0", {
            "SAI_PORT_STAT_ETHER_STATS_CRC_ALIGN_ERRORS": 500,
            "SAI_PORT_STAT_IF_IN_OCTETS": 10_000,
        }, now=now)
        device = await proto.poll_sensors()
        eth0 = next(i for i in device.interfaces if i.name == "Ethernet0")
        assert eth0.crc_error_rate == 50.0
        assert eth0.crc_errors_total == 500
        engine = primed_engine("Ethernet0", "Ethernet4")
        verdicts = await engine.evaluate(device)
        assert VerdictSeverity.CRITICAL in severities(verdicts)


class TestTrending:
    @pytest.mark.asyncio
    async def test_declining_optic_produces_trending_verdict(self):
        trending = TrendingEngine({
            "baseline": {"min_samples": 1},
            "trending": {"min_samples": 5, "slope_threshold": 0.0001,
                         "r_squared_min": 0.5},
            "polling": {"sensor_interval": 60},
        })
        engine = SkillEngine(
            [interface_skill()], PERMISSIVE_DEBOUNCE, trending_engine=trending
        )
        # Steady decline at the expected 60s poll cadence (the trending
        # engine treats larger gaps as discontinuities and resets). The
        # skill's optics_rx_power_dbm declining rule must fire.
        verdicts = []
        for i in range(12):
            device = NormalizedDevice(interfaces=[iface(
                optics_rx_power_dbm=-3.0 - i * 0.2,
            )])
            verdicts = await engine.evaluate(
                device, timestamp=1000.0 + i * 60.0
            )
        trending_verdicts = [
            v for v in verdicts if v.severity == VerdictSeverity.TRENDING
        ]
        assert trending_verdicts, "declining optic produced no TRENDING verdict"
        assert "declining" in trending_verdicts[0].message


class TestProbeWiring:
    def test_error_counters_from_real_interface(self):
        local = iface(crc_errors_total=42, in_errors_total=42)
        remote = iface(name="Ethernet8", crc_errors_total=0, in_errors_total=0)
        probe = CorrelationProbe("agent-a")
        result = probe.diagnose(
            "device-b",
            error_counters_from_interface(local),
            error_counters_from_interface(remote),
        )
        assert result.fault_location == FaultLocation.LOCAL_PORT

    def test_both_sides_errors_is_cable(self):
        probe = CorrelationProbe("agent-a")
        result = probe.diagnose(
            "device-b",
            error_counters_from_interface(iface(crc_errors_total=10)),
            error_counters_from_interface(
                iface(name="Ethernet8", crc_errors_total=7)
            ),
        )
        assert result.fault_location == FaultLocation.CABLE

    def test_unobservable_counters_inconclusive_never_fabricated(self):
        # None totals (virtual switch) become 0 = absence of evidence.
        probe = CorrelationProbe("agent-a")
        result = probe.diagnose(
            "device-b",
            error_counters_from_interface(iface()),
            error_counters_from_interface(iface(name="Ethernet8")),
        )
        assert result.fault_location == FaultLocation.INCONCLUSIVE
