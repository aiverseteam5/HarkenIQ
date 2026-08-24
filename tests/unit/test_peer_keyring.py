"""Tests for PeerKeyRing and message envelope (R3b-2 Phase 1)."""

from __future__ import annotations

import hashlib
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from harkeniq.autonomy.peer_keyring import PeerKeyRing
from harkeniq.heartbeat.protocol import (
    MSG_CLAIM,
    MSG_CLAIM_ACK,
    MSG_HEARTBEAT,
    MSG_SUSPICION,
    build_envelope,
    build_packet,
    parse_envelope,
    parse_packet,
)
from harkeniq.errors import HeartbeatError
from harkeniq.models import HeartbeatPacket


# -- helpers ----------------------------------------------------------------

def _keygen():
    """Generate an Ed25519 keypair and derive agent_id."""
    private = Ed25519PrivateKey.generate()
    public = private.public_key()
    pem = public.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    agent_id = hashlib.sha256(pem).hexdigest()[:16]
    return private, public, pem, agent_id


def _canonical_json(d: dict) -> bytes:
    return json.dumps(d, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sign_bundle(peer_keys: dict[str, bytes], sm_private: Ed25519PrivateKey) -> bytes:
    canonical = _canonical_json(
        {k: v.hex() for k, v in sorted(peer_keys.items())}
    )
    return sm_private.sign(canonical)


# -- PeerKeyRing tests ------------------------------------------------------


class TestPeerKeyRingStore:
    def test_add_and_lookup(self):
        _, pub, _, agent_id = _keygen()
        ring = PeerKeyRing()
        ring.add_key(agent_id, pub)
        assert ring.get_key(agent_id) is pub
        assert agent_id in ring
        assert len(ring) == 1

    def test_unknown_peer_returns_none(self):
        ring = PeerKeyRing()
        assert ring.get_key("nonexistent") is None
        assert "nonexistent" not in ring

    def test_known_peers_list(self):
        ring = PeerKeyRing()
        _, pub_a, _, id_a = _keygen()
        _, pub_b, _, id_b = _keygen()
        ring.add_key(id_a, pub_a)
        ring.add_key(id_b, pub_b)
        assert sorted(ring.known_peers()) == sorted([id_a, id_b])


class TestPeerKeyRingVerify:
    def test_verify_valid_signature(self):
        priv, pub, _, agent_id = _keygen()
        ring = PeerKeyRing()
        ring.add_key(agent_id, pub)
        message = b"test message"
        sig = priv.sign(message)
        assert ring.verify(agent_id, message, sig) is True

    def test_reject_tampered_message(self):
        priv, pub, _, agent_id = _keygen()
        ring = PeerKeyRing()
        ring.add_key(agent_id, pub)
        sig = priv.sign(b"original")
        assert ring.verify(agent_id, b"tampered", sig) is False

    def test_reject_unknown_agent(self):
        priv, _, _, _ = _keygen()
        ring = PeerKeyRing()
        sig = priv.sign(b"data")
        assert ring.verify("unknown", b"data", sig) is False

    def test_reject_wrong_signer(self):
        priv_a, pub_a, _, id_a = _keygen()
        priv_b, pub_b, _, id_b = _keygen()
        ring = PeerKeyRing()
        ring.add_key(id_a, pub_a)
        # Sign with B's key but verify under A's identity
        sig = priv_b.sign(b"data")
        assert ring.verify(id_a, b"data", sig) is False


class TestPeerKeyRingBundle:
    def test_load_from_sm_signed_bundle(self):
        sm_priv, sm_pub, _, _ = _keygen()
        _, _, pem_a, id_a = _keygen()
        _, _, pem_b, id_b = _keygen()

        peer_keys = {id_a: pem_a, id_b: pem_b}
        signature = _sign_bundle(peer_keys, sm_priv)

        ring = PeerKeyRing()
        loaded = ring.load_from_bundle(peer_keys, signature, sm_pub)
        assert loaded == 2
        assert id_a in ring
        assert id_b in ring

    def test_excludes_self(self):
        sm_priv, sm_pub, _, _ = _keygen()
        _, _, pem_a, id_a = _keygen()
        _, _, pem_b, id_b = _keygen()

        peer_keys = {id_a: pem_a, id_b: pem_b}
        signature = _sign_bundle(peer_keys, sm_priv)

        ring = PeerKeyRing()
        loaded = ring.load_from_bundle(
            peer_keys, signature, sm_pub, exclude_self=id_a
        )
        assert loaded == 1
        assert id_a not in ring
        assert id_b in ring

    def test_reject_invalid_sm_signature(self):
        sm_priv, sm_pub, _, _ = _keygen()
        other_priv, _, _, _ = _keygen()
        _, _, pem_a, id_a = _keygen()

        peer_keys = {id_a: pem_a}
        bad_sig = other_priv.sign(b"wrong data")

        ring = PeerKeyRing()
        with pytest.raises(Exception):  # InvalidSignature
            ring.load_from_bundle(peer_keys, bad_sig, sm_pub)

    def test_skip_mismatched_agent_id(self):
        sm_priv, sm_pub, _, _ = _keygen()
        _, _, pem_a, _ = _keygen()

        # Use wrong agent_id
        peer_keys = {"wrong_id_12345": pem_a}
        signature = _sign_bundle(peer_keys, sm_priv)

        ring = PeerKeyRing()
        loaded = ring.load_from_bundle(peer_keys, signature, sm_pub)
        assert loaded == 0


# -- Message envelope tests -------------------------------------------------


HEALTH = {"fan": "OK", "disk": "OK", "memory": "OK", "psu": "OK", "thermal": "OK"}
SECRET = "test-secret-123"


class TestMessageEnvelope:
    def test_heartbeat_envelope_round_trip(self):
        packet = HeartbeatPacket(
            v=1, agent_id="agent-a", name="srv-a",
            seq=1, ts=1000.0, state="OBSERVING",
            health_summary=HEALTH,
        )
        payload = build_packet(packet, SECRET)
        envelope = build_envelope(MSG_HEARTBEAT, payload)
        assert envelope[0] == MSG_HEARTBEAT
        msg_type, recovered = parse_envelope(envelope)
        assert msg_type == MSG_HEARTBEAT
        parsed = parse_packet(recovered, SECRET)
        assert parsed.agent_id == "agent-a"

    def test_claim_envelope_type(self):
        payload = b'{"claim": "test"}'
        envelope = build_envelope(MSG_CLAIM, payload)
        msg_type, recovered = parse_envelope(envelope)
        assert msg_type == MSG_CLAIM
        assert recovered == payload

    def test_all_message_types(self):
        for mt in (MSG_HEARTBEAT, MSG_CLAIM, MSG_CLAIM_ACK, MSG_SUSPICION):
            payload = b"test"
            envelope = build_envelope(mt, payload)
            msg_type, recovered = parse_envelope(envelope)
            assert msg_type == mt
            assert recovered == payload

    def test_reject_unknown_type(self):
        with pytest.raises(HeartbeatError, match="Unknown message type"):
            build_envelope(0xFF, b"test")

    def test_parse_reject_unknown_type(self):
        data = bytes([0xFF]) + b"test"
        with pytest.raises(HeartbeatError, match="Unknown message type"):
            parse_envelope(data)

    def test_reject_too_short(self):
        with pytest.raises(HeartbeatError, match="too short"):
            parse_envelope(b"\x01")

    def test_reject_oversized_envelope(self):
        with pytest.raises(HeartbeatError, match="exceeds"):
            build_envelope(MSG_HEARTBEAT, b"x" * 576)
