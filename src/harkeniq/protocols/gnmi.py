"""GNMIProtocol -- DeviceProtocol implementation for SONiC switches (R6-P3).

gNMI over gRPC (vendored proto, no client library — design doc §7 decision
3). Every wire behavior matches the R6-P0 live capture against real SONiC
(`docs/designs/r6-p0-spike-report.md`):

- Counters stream via Subscribe SAMPLE from COUNTERS_DB, OID-keyed through
  COUNTERS_PORT_NAME_MAP. Sample intervals below the 1s flexcounter floor
  buy nothing and are not requested.
- Identity is CONFIG_DB DEVICE_METADATA; oper state is APPL_DB PORT_TABLE
  (the OpenConfig oper-status path is NotFound on the standalone server);
  optics are STATE_DB TRANSCEIVER_DOM_SENSOR.
- Writes go through OpenConfig paths and are verified by READ-BACK — the
  P0 spike observed a SetResponse op:UPDATE with no persisted effect, so a
  SetResponse is never proof an action landed.

Staleness contract (review 2A): the Subscribe stream feeds a cache with
per-entry timestamps; ``poll_sensors()`` raises TimeoutError when the cache
is stale past ``staleness_multiplier x sample_interval`` — a dead stream
surfaces as device-unreachable through the existing poller paths, never as
stale-but-healthy data (OQ-12). The stream task reconnects with exponential
backoff.

Rate derivation (review T4): counters are monotonic; baselines and skills
consume per-second RATES computed here over wall-clock deltas. A decreasing
counter yields ``None`` for that interval (wrap/reset — never fabricated),
and ``note_counter_reset()`` opens a suppression window after CLEAR_COUNTERS
so a zeroed counter never reads as recovery.

Window features (review 9A/T4): constant-space accumulators (max-so-far),
keyed to wall-clock windows so ``poll_sensors()`` stays idempotent — a
retry inside the same window returns the same feature values.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import grpc

from harkeniq.proto.gnmi import gnmi_pb2, gnmi_pb2_grpc
from harkeniq.protocols.device import ProtocolError
from harkeniq.protocols.model import (
    DeviceIdentity,
    NormalizedDevice,
    NormalizedInterface,
    compute_health_rollup,
)

logger = logging.getLogger("harkeniq.protocols.gnmi")

#: Flexcounter cadence floor on SONiC (P0): never subscribe faster.
MIN_SAMPLE_INTERVAL_S = 1.0
#: Reconnect backoff bounds for the subscribe stream.
_BACKOFF_INITIAL_S = 1.0
_BACKOFF_MAX_S = 30.0

# COUNTERS_DB -> NormalizedInterface rate-field mapping.
_RATE_FIELDS = {
    "SAI_PORT_STAT_IF_IN_ERRORS": "in_error_rate",
    "SAI_PORT_STAT_IF_OUT_ERRORS": "out_error_rate",
    "SAI_PORT_STAT_IF_IN_DISCARDS": "in_discard_rate",
    "SAI_PORT_STAT_IF_OUT_DISCARDS": "out_discard_rate",
    "SAI_PORT_STAT_ETHER_STATS_CRC_ALIGN_ERRORS": "crc_error_rate",
    "SAI_PORT_STAT_IF_IN_OCTETS": "in_octet_rate",
    "SAI_PORT_STAT_IF_OUT_OCTETS": "out_octet_rate",
}
_TOTAL_FIELDS = {
    "SAI_PORT_STAT_IF_IN_ERRORS": "in_errors_total",
    "SAI_PORT_STAT_IF_OUT_ERRORS": "out_errors_total",
    "SAI_PORT_STAT_IF_IN_DISCARDS": "in_discards_total",
    "SAI_PORT_STAT_IF_OUT_DISCARDS": "out_discards_total",
    "SAI_PORT_STAT_ETHER_STATS_CRC_ALIGN_ERRORS": "crc_errors_total",
}


def _native_path(target: str, *elems: str) -> gnmi_pb2.Path:
    return gnmi_pb2.Path(
        target=target, elem=[gnmi_pb2.PathElem(name=e) for e in elems]
    )


def _oc_path(port_name: str, *tail: str) -> gnmi_pb2.Path:
    elems = [gnmi_pb2.PathElem(name="openconfig-interfaces:interfaces"),
             gnmi_pb2.PathElem(name="interface", key={"name": port_name})]
    elems += [gnmi_pb2.PathElem(name=t) for t in tail]
    return gnmi_pb2.Path(elem=elems)


@dataclass
class _PortCache:
    """Latest stream sample + rate/feature state for one port."""

    counters: dict[str, int] = field(default_factory=dict)
    updated_at: Optional[float] = None
    prev_counters: dict[str, int] = field(default_factory=dict)
    prev_at: Optional[float] = None
    rates: dict[str, Optional[float]] = field(default_factory=dict)
    queue_watermark_bytes: Optional[int] = None
    # Window accumulators (O(1); reset on wall-clock window rollover)
    window_id: int = -1
    crc_error_rate_max: Optional[float] = None
    queue_occupancy_max: Optional[float] = None
    # Reset suppression: rates are None until this timestamp
    suppress_until: float = 0.0


class GNMIProtocol:
    """DeviceProtocol for gNMI switches (SONiC anchor)."""

    def __init__(
        self,
        host: str,
        port: int = 8080,
        plaintext: bool = False,
        sample_interval: float = 1.0,
        staleness_multiplier: float = 3.0,
        feature_window_s: float = 60.0,
        request_timeout: float = 10.0,
    ) -> None:
        self.host = host
        self.port = port
        self.plaintext = plaintext
        self.sample_interval = max(sample_interval, MIN_SAMPLE_INTERVAL_S)
        self.staleness_multiplier = staleness_multiplier
        self.feature_window_s = feature_window_s
        self.request_timeout = request_timeout
        self._channel: Optional[grpc.aio.Channel] = None
        self._stub: Optional[gnmi_pb2_grpc.gNMIStub] = None
        self._metadata: list[tuple[str, str]] = []
        self._identity: Optional[DeviceIdentity] = None
        self._name_map: dict[str, str] = {}  # port name -> oid
        self._lag_map: dict[str, str] = {}  # port name -> PortChannel
        self._cache: dict[str, _PortCache] = {}
        self._stream_task: Optional[asyncio.Task] = None
        self._stream_started = False

    # -- DeviceProtocol surface ------------------------------------------------

    @property
    def name(self) -> str:
        return "gnmi"

    async def connect(self, credentials: dict) -> None:
        username = credentials.get("username", "")
        password = credentials.get("password", "")
        if username:
            # SONiC telemetry reads user auth from these metadata keys.
            self._metadata = [("username", username), ("password", password)]
        address = f"{self.host}:{self.port}"
        try:
            if self.plaintext:
                self._channel = grpc.aio.insecure_channel(address)
            else:
                # Real SONiC serves TLS (self-signed in the field). Python
                # gRPC cannot skip verification, so pin the presented cert
                # (TOFU, same posture as SM key pinning in A2.4). The fetch
                # must offer ALPN h2 — gRPC servers reset bare TLS probes.
                pem = await asyncio.to_thread(
                    _fetch_server_cert_pem, self.host, self.port
                )
                creds = grpc.ssl_channel_credentials(
                    root_certificates=pem.encode()
                )
                # Self-signed certs carry their own CN, not our address;
                # pinning makes the name check redundant, so point it at
                # the pinned cert's subject.
                options = []
                cn = _cert_common_name(pem)
                if cn:
                    options.append(("grpc.ssl_target_name_override", cn))
                self._channel = grpc.aio.secure_channel(
                    address, creds, options=options
                )
            self._stub = gnmi_pb2_grpc.gNMIStub(self._channel)
            # Liveness + capability check.
            await self._stub.Capabilities(
                gnmi_pb2.CapabilityRequest(),
                metadata=self._metadata,
                timeout=self.request_timeout,
            )
            await self._refresh_topology()
        except grpc.aio.AioRpcError as e:
            await self.disconnect()
            if e.code() == grpc.StatusCode.UNAUTHENTICATED:
                raise ConnectionError(f"gNMI auth failed: {e.details()}") from e
            raise TimeoutError(
                f"gNMI unreachable at {address}: {e.details()}"
            ) from e
        except (OSError, ssl.SSLError) as e:
            await self.disconnect()
            raise TimeoutError(f"gNMI unreachable at {address}: {e}") from e
        self._start_stream()

    async def disconnect(self) -> None:
        if self._stream_task is not None:
            self._stream_task.cancel()
            try:
                await self._stream_task
            except (asyncio.CancelledError, Exception):
                pass
            self._stream_task = None
        self._stream_started = False
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
            self._stub = None

    async def detect_identity(self) -> DeviceIdentity:
        meta = await self._get_json(
            _native_path("CONFIG_DB", "DEVICE_METADATA", "localhost")
        )
        self._identity = DeviceIdentity(
            vendor="sonic",
            model=meta.get("hwsku", ""),
            controller_type=meta.get("platform", ""),
            firmware_version=meta.get("sonic_version", ""),
            service_tag=meta.get("mac", ""),
            system_id=meta.get("hostname", ""),
            device_class="switch",
        )
        return self._identity

    async def poll_sensors(self) -> NormalizedDevice:
        if self._stub is None:
            raise ProtocolError("gNMI not connected")
        self._check_staleness()
        if self._identity is None:
            await self.detect_identity()

        interfaces: list[NormalizedInterface] = []
        port_config = await self._get_json(_native_path("CONFIG_DB", "PORT"))
        for port_name in sorted(self._name_map):
            config = port_config.get(port_name, {})
            iface = NormalizedInterface(
                name=port_name,
                admin_state=_updown(config.get("admin_status")),
                speed_mbps=_int_or_none(config.get("speed")),
                lag_name=self._lag_map.get(port_name),
            )
            # Oper state: APPL_DB PORT_TABLE (P0: the OC path is NotFound).
            try:
                appl = await self._get_json(
                    _native_path("APPL_DB", "PORT_TABLE", port_name)
                )
                iface.oper_state = _updown(appl.get("oper_status"))
            except ProtocolError:
                iface.oper_state = "Unknown"
            # Optics: STATE_DB DOM table; absent = no transceiver, keep None.
            try:
                dom = await self._get_json(_native_path(
                    "STATE_DB", "TRANSCEIVER_DOM_SENSOR", port_name
                ))
                iface.optics_tx_power_dbm = _float_or_none(dom.get("tx1power"))
                iface.optics_rx_power_dbm = _float_or_none(dom.get("rx1power"))
                iface.optics_temperature_c = _float_or_none(
                    dom.get("temperature")
                )
                iface.pre_fec_ber = _float_or_none(dom.get("prefec_ber"))
            except ProtocolError:
                pass
            # Counters/rates/features from the stream cache.
            cache = self._cache.get(port_name)
            if cache is not None and cache.updated_at is not None:
                for sai, attr in _TOTAL_FIELDS.items():
                    if sai in cache.counters:
                        setattr(iface, attr, cache.counters[sai])
                for sai, attr in _RATE_FIELDS.items():
                    if sai in cache.rates:
                        setattr(iface, attr, cache.rates[sai])
                iface.crc_error_rate_max = cache.crc_error_rate_max
                iface.queue_occupancy_max_pct = cache.queue_occupancy_max
            iface.health = _port_health(iface)
            interfaces.append(iface)

        device = NormalizedDevice(
            identity=self._identity or DeviceIdentity(device_class="switch"),
            interfaces=interfaces,
        )
        device.health_rollup = compute_health_rollup(device)
        return device

    async def collect_config(self) -> dict:
        """Flat config dict for drift detection: PORT admin/speed."""
        config: dict[str, Any] = {}
        try:
            ports = await self._get_json(_native_path("CONFIG_DB", "PORT"))
        except ProtocolError:
            return {}
        for name, fields_ in sorted(ports.items()):
            for key in ("admin_status", "speed"):
                if key in fields_:
                    config[f"PORT.{name}.{key}"] = fields_[key]
        return config

    async def collect_firmware_inventory(self) -> list[dict]:
        try:
            meta = await self._get_json(
                _native_path("CONFIG_DB", "DEVICE_METADATA", "localhost")
            )
        except ProtocolError:
            return []
        inventory = []
        if meta.get("sonic_version"):
            inventory.append({
                "component": "nos",
                "name": "SONiC",
                "version": meta["sonic_version"],
            })
        # Components the platform does not expose are omitted, never guessed.
        return inventory

    async def execute_action(self, action_type: str, params: dict) -> dict:
        started = time.monotonic()

        def done(success: bool, error: str = "") -> dict:
            result: dict[str, Any] = {
                "success": success,
                "duration_ms": (time.monotonic() - started) * 1000.0,
            }
            if error:
                result["error"] = error
            return result

        if action_type in ("INTERFACE_DISABLE", "INTERFACE_ENABLE"):
            port_name = params.get("interface", "")
            if port_name not in self._name_map:
                return done(False, f"unknown interface {port_name!r}")
            enabled = action_type == "INTERFACE_ENABLE"
            try:
                await self._set_enabled(port_name, enabled)
            except ProtocolError as e:
                return done(False, str(e))
            # READ-BACK verification (P0: a SetResponse proves nothing).
            try:
                readback = await self._get_json(
                    _oc_path(port_name, "config", "enabled")
                )
            except ProtocolError as e:
                return done(False, f"read-back failed: {e}")
            actual = readback.get("openconfig-interfaces:enabled")
            if actual is not enabled:
                return done(
                    False,
                    "set accepted but not applied: read-back shows "
                    f"enabled={actual!r} (wanted {enabled}) — the P0 "
                    "accepted-but-ignored pathology",
                )
            if action_type == "INTERFACE_DISABLE":
                self.note_counter_reset(port_name)
            return done(True)

        if action_type == "CLEAR_COUNTERS":
            # No gNMI surface for counter clearing on SONiC (CLI-only).
            # Refused, never faked (design doc §7 P6).
            return done(
                False, "CLEAR_COUNTERS has no gNMI transport on this NOS"
            )

        return done(
            False, f"action {action_type!r} not supported by gnmi protocol"
        )

    # -- rate/feature bookkeeping ----------------------------------------------

    def note_counter_reset(self, port_name: str, window_s: float = 5.0) -> None:
        """Open a rate-suppression window after a known counter reset.

        Called after CLEAR_COUNTERS (and admin bounces): the next decreasing
        sample is expected, and rates stay None until the window passes so a
        zeroed counter never reads as recovery (review T4).
        """
        cache = self._cache.setdefault(port_name, _PortCache())
        cache.suppress_until = time.time() + window_s

    def _ingest_sample(
        self, port_name: str, counters: dict[str, int], now: Optional[float] = None
    ) -> None:
        now = time.time() if now is None else now
        cache = self._cache.setdefault(port_name, _PortCache())
        cache.prev_counters, cache.prev_at = cache.counters, cache.updated_at
        cache.counters, cache.updated_at = counters, now
        watermark = counters.get("SAI_QUEUE_STAT_SHARED_WATERMARK_BYTES")
        cache.queue_watermark_bytes = watermark

        suppressed = now < cache.suppress_until
        cache.rates = {}
        if cache.prev_at is not None and cache.updated_at > cache.prev_at:
            dt = cache.updated_at - cache.prev_at
            for sai in _RATE_FIELDS:
                new = counters.get(sai)
                old = cache.prev_counters.get(sai)
                if new is None or old is None or suppressed:
                    cache.rates[sai] = None
                elif new < old:
                    # Wrap or unannounced reset: never fabricate a rate.
                    cache.rates[sai] = None
                else:
                    cache.rates[sai] = (new - old) / dt

        # Wall-clock window features (idempotent reads within a window).
        window_id = int(now // self.feature_window_s)
        if window_id != cache.window_id:
            cache.window_id = window_id
            cache.crc_error_rate_max = None
            cache.queue_occupancy_max = None
        crc_rate = cache.rates.get("SAI_PORT_STAT_ETHER_STATS_CRC_ALIGN_ERRORS")
        if crc_rate is not None:
            cache.crc_error_rate_max = max(
                cache.crc_error_rate_max or 0.0, crc_rate
            )
        if watermark is not None:
            # Expressed as % of a nominal 10MB shared buffer.
            occupancy = min(100.0, watermark / 10_000_000 * 100.0)
            cache.queue_occupancy_max = max(
                cache.queue_occupancy_max or 0.0, occupancy
            )

    def _check_staleness(self) -> None:
        """Review 2A: a dead stream is device-unreachable, never stale-healthy."""
        if not self._stream_started:
            return  # first poll may run before the stream produced data
        threshold = self.staleness_multiplier * self.sample_interval
        newest = max(
            (c.updated_at for c in self._cache.values() if c.updated_at),
            default=None,
        )
        if newest is None:
            return
        age = time.time() - newest
        if age > threshold:
            raise TimeoutError(
                f"gNMI stream stale: newest sample {age:.1f}s old "
                f"(threshold {threshold:.1f}s) — device unobserved"
            )

    # -- internals ---------------------------------------------------------------

    async def _get_json(self, path: gnmi_pb2.Path) -> dict:
        if self._stub is None:
            raise ProtocolError("gNMI not connected")
        # Real SONiC resolves the DB name from the request PREFIX target,
        # not a path-level target (P8 live finding — path-level targets get
        # NOT_FOUND). Hoist it.
        request = gnmi_pb2.GetRequest(encoding=gnmi_pb2.Encoding.JSON_IETF)
        if path.target:
            request.prefix.target = path.target
            request.path.append(gnmi_pb2.Path(elem=path.elem))
        else:
            request.path.append(path)
        try:
            response = await self._stub.Get(
                request,
                metadata=self._metadata,
                timeout=self.request_timeout,
            )
        except grpc.aio.AioRpcError as e:
            raise ProtocolError(f"gNMI Get failed: {e.details()}") from e
        try:
            return json.loads(
                response.notification[0].update[0].val.json_ietf_val
            )
        except (IndexError, json.JSONDecodeError) as e:
            raise ProtocolError(f"malformed gNMI response: {e}") from e

    async def _refresh_topology(self) -> None:
        self._name_map = {
            k: str(v) for k, v in (await self._get_json(
                _native_path("COUNTERS_DB", "COUNTERS_PORT_NAME_MAP")
            )).items()
        }
        try:
            members = await self._get_json(
                _native_path("CONFIG_DB", "PORTCHANNEL_MEMBER")
            )
            self._lag_map = {}
            for key in members:
                lag, _, member = key.partition("|")
                if member:
                    self._lag_map[member] = lag
        except ProtocolError:
            self._lag_map = {}

    async def _set_enabled(self, port_name: str, enabled: bool) -> None:
        assert self._stub is not None
        update = gnmi_pb2.Update(
            path=_oc_path(port_name, "config", "enabled"),
            val=gnmi_pb2.TypedValue(json_ietf_val=json.dumps(
                {"openconfig-interfaces:enabled": enabled}
            ).encode()),
        )
        try:
            await self._stub.Set(
                gnmi_pb2.SetRequest(update=[update]),
                metadata=self._metadata,
                timeout=self.request_timeout,
            )
        except grpc.aio.AioRpcError as e:
            raise ProtocolError(f"gNMI Set failed: {e.details()}") from e

    def _start_stream(self) -> None:
        if self._stream_task is None:
            self._stream_task = asyncio.get_running_loop().create_task(
                self._stream_loop(), name="gnmi-subscribe"
            )

    async def _stream_loop(self) -> None:
        backoff = _BACKOFF_INITIAL_S
        while True:
            try:
                await self._consume_stream()
                backoff = _BACKOFF_INITIAL_S
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — stream must survive anything
                logger.warning(
                    "gNMI subscribe stream lost (%s); reconnecting in %.1fs",
                    e, backoff,
                )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX_S)

    async def _consume_stream(self) -> None:
        if self._stub is None or not self._name_map:
            raise ProtocolError("stream prerequisites missing")
        oid_to_port = {oid: name for name, oid in self._name_map.items()}
        subscriptions = [
            gnmi_pb2.Subscription(
                path=gnmi_pb2.Path(elem=[
                    gnmi_pb2.PathElem(name="COUNTERS"),
                    gnmi_pb2.PathElem(name=oid),
                ]),
                mode=gnmi_pb2.SubscriptionMode.SAMPLE,
                sample_interval=int(self.sample_interval * 1e9),
            )
            for oid in oid_to_port
        ]
        request = gnmi_pb2.SubscribeRequest(subscribe=gnmi_pb2.SubscriptionList(
            prefix=gnmi_pb2.Path(target="COUNTERS_DB"),
            mode=gnmi_pb2.SubscriptionList.Mode.STREAM,
            subscription=subscriptions,
            encoding=gnmi_pb2.Encoding.JSON_IETF,
        ))

        async def request_stream():
            yield request
            # Keep the client half open for the server stream's lifetime.
            await asyncio.Event().wait()

        call = self._stub.Subscribe(request_stream(), metadata=self._metadata)
        async for response in call:
            if response.HasField("sync_response"):
                self._stream_started = True
                continue
            if not response.HasField("update"):
                continue
            notification = response.update
            if not notification.update:
                continue
            path = notification.update[0].path
            elems = [e.name for e in path.elem]
            if len(elems) != 2 or elems[0] != "COUNTERS":
                continue
            port_name = oid_to_port.get(elems[1])
            if port_name is None:
                continue
            try:
                payload = json.loads(
                    notification.update[0].val.json_ietf_val
                )
                counters = {k: int(v) for k, v in payload.items()}
            except (json.JSONDecodeError, ValueError):
                continue
            self._ingest_sample(port_name, counters)


# -- helpers -------------------------------------------------------------------


def _fetch_server_cert_pem(host: str, port: int, timeout: float = 10.0) -> str:
    """Fetch the server's TLS cert for TOFU pinning, offering ALPN h2."""
    import socket

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.set_alpn_protocols(["h2"])
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with context.wrap_socket(sock, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)
    return ssl.DER_cert_to_PEM_cert(der)


def _cert_common_name(pem: str) -> str:
    """Subject CN of a PEM cert (empty string when unparseable)."""
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID

        cert = x509.load_pem_x509_certificate(pem.encode())
        attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        return str(attrs[0].value) if attrs else ""
    except Exception:  # noqa: BLE001 — fall back to strict name check
        return ""


def _updown(value: Optional[str]) -> str:
    if value == "up":
        return "Up"
    if value == "down":
        return "Down"
    return "Unknown"


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _port_health(iface: NormalizedInterface) -> str:
    """Link down while admin up is the one health fact the protocol asserts;
    everything subtler (rates, optics) is the skill engine's job."""
    if iface.admin_state == "Up" and iface.oper_state == "Down":
        return "Critical"
    if iface.oper_state == "Unknown" and iface.admin_state == "Unknown":
        return "Unknown"
    return "OK"
