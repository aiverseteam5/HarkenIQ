"""Switch simulator: SONiC-shaped state model + gNMI server (R6-P2).

The network analogue of MockSimulator/MockIPMIBMC: an in-process test double
that GNMIProtocol (R6-P3) and the exit gate run against, with fault
injection for scenarios a virtual switch cannot produce (optic decay,
pre-FEC BER ramps, CRC storms).

Fidelity contract — every protocol behavior below mirrors the R6-P0 live
capture against real SONiC (`docs/designs/r6-p0-spike-report.md`), not our
reading of the gNMI spec:

- Counters live in COUNTERS_DB keyed by OID; port names resolve through
  COUNTERS_PORT_NAME_MAP. A Get on ``COUNTERS/<port-name>`` returns ``{}``
  (real quirk), and an unknown OID returns NotFound.
- Subscribe supports SAMPLE mode only; TARGET_DEFINED and ON_CHANGE return
  InvalidArgument ("unsupported subscription mode"). Sample intervals below
  the flexcounter floor (1s) are served at the floor.
- Set is REFUSED by default (``Unimplemented: Translib write is disabled``),
  mirroring the stock server. With ``translib_write=True`` it requires auth
  unless ``client_auth="none"``; raw DB paths are rejected; only the
  OpenConfig ``.../config/enabled`` write is modeled.
- Encoding is JSON_IETF; malformed or unsupported paths return proper gRPC
  errors, never a crash or a silently-empty response.
- Identity is served natively: CONFIG_DB ``DEVICE_METADATA/localhost``.
- Optics come from STATE_DB ``TRANSCEIVER_DOM_SENSOR|<port>`` (the SONiC
  table); the virtual switch exports none, so these values exist only here
  and in real ASIC hardware — which is exactly why the simulator carries
  them (§5 fidelity gate).

Time is injected (``time_fn``) so tests are deterministic; counters advance
via ``tick(seconds)``, never wall-clock drift.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import grpc

from harkeniq.proto.gnmi import gnmi_pb2, gnmi_pb2_grpc

logger = logging.getLogger("harkeniq.mock.switch_sim")

# Flexcounter cadence floor observed on real SONiC (counterpoll default).
FLEXCOUNTER_FLOOR_S = 1.0

_GNMI_VERSION = "0.10.0"

# Captured from the real server (P0): the models it advertises.
_SUPPORTED_MODELS = [
    ("openconfig-interfaces", "OpenConfig working group", "1.0.2"),
    ("openconfig-platform", "OpenConfig working group", "1.0.2"),
    ("openconfig-lldp", "OpenConfig working group", "1.0.2"),
    ("openconfig-system", "OpenConfig working group", "1.0.2"),
    ("openconfig-acl", "OpenConfig working group", "1.0.2"),
    ("sonic-db", "SONiC", "0.1.0"),
]


# ---------------------------------------------------------------------------
# State model
# ---------------------------------------------------------------------------


@dataclass
class PortState:
    """One front-panel port."""

    name: str
    oid: str
    admin_status: str = "up"  # CONFIG_DB semantics: "up" | "down"
    oper_status: str = "up"
    speed_mbps: int = 100000
    lag: Optional[str] = None  # parent PortChannel name
    # Monotonic SAI counters (COUNTERS_DB names, per P0 capture)
    counters: dict[str, int] = field(default_factory=dict)
    # Optics (STATE_DB TRANSCEIVER_DOM_SENSOR); None = no transceiver
    optics_tx_power_dbm: Optional[float] = -2.0
    optics_rx_power_dbm: Optional[float] = -3.0
    optics_temperature_c: Optional[float] = 35.0
    pre_fec_ber: Optional[float] = 1e-12
    # Queue watermark (bytes, high-water since last read window)
    queue_watermark_bytes: int = 0
    # Background traffic profile (bytes/sec, errors/sec added on tick)
    traffic_octets_per_s: int = 10_000_000
    _crc_ramp_per_s: float = 0.0
    _optic_decay_db_per_s: float = 0.0
    _ber_ramp_factor_per_s: float = 0.0
    _congestion_until: float = 0.0

    def __post_init__(self) -> None:
        if not self.counters:
            self.counters = {
                "SAI_PORT_STAT_IF_IN_OCTETS": 0,
                "SAI_PORT_STAT_IF_OUT_OCTETS": 0,
                "SAI_PORT_STAT_IF_IN_UCAST_PKTS": 0,
                "SAI_PORT_STAT_IF_OUT_UCAST_PKTS": 0,
                "SAI_PORT_STAT_IF_IN_ERRORS": 0,
                "SAI_PORT_STAT_IF_OUT_ERRORS": 0,
                "SAI_PORT_STAT_IF_IN_DISCARDS": 0,
                "SAI_PORT_STAT_IF_OUT_DISCARDS": 0,
                # ASIC-only counters: absent on the virtual switch, present
                # here because the simulator models real hardware.
                "SAI_PORT_STAT_ETHER_STATS_CRC_ALIGN_ERRORS": 0,
                "SAI_PORT_STAT_IF_IN_FEC_CORRECTABLE_FRAMES": 0,
                "SAI_PORT_STAT_IF_IN_FEC_NOT_CORRECTABLE_FRAMES": 0,
            }


class SwitchState:
    """Deterministic switch: ports, LAGs, counters, faults, tick clock."""

    def __init__(
        self,
        num_ports: int = 8,
        hwsku: str = "Force10-S6000",
        hostname: str = "mock-switch",
        lags: Optional[dict[str, list[str]]] = None,
    ) -> None:
        self.hwsku = hwsku
        self.hostname = hostname
        self.platform = "x86_64-kvm_x86_64-r0"
        self.mac = "52:54:00:12:34:56"
        self.ports: dict[str, PortState] = {}
        for i in range(num_ports):
            # SONiC 4-lane naming: Ethernet0, Ethernet4, ...
            name = f"Ethernet{i * 4}"
            oid = f"oid:0x10000000000{i:02x}"
            self.ports[name] = PortState(name=name, oid=oid)
        self.lags: dict[str, list[str]] = lags or {}
        for lag_name, members in self.lags.items():
            for member in members:
                if member in self.ports:
                    self.ports[member].lag = lag_name
        self.control_plane_stalled = False
        # Set-accepted-but-ignored pathology observed at P0; injectable so
        # the read-back-verification path is testable.
        self.set_accepts_but_ignores = False
        self._now = 0.0

    # -- clock ---------------------------------------------------------------

    @property
    def now(self) -> float:
        return self._now

    def tick(self, seconds: float = 1.0) -> None:
        """Advance simulated time; counters and fault ramps progress."""
        steps = seconds
        self._now += seconds
        for port in self.ports.values():
            if port.oper_status != "up":
                continue
            octets = int(port.traffic_octets_per_s * steps)
            port.counters["SAI_PORT_STAT_IF_IN_OCTETS"] += octets
            port.counters["SAI_PORT_STAT_IF_OUT_OCTETS"] += octets
            port.counters["SAI_PORT_STAT_IF_IN_UCAST_PKTS"] += octets // 1000
            port.counters["SAI_PORT_STAT_IF_OUT_UCAST_PKTS"] += octets // 1000
            if port._crc_ramp_per_s > 0:
                crc = int(port._crc_ramp_per_s * steps)
                port.counters["SAI_PORT_STAT_ETHER_STATS_CRC_ALIGN_ERRORS"] += crc
                port.counters["SAI_PORT_STAT_IF_IN_ERRORS"] += crc
            if port._optic_decay_db_per_s > 0 and port.optics_rx_power_dbm is not None:
                port.optics_rx_power_dbm -= port._optic_decay_db_per_s * steps
            if port._ber_ramp_factor_per_s > 0 and port.pre_fec_ber is not None:
                port.pre_fec_ber *= (1.0 + port._ber_ramp_factor_per_s) ** steps
                port.counters["SAI_PORT_STAT_IF_IN_FEC_CORRECTABLE_FRAMES"] += int(
                    port.pre_fec_ber * 1e12 * steps
                )
            if self._now < port._congestion_until:
                port.queue_watermark_bytes = max(
                    port.queue_watermark_bytes, 9_000_000
                )
                port.counters["SAI_PORT_STAT_IF_IN_DISCARDS"] += int(500 * steps)
            else:
                port.queue_watermark_bytes = int(
                    port.traffic_octets_per_s * 0.001
                )

    # -- fault injection (design doc §7 P2) -----------------------------------

    def inject_link_flap(self, port: str) -> None:
        self.ports[port].oper_status = (
            "down" if self.ports[port].oper_status == "up" else "up"
        )

    def inject_crc_ramp(self, port: str, errors_per_s: float = 50.0) -> None:
        self.ports[port]._crc_ramp_per_s = errors_per_s

    def inject_optic_rx_decay(self, port: str, db_per_s: float = 0.05) -> None:
        self.ports[port]._optic_decay_db_per_s = db_per_s

    def inject_prefec_ber_ramp(self, port: str, factor_per_s: float = 0.5) -> None:
        self.ports[port]._ber_ramp_factor_per_s = factor_per_s

    def inject_congestion_burst(self, port: str, duration_s: float = 30.0) -> None:
        self.ports[port]._congestion_until = self._now + duration_s

    def inject_control_plane_stall(self, stalled: bool = True) -> None:
        self.control_plane_stalled = stalled

    def clear_counters(self, port: str) -> None:
        """The CLEAR_COUNTERS action: zeroes the monotonic counters."""
        for key in self.ports[port].counters:
            self.ports[port].counters[key] = 0


# ---------------------------------------------------------------------------
# gNMI service
# ---------------------------------------------------------------------------


def _path_str(path: gnmi_pb2.Path) -> str:
    return "/".join(e.name for e in path.elem)


def _json_val(payload: Any) -> gnmi_pb2.TypedValue:
    return gnmi_pb2.TypedValue(
        json_ietf_val=json.dumps(payload).encode("utf-8")
    )


def _notification(
    target: str, path: gnmi_pb2.Path, payload: Any, timestamp_ns: int
) -> gnmi_pb2.Notification:
    return gnmi_pb2.Notification(
        timestamp=timestamp_ns,
        prefix=gnmi_pb2.Path(target=target),
        update=[gnmi_pb2.Update(path=path, val=_json_val(payload))],
    )


class SwitchGNMIService(gnmi_pb2_grpc.gNMIServicer):
    """gNMI servicer over a SwitchState, faithful to the P0 capture."""

    def __init__(
        self,
        state: SwitchState,
        translib_write: bool = False,
        client_auth: str = "password",
        time_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        self.state = state
        self.translib_write = translib_write
        self.client_auth = client_auth
        self._time_fn = time_fn or (lambda: self.state.now)

    # -- helpers --------------------------------------------------------------

    def _ts_ns(self) -> int:
        return int(self._time_fn() * 1e9)

    async def _stall_check(self, context: grpc.aio.ServicerContext) -> None:
        if self.state.control_plane_stalled:
            # A stalled control plane neither answers nor errors — it hangs.
            # Bounded so a crashed test doesn't hang the suite forever.
            await asyncio.sleep(3600)

    def _resolve_native(self, target: str, elems: list[str]) -> Any:
        """Native sonic-db reads. Raises KeyError -> NotFound upstream."""
        state = self.state
        if target == "CONFIG_DB":
            if elems == ["DEVICE_METADATA", "localhost"]:
                return {
                    "hwsku": state.hwsku,
                    "platform": state.platform,
                    "mac": state.mac,
                    "hostname": state.hostname,
                    "type": "LeafRouter",
                }
            if elems and elems[0] == "PORT":
                if len(elems) == 1:
                    return {
                        name: {
                            "admin_status": p.admin_status,
                            "speed": str(p.speed_mbps),
                            "alias": name,
                        }
                        for name, p in state.ports.items()
                    }
                port = state.ports[elems[1]]  # KeyError -> NotFound
                return {
                    "admin_status": port.admin_status,
                    "speed": str(port.speed_mbps),
                    "alias": port.name,
                }
            if elems == ["PORTCHANNEL"]:
                return {lag: {"admin_status": "up"} for lag in state.lags}
            if elems == ["PORTCHANNEL_MEMBER"]:
                return {
                    f"{lag}|{member}": {}
                    for lag, members in state.lags.items()
                    for member in members
                }
            raise KeyError("/".join(elems))
        if target == "APPL_DB":
            if elems and elems[0] == "PORT_TABLE" and len(elems) == 2:
                port = state.ports[elems[1]]
                return {
                    "admin_status": port.admin_status,
                    "oper_status": port.oper_status,
                    "speed": str(port.speed_mbps),
                }
            raise KeyError("/".join(elems))
        if target == "STATE_DB":
            if elems and elems[0] == "TRANSCEIVER_DOM_SENSOR" and len(elems) == 2:
                port = state.ports[elems[1]]
                if port.optics_rx_power_dbm is None:
                    raise KeyError("no transceiver")
                return {
                    "tx1power": f"{port.optics_tx_power_dbm:.2f}",
                    "rx1power": f"{port.optics_rx_power_dbm:.2f}",
                    "temperature": f"{port.optics_temperature_c:.1f}",
                    # Not a standard DOM field; carried in the same table the
                    # way vendor builds extend it. None on non-FEC optics.
                    "prefec_ber": (
                        f"{port.pre_fec_ber:.3e}"
                        if port.pre_fec_ber is not None else "N/A"
                    ),
                }
            raise KeyError("/".join(elems))
        if target == "COUNTERS_DB":
            if elems == ["COUNTERS_PORT_NAME_MAP"]:
                return {name: p.oid for name, p in state.ports.items()}
            if elems and elems[0] == "COUNTERS" and len(elems) == 2:
                key = elems[1]
                if key.startswith("oid:"):
                    for port in state.ports.values():
                        if port.oid == key:
                            counters = {
                                k: str(v) for k, v in port.counters.items()
                            }
                            counters["SAI_QUEUE_STAT_SHARED_WATERMARK_BYTES"] = str(
                                port.queue_watermark_bytes
                            )
                            return counters
                    raise KeyError(key)
                # Real-server quirk (P0): Get by port NAME succeeds with {}.
                if key in state.ports:
                    return {}
                raise KeyError(key)
            raise KeyError("/".join(elems))
        raise KeyError(target)

    def _resolve_openconfig(self, elems: list[str]) -> Any:
        """The OpenConfig read surface the real server proved (P0 §b4)."""
        # /openconfig-interfaces:interfaces/interface[name=X]/state/counters
        if (
            len(elems) >= 4
            and elems[0] in ("openconfig-interfaces:interfaces", "interfaces")
            and elems[1].startswith("interface")
        ):
            raise KeyError("keyed path handled in Get")  # pragma: no cover
        raise KeyError("/".join(elems))

    def _oc_counters(self, port: PortState) -> dict:
        c = port.counters
        return {
            "openconfig-interfaces:counters": {
                "in-octets": str(c["SAI_PORT_STAT_IF_IN_OCTETS"]),
                "out-octets": str(c["SAI_PORT_STAT_IF_OUT_OCTETS"]),
                "in-unicast-pkts": str(c["SAI_PORT_STAT_IF_IN_UCAST_PKTS"]),
                "out-unicast-pkts": str(c["SAI_PORT_STAT_IF_OUT_UCAST_PKTS"]),
                "in-errors": str(c["SAI_PORT_STAT_IF_IN_ERRORS"]),
                "out-errors": str(c["SAI_PORT_STAT_IF_OUT_ERRORS"]),
                "in-discards": str(c["SAI_PORT_STAT_IF_IN_DISCARDS"]),
                "out-discards": str(c["SAI_PORT_STAT_IF_OUT_DISCARDS"]),
            }
        }

    # -- RPCs -----------------------------------------------------------------

    async def Capabilities(self, request, context):
        await self._stall_check(context)
        return gnmi_pb2.CapabilityResponse(
            supported_models=[
                gnmi_pb2.ModelData(name=n, organization=o, version=v)
                for n, o, v in _SUPPORTED_MODELS
            ],
            supported_encodings=[gnmi_pb2.Encoding.JSON_IETF],
            gNMI_version=_GNMI_VERSION,
        )

    async def Get(self, request, context):
        await self._stall_check(context)
        notifications = []
        for path in request.path:
            # Fidelity (P8 live finding): the real server resolves the DB
            # from the request PREFIX only; a path-level target is ignored
            # and the lookup falls through to NOT_FOUND.
            target = request.prefix.target
            elems = [e.name for e in path.elem]
            # OpenConfig keyed interface paths
            if elems and elems[0] in (
                "openconfig-interfaces:interfaces", "interfaces"
            ):
                port_name = None
                for e in path.elem:
                    if e.name == "interface" and "name" in e.key:
                        port_name = e.key["name"]
                if port_name is None or port_name not in self.state.ports:
                    await context.abort(
                        grpc.StatusCode.NOT_FOUND,
                        f"Node not found in the given gnmi path {_path_str(path)}",
                    )
                port = self.state.ports[port_name]
                tail = elems[-1]
                if tail == "counters":
                    payload: Any = self._oc_counters(port)
                elif tail == "oper-status":
                    # Faithful to P0: the standalone server returns NotFound
                    # for oper-status. GNMIProtocol reads APPL_DB instead.
                    await context.abort(
                        grpc.StatusCode.NOT_FOUND,
                        f"Node oper-status not found in the given gnmi path "
                        f"{_path_str(path)}",
                    )
                elif tail == "enabled":
                    payload = {
                        "openconfig-interfaces:enabled":
                            port.admin_status == "up"
                    }
                else:
                    await context.abort(
                        grpc.StatusCode.NOT_FOUND,
                        f"Node {tail} not found in the given gnmi path",
                    )
                notifications.append(
                    _notification("", path, payload, self._ts_ns())
                )
                continue
            # Native sonic-db
            if not target:
                await context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    "unsupported path: no target and not an OpenConfig path",
                )
            try:
                payload = self._resolve_native(target, elems)
            except KeyError:
                await context.abort(
                    grpc.StatusCode.NOT_FOUND,
                    f"No valid entry found on {target} with key "
                    f"{':'.join(elems)}",
                )
            notifications.append(
                _notification(target, path, payload, self._ts_ns())
            )
        return gnmi_pb2.GetResponse(notification=notifications)

    async def Set(self, request, context):
        await self._stall_check(context)
        if not self.translib_write:
            # Verbatim real-server refusal (P0).
            await context.abort(
                grpc.StatusCode.UNIMPLEMENTED, "Translib write is disabled"
            )
        if self.client_auth != "none":
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED, "Unauthenticated"
            )
        results = []
        for update in list(request.update) + list(request.replace):
            elems = [e.name for e in update.path.elem]
            if elems and elems[0] in (
                "openconfig-interfaces:interfaces", "interfaces"
            ):
                port_name = None
                for e in update.path.elem:
                    if e.name == "interface" and "name" in e.key:
                        port_name = e.key["name"]
                if port_name not in self.state.ports or elems[-1] != "enabled":
                    await context.abort(
                        grpc.StatusCode.UNKNOWN,
                        f"Node not found in the given gnmi path "
                        f"{_path_str(update.path)}",
                    )
                raw = update.val.json_ietf_val or update.val.json_val
                try:
                    value = json.loads(raw) if raw else None
                except json.JSONDecodeError:
                    await context.abort(
                        grpc.StatusCode.INVALID_ARGUMENT,
                        "Request payload is not valid JSON",
                    )
                if isinstance(value, dict):
                    value = value.get(
                        "openconfig-interfaces:enabled", value.get("enabled")
                    )
                if not isinstance(value, bool):
                    await context.abort(
                        grpc.StatusCode.INVALID_ARGUMENT,
                        "Request payload is empty",
                    )
                if not self.state.set_accepts_but_ignores:
                    port = self.state.ports[port_name]
                    port.admin_status = "up" if value else "down"
                    port.oper_status = "up" if value else "down"
                results.append(gnmi_pb2.UpdateResult(
                    path=update.path,
                    op=gnmi_pb2.UpdateResult.Operation.UPDATE,
                ))
            else:
                # Raw DB writes rejected once translib owns the target (P0).
                await context.abort(
                    grpc.StatusCode.UNKNOWN,
                    f"Node {elems[0] if elems else '?'} not found in the "
                    f"given gnmi path",
                )
        return gnmi_pb2.SetResponse(
            response=results, timestamp=self._ts_ns()
        )

    async def Subscribe(self, request_iterator, context):
        await self._stall_check(context)
        first = await request_iterator.__anext__()
        if not first.HasField("subscribe"):
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT, "expected SubscriptionList"
            )
        sub_list = first.subscribe
        if sub_list.mode != gnmi_pb2.SubscriptionList.Mode.STREAM:
            await context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "only STREAM subscriptions are supported",
            )
        for sub in sub_list.subscription:
            if sub.mode != gnmi_pb2.SubscriptionMode.SAMPLE:
                # Verbatim real-server refusal (P0).
                mode_name = gnmi_pb2.SubscriptionMode.Name(sub.mode)
                await context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    f"unsupported subscription mode, {mode_name}",
                )
        target = sub_list.prefix.target

        async def emit_all() -> list[gnmi_pb2.SubscribeResponse]:
            out = []
            for sub in sub_list.subscription:
                elems = [e.name for e in sub.path.elem]
                try:
                    payload = self._resolve_native(target, elems)
                except KeyError:
                    continue
                out.append(gnmi_pb2.SubscribeResponse(
                    update=_notification(
                        target, sub.path, payload, self._ts_ns()
                    )
                ))
            return out

        for response in await emit_all():
            yield response
        yield gnmi_pb2.SubscribeResponse(sync_response=True)

        # Flexcounter floor: intervals below 1s are served at 1s (P0).
        interval_ns = max(
            (s.sample_interval for s in sub_list.subscription), default=0
        )
        interval_s = max(interval_ns / 1e9, FLEXCOUNTER_FLOOR_S)
        while True:
            await asyncio.sleep(interval_s)
            if self.state.control_plane_stalled:
                await asyncio.sleep(3600)
            for response in await emit_all():
                yield response


# ---------------------------------------------------------------------------
# Server wrapper (in-process for tests; standalone for compose)
# ---------------------------------------------------------------------------


class SwitchSimulator:
    """Owns a SwitchState + grpc.aio server; mirrors MockSimulator's shape."""

    def __init__(
        self,
        num_ports: int = 8,
        port: int = 0,
        host: str = "127.0.0.1",
        translib_write: bool = False,
        client_auth: str = "password",
        lags: Optional[dict[str, list[str]]] = None,
    ) -> None:
        self.state = SwitchState(num_ports=num_ports, lags=lags)
        self.service = SwitchGNMIService(
            self.state,
            translib_write=translib_write,
            client_auth=client_auth,
        )
        self._requested_port = port
        self._host = host
        self.port: Optional[int] = None
        self._server: Optional[grpc.aio.Server] = None

    async def start(self) -> None:
        self._server = grpc.aio.server()
        gnmi_pb2_grpc.add_gNMIServicer_to_server(self.service, self._server)
        self.port = self._server.add_insecure_port(
            f"{self._host}:{self._requested_port}"
        )
        await self._server.start()
        logger.info("Switch simulator gNMI on %s:%d", self._host, self.port)

    async def stop(self) -> None:
        if self._server is not None:
            await self._server.stop(grace=0.1)
            self._server = None

    @property
    def address(self) -> str:
        return f"127.0.0.1:{self.port}"
