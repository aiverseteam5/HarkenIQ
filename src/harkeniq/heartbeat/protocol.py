"""Heartbeat packet serialization + HMAC (Doc 06 §9.2, decision D18).

Packets are compact JSON with an ``hmac`` field carrying an HMAC-SHA256
hex digest computed over the canonical JSON encoding (sorted keys, no
whitespace) of the payload *without* the ``hmac`` field. Total packet
size must stay under 576 bytes so UDP datagrams never fragment.
"""

from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json

from harkeniq.errors import HeartbeatError
from harkeniq.models import HeartbeatPacket

PROTOCOL_VERSION = 1
MAX_PACKET_BYTES = 576

_REQUIRED_FIELDS = ("v", "agent_id", "name", "seq", "ts", "state", "health_summary", "hmac")


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def _digest(payload: dict, secret: str) -> str:
    return hmac_mod.new(secret.encode(), _canonical(payload), hashlib.sha256).hexdigest()


def build_packet(packet: HeartbeatPacket, secret: str) -> bytes:
    """Serialize + sign a heartbeat packet for the wire."""
    payload = {
        "v": packet.v,
        "agent_id": packet.agent_id,
        "name": packet.name,
        "seq": packet.seq,
        "ts": packet.ts,
        "state": packet.state,
        "health_summary": packet.health_summary,
    }
    payload["hmac"] = _digest(payload, secret)
    data = _canonical(payload)
    if len(data) > MAX_PACKET_BYTES:
        raise HeartbeatError(
            f"Heartbeat packet is {len(data)} bytes, exceeds {MAX_PACKET_BYTES}"
        )
    return data


def parse_packet(data: bytes, secret: str) -> HeartbeatPacket:
    """Parse + authenticate a received datagram.

    Raises HeartbeatError on malformed JSON, missing fields, unsupported
    version, or HMAC mismatch. Invalid packets must be rejected and logged
    as WARNING by the caller (Doc 06 §9.2).
    """
    if len(data) > MAX_PACKET_BYTES:
        raise HeartbeatError(f"Packet too large: {len(data)} bytes")
    try:
        payload = json.loads(data.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise HeartbeatError(f"Malformed heartbeat packet: {e}") from e
    if not isinstance(payload, dict):
        raise HeartbeatError("Heartbeat payload must be a JSON object")

    missing = [f for f in _REQUIRED_FIELDS if f not in payload]
    if missing:
        raise HeartbeatError(f"Heartbeat packet missing fields: {', '.join(missing)}")
    if payload["v"] != PROTOCOL_VERSION:
        raise HeartbeatError(f"Unsupported heartbeat protocol version: {payload['v']}")

    received_mac = payload.pop("hmac")
    expected_mac = _digest(payload, secret)
    if not isinstance(received_mac, str) or not hmac_mod.compare_digest(
        received_mac, expected_mac
    ):
        raise HeartbeatError("Heartbeat HMAC verification failed")

    try:
        return HeartbeatPacket(
            v=int(payload["v"]),
            agent_id=str(payload["agent_id"]),
            name=str(payload["name"]),
            seq=int(payload["seq"]),
            ts=float(payload["ts"]),
            state=str(payload["state"]),
            health_summary=dict(payload["health_summary"]),
            hmac=received_mac,
        )
    except (TypeError, ValueError) as e:
        raise HeartbeatError(f"Heartbeat field has wrong type: {e}") from e
