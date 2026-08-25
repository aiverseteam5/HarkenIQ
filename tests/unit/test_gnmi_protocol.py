"""R6-P3 unit tests: GNMIProtocol against the in-process switch simulator.

Named tests from the design doc §7 (reviews 2A, 8A, 9A, T4): staleness →
TimeoutError; reconnect after stream death; rate derivation with wrap/reset
suppression; feature windows keyed to wall clock; auth failure →
ConnectionError; read-back-verified actions incl. the accepted-but-ignored
pathology; the 64-port resource load test.
"""

import asyncio
import resource

import grpc
import pytest
import pytest_asyncio

from harkeniq.mock.switch_sim import SwitchSimulator
from harkeniq.proto.gnmi import gnmi_pb2, gnmi_pb2_grpc
from harkeniq.protocols.device import ProtocolError, create_device_protocol
from harkeniq.protocols.gnmi import GNMIProtocol


async def make_protocol(sim: SwitchSimulator, **kwargs) -> GNMIProtocol:
    proto = GNMIProtocol(
        host="127.0.0.1", port=sim.port, plaintext=True, **kwargs
    )
    await proto.connect({"username": "admin", "password": "pw"})
    return proto


@pytest_asyncio.fixture
async def sim():
    simulator = SwitchSimulator(
        num_ports=4,
        translib_write=True,
        client_auth="none",
        lags={"PortChannel1": ["Ethernet0", "Ethernet4"]},
    )
    await simulator.start()
    yield simulator
    await simulator.stop()


@pytest_asyncio.fixture
async def proto(sim):
    p = await make_protocol(sim)
    yield p
    await p.disconnect()


class TestConnectAndIdentity:
    @pytest.mark.asyncio
    async def test_identity_is_a_switch(self, proto):
        identity = await proto.detect_identity()
        assert identity.device_class == "switch"
        assert identity.vendor == "sonic"
        assert identity.model  # hwsku
        assert proto.name == "gnmi"

    @pytest.mark.asyncio
    async def test_factory_branch(self, sim):
        p = create_device_protocol("gnmi", "127.0.0.1", port=sim.port, plaintext=True)
        assert isinstance(p, GNMIProtocol)

    @pytest.mark.asyncio
    async def test_unreachable_raises_timeout(self):
        p = GNMIProtocol(
            host="127.0.0.1", port=1, plaintext=True, request_timeout=0.5
        )
        with pytest.raises(TimeoutError):
            await p.connect({})

    @pytest.mark.asyncio
    async def test_auth_failure_maps_to_connection_error(self):
        class DenyAuth(gnmi_pb2_grpc.gNMIServicer):
            async def Capabilities(self, request, context):
                await context.abort(
                    grpc.StatusCode.UNAUTHENTICATED, "Unauthenticated"
                )

        server = grpc.aio.server()
        gnmi_pb2_grpc.add_gNMIServicer_to_server(DenyAuth(), server)
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        try:
            p = GNMIProtocol(host="127.0.0.1", port=port, plaintext=True)
            with pytest.raises(ConnectionError):
                await p.connect({"username": "x", "password": "y"})
        finally:
            await server.stop(grace=0.1)


class TestPollSensors:
    @pytest.mark.asyncio
    async def test_interfaces_populated(self, sim, proto):
        device = await proto.poll_sensors()
        assert device.identity.device_class == "switch"
        names = {i.name for i in device.interfaces}
        assert "Ethernet0" in names and len(names) == 4
        eth0 = next(i for i in device.interfaces if i.name == "Ethernet0")
        assert eth0.admin_state == "Up" and eth0.oper_state == "Up"
        assert eth0.lag_name == "PortChannel1"
        assert eth0.optics_rx_power_dbm is not None
        assert eth0.health == "OK"
        assert device.health_rollup.interface == "OK"

    @pytest.mark.asyncio
    async def test_link_down_is_critical(self, sim, proto):
        sim.state.inject_link_flap("Ethernet0")
        device = await proto.poll_sensors()
        eth0 = next(i for i in device.interfaces if i.name == "Ethernet0")
        assert eth0.oper_state == "Down" and eth0.health == "Critical"
        assert device.health_rollup.interface == "Critical"

    @pytest.mark.asyncio
    async def test_stream_rates_reach_poll(self, sim, proto):
        # Real stream at the 1s floor: inject CRC ramp, advance sim time as
        # samples arrive, and rates must appear on the polled device.
        sim.state.inject_crc_ramp("Ethernet0", errors_per_s=100)
        for _ in range(3):
            sim.state.tick(1)
            await asyncio.sleep(1.1)
        device = await proto.poll_sensors()
        eth0 = next(i for i in device.interfaces if i.name == "Ethernet0")
        assert eth0.in_errors_total and eth0.in_errors_total > 0
        assert eth0.crc_error_rate is not None and eth0.crc_error_rate > 0
        assert eth0.crc_error_rate_max is not None


class TestStaleness:
    @pytest.mark.asyncio
    async def test_stale_stream_raises_timeout(self, proto):
        # Deterministic 2A check: backdate the cache past the threshold.
        proto._stream_started = True
        proto._ingest_sample("Ethernet0", {"SAI_PORT_STAT_IF_IN_ERRORS": 0})
        cache = proto._cache["Ethernet0"]
        cache.updated_at -= proto.staleness_multiplier * proto.sample_interval + 5
        with pytest.raises(TimeoutError) as exc:
            await proto.poll_sensors()
        assert "unobserved" in str(exc.value)

    @pytest.mark.asyncio
    async def test_dead_server_goes_stale_not_stale_healthy(self, sim):
        proto = await make_protocol(sim, staleness_multiplier=2.0)
        try:
            await asyncio.sleep(1.5)  # let at least one sample land
            await sim.stop()  # kill the switch
            await asyncio.sleep(3.0)  # past 2x1s threshold
            with pytest.raises(TimeoutError):
                await proto.poll_sensors()
        finally:
            await proto.disconnect()

    @pytest.mark.asyncio
    async def test_first_poll_before_stream_data_works(self, sim):
        proto = await make_protocol(sim)
        try:
            device = await proto.poll_sensors()  # no samples yet — no error
            assert device.interfaces
        finally:
            await proto.disconnect()


class TestRateDerivation:
    def _proto(self) -> GNMIProtocol:
        return GNMIProtocol(host="x", plaintext=True)

    def test_rates_from_deltas_over_wall_clock(self):
        p = self._proto()
        p._ingest_sample("Ethernet0", {"SAI_PORT_STAT_IF_IN_ERRORS": 100}, now=1000.0)
        p._ingest_sample("Ethernet0", {"SAI_PORT_STAT_IF_IN_ERRORS": 150}, now=1010.0)
        assert p._cache["Ethernet0"].rates["SAI_PORT_STAT_IF_IN_ERRORS"] == 5.0

    def test_decreasing_counter_yields_none_never_negative(self):
        p = self._proto()
        p._ingest_sample("Ethernet0", {"SAI_PORT_STAT_IF_IN_ERRORS": 100}, now=1000.0)
        p._ingest_sample("Ethernet0", {"SAI_PORT_STAT_IF_IN_ERRORS": 5}, now=1010.0)
        assert p._cache["Ethernet0"].rates["SAI_PORT_STAT_IF_IN_ERRORS"] is None

    def test_known_reset_suppression_window(self):
        p = self._proto()
        p._ingest_sample("Ethernet0", {"SAI_PORT_STAT_IF_IN_ERRORS": 100}, now=1000.0)
        p.note_counter_reset("Ethernet0", window_s=5.0)
        p._cache["Ethernet0"].suppress_until = 1012.0  # deterministic clock
        p._ingest_sample("Ethernet0", {"SAI_PORT_STAT_IF_IN_ERRORS": 0}, now=1010.0)
        assert p._cache["Ethernet0"].rates["SAI_PORT_STAT_IF_IN_ERRORS"] is None
        # After the window, rates flow again.
        p._ingest_sample("Ethernet0", {"SAI_PORT_STAT_IF_IN_ERRORS": 20}, now=1020.0)
        assert p._cache["Ethernet0"].rates["SAI_PORT_STAT_IF_IN_ERRORS"] == 2.0

    def test_feature_window_resets_on_wall_clock_rollover(self):
        p = GNMIProtocol(host="x", plaintext=True, feature_window_s=60.0)
        p._ingest_sample("Ethernet0", {"SAI_PORT_STAT_ETHER_STATS_CRC_ALIGN_ERRORS": 0}, now=0.0)
        p._ingest_sample("Ethernet0", {"SAI_PORT_STAT_ETHER_STATS_CRC_ALIGN_ERRORS": 300}, now=10.0)
        assert p._cache["Ethernet0"].crc_error_rate_max == 30.0
        # Same window: the max persists (idempotent reads).
        p._ingest_sample("Ethernet0", {"SAI_PORT_STAT_ETHER_STATS_CRC_ALIGN_ERRORS": 310}, now=20.0)
        assert p._cache["Ethernet0"].crc_error_rate_max == 30.0
        # Next wall-clock window: accumulator resets — no double-counting.
        p._ingest_sample("Ethernet0", {"SAI_PORT_STAT_ETHER_STATS_CRC_ALIGN_ERRORS": 320}, now=70.0)
        assert p._cache["Ethernet0"].crc_error_rate_max == 0.2


class TestActions:
    @pytest.mark.asyncio
    async def test_disable_with_readback_success(self, sim, proto):
        result = await proto.execute_action(
            "INTERFACE_DISABLE", {"interface": "Ethernet0"}
        )
        assert result["success"] is True
        assert sim.state.ports["Ethernet0"].admin_status == "down"

    @pytest.mark.asyncio
    async def test_enable_restores(self, sim, proto):
        await proto.execute_action("INTERFACE_DISABLE", {"interface": "Ethernet0"})
        result = await proto.execute_action(
            "INTERFACE_ENABLE", {"interface": "Ethernet0"}
        )
        assert result["success"] is True
        assert sim.state.ports["Ethernet0"].admin_status == "up"

    @pytest.mark.asyncio
    async def test_accepted_but_ignored_fails_via_readback(self, sim, proto):
        # The P0 pathology: SetResponse op:UPDATE, nothing persisted. The
        # read-back verification must fail the action.
        sim.state.set_accepts_but_ignores = True
        result = await proto.execute_action(
            "INTERFACE_DISABLE", {"interface": "Ethernet0"}
        )
        assert result["success"] is False
        assert "read-back" in result["error"] or "not applied" in result["error"]

    @pytest.mark.asyncio
    async def test_write_disabled_server_fails_cleanly(self):
        sim = SwitchSimulator(num_ports=2)  # stock: translib write disabled
        await sim.start()
        try:
            proto = await make_protocol(sim)
            result = await proto.execute_action(
                "INTERFACE_DISABLE", {"interface": "Ethernet0"}
            )
            assert result["success"] is False
            assert "Translib write is disabled" in result["error"]
            await proto.disconnect()
        finally:
            await sim.stop()

    @pytest.mark.asyncio
    async def test_unknown_interface_refused(self, proto):
        result = await proto.execute_action(
            "INTERFACE_DISABLE", {"interface": "Ethernet999"}
        )
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_clear_counters_refused_no_transport(self, proto):
        result = await proto.execute_action(
            "CLEAR_COUNTERS", {"interface": "Ethernet0"}
        )
        assert result["success"] is False
        assert "no gNMI transport" in result["error"]

    @pytest.mark.asyncio
    async def test_unsupported_action_refused_never_faked(self, proto):
        result = await proto.execute_action("IDENTIFY_LED", {})
        assert result["success"] is False


class TestConfigAndInventory:
    @pytest.mark.asyncio
    async def test_collect_config_flat(self, proto):
        config = await proto.collect_config()
        assert config["PORT.Ethernet0.admin_status"] == "up"
        assert "PORT.Ethernet0.speed" in config

    @pytest.mark.asyncio
    async def test_firmware_inventory_omits_unknown(self, proto):
        # The simulator's DEVICE_METADATA has no sonic_version: omitted,
        # never guessed (DeviceProtocol contract).
        assert await proto.collect_firmware_inventory() == []


class TestResourceLoad:
    @pytest.mark.asyncio
    async def test_64_port_stream_bounded_memory(self):
        # Review 9A: constant-space accumulators. Stream a 64-port switch
        # for ~3s at the 1s floor; RSS growth must stay bounded and the
        # cache must hold exactly one entry per port with O(1) state each.
        sim = SwitchSimulator(num_ports=64)
        await sim.start()
        rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        try:
            proto = await make_protocol(sim)
            for _ in range(3):
                sim.state.tick(1)
                await asyncio.sleep(1.1)
            device = await proto.poll_sensors()
            assert len(device.interfaces) == 64
            assert len(proto._cache) == 64
            for cache in proto._cache.values():
                # O(1) per port: two counter dicts + one rates dict, no
                # sample buffers.
                assert len(cache.rates) <= 7
            await proto.disconnect()
        finally:
            await sim.stop()
        rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        growth_mb = (rss_after - rss_before) / 1024
        assert growth_mb < 25, f"RSS grew {growth_mb:.1f}MB streaming 64 ports"
