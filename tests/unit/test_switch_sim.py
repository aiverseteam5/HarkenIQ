"""R6-P2 unit tests: switch simulator gNMI behavior vs the P0 fixture.

Every assertion here mirrors a fact captured live against real SONiC
(docs/designs/r6-p0-spike-report.md). The simulator is exercised through a
real gRPC client — the same wire the GNMIProtocol will use.
"""

import asyncio
import json

import grpc
import pytest
import pytest_asyncio

from harkeniq.mock.switch_sim import SwitchSimulator
from harkeniq.proto.gnmi import gnmi_pb2, gnmi_pb2_grpc


def oc_interface_path(port_name: str, *tail: str) -> gnmi_pb2.Path:
    elems = [gnmi_pb2.PathElem(name="openconfig-interfaces:interfaces"),
             gnmi_pb2.PathElem(name="interface", key={"name": port_name})]
    elems += [gnmi_pb2.PathElem(name=t) for t in tail]
    return gnmi_pb2.Path(elem=elems)


def native_path(target: str, *elems: str) -> gnmi_pb2.Path:
    return gnmi_pb2.Path(
        target=target, elem=[gnmi_pb2.PathElem(name=e) for e in elems]
    )


def payload_of(response: gnmi_pb2.GetResponse) -> dict:
    return json.loads(response.notification[0].update[0].val.json_ietf_val)


async def gnmi_get(stub, path: gnmi_pb2.Path) -> gnmi_pb2.GetResponse:
    # Real-server semantics (P8): the DB target rides the request PREFIX.
    request = gnmi_pb2.GetRequest(encoding=gnmi_pb2.Encoding.JSON_IETF)
    if path.target:
        request.prefix.target = path.target
        request.path.append(gnmi_pb2.Path(elem=path.elem))
    else:
        request.path.append(path)
    return await stub.Get(request)


def enabled_update(port_name: str, enabled: bool) -> gnmi_pb2.Update:
    return gnmi_pb2.Update(
        path=oc_interface_path(port_name, "config", "enabled"),
        val=gnmi_pb2.TypedValue(
            json_ietf_val=json.dumps(
                {"openconfig-interfaces:enabled": enabled}
            ).encode()
        ),
    )


@pytest_asyncio.fixture
async def sim():
    simulator = SwitchSimulator(num_ports=4, lags={"PortChannel1": ["Ethernet0", "Ethernet4"]})
    await simulator.start()
    yield simulator
    await simulator.stop()


@pytest_asyncio.fixture
async def writable_sim():
    simulator = SwitchSimulator(num_ports=4, translib_write=True, client_auth="none")
    await simulator.start()
    yield simulator
    await simulator.stop()


@pytest_asyncio.fixture
async def stub(sim):
    async with grpc.aio.insecure_channel(sim.address) as channel:
        yield gnmi_pb2_grpc.gNMIStub(channel)


@pytest_asyncio.fixture
async def wstub(writable_sim):
    async with grpc.aio.insecure_channel(writable_sim.address) as channel:
        yield gnmi_pb2_grpc.gNMIStub(channel)


class TestCapabilities:
    @pytest.mark.asyncio
    async def test_models_and_encoding_match_fixture(self, stub):
        caps = await stub.Capabilities(gnmi_pb2.CapabilityRequest())
        names = {m.name for m in caps.supported_models}
        assert {"openconfig-interfaces", "openconfig-platform", "sonic-db"} <= names
        assert gnmi_pb2.Encoding.JSON_IETF in caps.supported_encodings
        assert caps.gNMI_version == "0.10.0"


class TestNativeReads:
    @pytest.mark.asyncio
    async def test_device_metadata_identity(self, stub):
        resp = await gnmi_get(
            stub, native_path("CONFIG_DB", "DEVICE_METADATA", "localhost")
        )
        meta = payload_of(resp)
        assert meta["hwsku"] and meta["platform"] and meta["hostname"]

    @pytest.mark.asyncio
    async def test_counters_are_oid_keyed_via_name_map(self, stub):
        name_map = payload_of(await gnmi_get(
            stub, native_path("COUNTERS_DB", "COUNTERS_PORT_NAME_MAP")
        ))
        assert "Ethernet0" in name_map
        oid = name_map["Ethernet0"]
        counters = payload_of(await gnmi_get(
            stub, native_path("COUNTERS_DB", "COUNTERS", oid)
        ))
        assert "SAI_PORT_STAT_IF_IN_ERRORS" in counters

    @pytest.mark.asyncio
    async def test_get_by_port_name_returns_empty_dict_quirk(self, stub):
        # Real-server quirk (P0): name-keyed Get succeeds with {}.
        resp = await gnmi_get(
            stub, native_path("COUNTERS_DB", "COUNTERS", "Ethernet0")
        )
        assert payload_of(resp) == {}

    @pytest.mark.asyncio
    async def test_unknown_oid_not_found(self, stub):
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await gnmi_get(
                stub, native_path("COUNTERS_DB", "COUNTERS", "oid:0xdead")
            )
        assert exc.value.code() == grpc.StatusCode.NOT_FOUND

    @pytest.mark.asyncio
    async def test_malformed_path_errors_never_crashes(self, stub):
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await gnmi_get(stub, native_path("NO_SUCH_DB", "WHAT", "EVER"))
        assert exc.value.code() == grpc.StatusCode.NOT_FOUND
        # Server survives — a follow-up call still works.
        caps = await stub.Capabilities(gnmi_pb2.CapabilityRequest())
        assert caps.gNMI_version

    @pytest.mark.asyncio
    async def test_lag_membership_served(self, stub):
        members = payload_of(await gnmi_get(
            stub, native_path("CONFIG_DB", "PORTCHANNEL_MEMBER")
        ))
        assert "PortChannel1|Ethernet0" in members


class TestOpenConfigReads:
    @pytest.mark.asyncio
    async def test_state_counters_normalized(self, stub):
        counters = payload_of(await gnmi_get(
            stub, oc_interface_path("Ethernet0", "state", "counters")
        ))["openconfig-interfaces:counters"]
        assert "in-errors" in counters and "in-octets" in counters

    @pytest.mark.asyncio
    async def test_oper_status_not_found_fixture_quirk(self, stub):
        # P0: standalone server serves counters but NOT oper-status;
        # GNMIProtocol must read APPL_DB PORT_TABLE for oper state.
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await gnmi_get(
                stub, oc_interface_path("Ethernet0", "state", "oper-status")
            )
        assert exc.value.code() == grpc.StatusCode.NOT_FOUND

    @pytest.mark.asyncio
    async def test_appl_db_serves_oper_status(self, stub):
        entry = payload_of(await gnmi_get(
            stub, native_path("APPL_DB", "PORT_TABLE", "Ethernet0")
        ))
        assert entry["oper_status"] == "up"


class TestSetBehavior:
    @pytest.mark.asyncio
    async def test_write_disabled_by_default(self, stub):
        # Verbatim P0 refusal on the stock server.
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await stub.Set(gnmi_pb2.SetRequest(
                update=[enabled_update("Ethernet0", False)]
            ))
        assert exc.value.code() == grpc.StatusCode.UNIMPLEMENTED
        assert "Translib write is disabled" in exc.value.details()

    @pytest.mark.asyncio
    async def test_write_requires_auth_when_enabled(self):
        sim = SwitchSimulator(translib_write=True, client_auth="password")
        await sim.start()
        try:
            async with grpc.aio.insecure_channel(sim.address) as channel:
                stub = gnmi_pb2_grpc.gNMIStub(channel)
                with pytest.raises(grpc.aio.AioRpcError) as exc:
                    await stub.Set(gnmi_pb2.SetRequest(
                        update=[enabled_update("Ethernet0", False)]
                    ))
                assert exc.value.code() == grpc.StatusCode.UNAUTHENTICATED
        finally:
            await sim.stop()

    @pytest.mark.asyncio
    async def test_enabled_write_applies_and_reads_back(self, writable_sim, wstub):
        resp = await wstub.Set(gnmi_pb2.SetRequest(
            update=[enabled_update("Ethernet0", False)]
        ))
        assert resp.response[0].op == gnmi_pb2.UpdateResult.Operation.UPDATE
        assert writable_sim.state.ports["Ethernet0"].admin_status == "down"
        readback = payload_of(await gnmi_get(
            wstub, oc_interface_path("Ethernet0", "config", "enabled")
        ))
        assert readback["openconfig-interfaces:enabled"] is False

    @pytest.mark.asyncio
    async def test_accepted_but_ignored_pathology(self, writable_sim, wstub):
        # P0 observed a Set accepted (op UPDATE) with no persisted effect.
        # Injectable so read-back verification is testable: the SetResponse
        # alone must never count as action success.
        writable_sim.state.set_accepts_but_ignores = True
        resp = await wstub.Set(gnmi_pb2.SetRequest(
            update=[enabled_update("Ethernet0", False)]
        ))
        assert resp.response[0].op == gnmi_pb2.UpdateResult.Operation.UPDATE
        assert writable_sim.state.ports["Ethernet0"].admin_status == "up"

    @pytest.mark.asyncio
    async def test_raw_db_write_rejected(self, wstub):
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await wstub.Set(gnmi_pb2.SetRequest(update=[gnmi_pb2.Update(
                path=native_path("CONFIG_DB", "PORT", "Ethernet0", "admin_status"),
                val=gnmi_pb2.TypedValue(json_ietf_val=b'"down"'),
            )]))
        assert "not found" in exc.value.details()


class TestSubscribe:
    @pytest.mark.asyncio
    async def test_sample_mode_streams_updates(self, sim, stub):
        request = gnmi_pb2.SubscribeRequest(subscribe=gnmi_pb2.SubscriptionList(
            prefix=gnmi_pb2.Path(target="COUNTERS_DB"),
            mode=gnmi_pb2.SubscriptionList.Mode.STREAM,
            subscription=[gnmi_pb2.Subscription(
                path=gnmi_pb2.Path(elem=[
                    gnmi_pb2.PathElem(name="COUNTERS"),
                    gnmi_pb2.PathElem(name=sim.state.ports["Ethernet0"].oid),
                ]),
                mode=gnmi_pb2.SubscriptionMode.SAMPLE,
                sample_interval=int(1e9),
            )],
            encoding=gnmi_pb2.Encoding.JSON_IETF,
        ))

        async def request_stream():
            yield request
            await asyncio.sleep(3)

        got_sync = False
        updates = 0
        call = stub.Subscribe(request_stream())
        try:
            async with asyncio.timeout(5):
                async for response in call:
                    if response.HasField("sync_response"):
                        got_sync = True
                    elif response.HasField("update"):
                        updates += 1
                    if got_sync and updates >= 2:
                        break
        finally:
            call.cancel()
        assert got_sync and updates >= 2

    @pytest.mark.asyncio
    async def test_target_defined_mode_rejected(self, stub):
        # Verbatim P0 refusal.
        request = gnmi_pb2.SubscribeRequest(subscribe=gnmi_pb2.SubscriptionList(
            prefix=gnmi_pb2.Path(target="COUNTERS_DB"),
            mode=gnmi_pb2.SubscriptionList.Mode.STREAM,
            subscription=[gnmi_pb2.Subscription(
                path=gnmi_pb2.Path(elem=[gnmi_pb2.PathElem(name="COUNTERS")]),
                mode=gnmi_pb2.SubscriptionMode.TARGET_DEFINED,
            )],
        ))

        async def request_stream():
            yield request

        with pytest.raises(grpc.aio.AioRpcError) as exc:
            async for _ in stub.Subscribe(request_stream()):
                pass
        assert exc.value.code() == grpc.StatusCode.INVALID_ARGUMENT
        assert "unsupported subscription mode" in exc.value.details()


class TestFaultInjection:
    @pytest.mark.asyncio
    async def test_crc_ramp_visible_via_gnmi(self, sim, stub):
        sim.state.inject_crc_ramp("Ethernet0", errors_per_s=50)
        sim.state.tick(10)
        counters = payload_of(await gnmi_get(
            stub, native_path(
                "COUNTERS_DB", "COUNTERS", sim.state.ports["Ethernet0"].oid
            )
        ))
        assert int(counters["SAI_PORT_STAT_ETHER_STATS_CRC_ALIGN_ERRORS"]) == 500
        assert int(counters["SAI_PORT_STAT_IF_IN_ERRORS"]) == 500

    @pytest.mark.asyncio
    async def test_optic_decay_visible_in_dom_sensor(self, sim, stub):
        before = payload_of(await gnmi_get(
            stub, native_path("STATE_DB", "TRANSCEIVER_DOM_SENSOR", "Ethernet0")
        ))
        sim.state.inject_optic_rx_decay("Ethernet0", db_per_s=0.1)
        sim.state.tick(20)
        after = payload_of(await gnmi_get(
            stub, native_path("STATE_DB", "TRANSCEIVER_DOM_SENSOR", "Ethernet0")
        ))
        assert float(after["rx1power"]) < float(before["rx1power"]) - 1.0

    @pytest.mark.asyncio
    async def test_prefec_ber_ramp(self, sim, stub):
        sim.state.inject_prefec_ber_ramp("Ethernet0", factor_per_s=1.0)
        sim.state.tick(10)
        dom = payload_of(await gnmi_get(
            stub, native_path("STATE_DB", "TRANSCEIVER_DOM_SENSOR", "Ethernet0")
        ))
        assert float(dom["prefec_ber"]) > 1e-12

    @pytest.mark.asyncio
    async def test_congestion_burst_discards_and_watermark(self, sim, stub):
        sim.state.inject_congestion_burst("Ethernet0", duration_s=30)
        sim.state.tick(10)
        counters = payload_of(await gnmi_get(
            stub, native_path(
                "COUNTERS_DB", "COUNTERS", sim.state.ports["Ethernet0"].oid
            )
        ))
        assert int(counters["SAI_PORT_STAT_IF_IN_DISCARDS"]) == 5000
        assert int(counters["SAI_QUEUE_STAT_SHARED_WATERMARK_BYTES"]) >= 9_000_000

    @pytest.mark.asyncio
    async def test_link_flap_flips_appl_db_oper_status(self, sim, stub):
        sim.state.inject_link_flap("Ethernet0")
        entry = payload_of(await gnmi_get(
            stub, native_path("APPL_DB", "PORT_TABLE", "Ethernet0")
        ))
        assert entry["oper_status"] == "down"

    @pytest.mark.asyncio
    async def test_clear_counters_zeroes(self, sim, stub):
        sim.state.tick(10)
        sim.state.clear_counters("Ethernet0")
        counters = payload_of(await gnmi_get(
            stub, native_path(
                "COUNTERS_DB", "COUNTERS", sim.state.ports["Ethernet0"].oid
            )
        ))
        assert int(counters["SAI_PORT_STAT_IF_IN_OCTETS"]) == 0
