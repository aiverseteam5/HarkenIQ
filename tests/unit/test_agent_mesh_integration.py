"""Unit tests for agent mesh protocol wiring (R3b-2 Phase 7)."""

from __future__ import annotations

import hashlib

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from harkeniq.autonomy.claim import Claim, ClaimAck
from harkeniq.autonomy.claim_exchange import ClaimExchange
from harkeniq.autonomy.claim_manager import ClaimManager
from harkeniq.autonomy.partition_fence import PartitionFence
from harkeniq.autonomy.peer_keyring import PeerKeyRing
from harkeniq.autonomy.peer_protocol import PeerProtocol
from harkeniq.autonomy.quorum import QuorumEngine
from harkeniq.autonomy.suspicion import SuspicionTracker
from harkeniq.heartbeat.protocol import (
    MSG_CLAIM,
    MSG_CLAIM_ACK,
    MSG_HEARTBEAT,
    MSG_SUSPICION,
    build_envelope,
    parse_envelope,
)
from harkeniq.heartbeat.tracker import PeerTracker
from harkeniq.models import PeerStatus, QuorumVerdict


def _keygen():
    private = Ed25519PrivateKey.generate()
    public = private.public_key()
    pem = public.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    agent_id = hashlib.sha256(pem).hexdigest()[:16]
    return private, public, pem, agent_id


class TestDatagramDispatch:
    def test_heartbeat_type_parsed(self):
        payload = b'{"v":1,"agent_id":"a","name":"n","seq":1,"ts":1.0,"state":"OBSERVING","health_summary":{},"hmac":"0"}'
        envelope = build_envelope(MSG_HEARTBEAT, payload)
        msg_type, recovered = parse_envelope(envelope)
        assert msg_type == MSG_HEARTBEAT
        assert recovered == payload

    def test_claim_type_parsed(self):
        payload = b'test-claim-payload'
        envelope = build_envelope(MSG_CLAIM, payload)
        msg_type, recovered = parse_envelope(envelope)
        assert msg_type == MSG_CLAIM

    def test_all_types_round_trip(self):
        for mt in (MSG_HEARTBEAT, MSG_CLAIM, MSG_CLAIM_ACK, MSG_SUSPICION):
            payload = b'test'
            envelope = build_envelope(mt, payload)
            msg_type, recovered = parse_envelope(envelope)
            assert msg_type == mt
            assert recovered == payload


class TestPeerProtocolIntegration:
    def test_full_claim_lifecycle(self):
        """End-to-end: create claim, process, renew, lapse."""
        priv_me, pub_me, _, id_me = _keygen()
        priv_peer, pub_peer, _, id_peer = _keygen()

        from harkeniq.autonomy.identity import AgentIdentity
        identity = AgentIdentity(
            agent_id=id_me,
            _private_key=priv_me,
            _public_key=pub_me,
        )

        # Create tracker with a peer that's ALIVE
        tracker = PeerTracker({
            "peers": [{"host": "10.0.0.1", "port": 5150}],
            "heartbeat": {"interval": 10, "timeout_multiplier": 3},
        })
        # Simulate peer being alive by recording a heartbeat
        from harkeniq.models import HeartbeatPacket
        packet = HeartbeatPacket(
            v=1, agent_id=id_peer, name="peer",
            seq=1, ts=1000.0, state="OBSERVING",
            health_summary={"fan": "OK"},
        )
        tracker.record_heartbeat(packet, "10.0.0.1", now=1000.0)

        # Build PeerProtocol with keyring
        keyring = PeerKeyRing()
        keyring.add_key(id_peer, pub_peer)

        proto = PeerProtocol(
            tracker=tracker,
            agent_id=id_me,
            keyring=keyring,
            identity=identity,
            claim_config={"lease_duration": 10.0},
        )

        # Broadcast a claim
        claim_id = proto.broadcast_claim("device-x", {"sensor": "fan"})
        assert claim_id is not None

        # Renew the lease
        assert proto.renew_lease(claim_id) is True

        # Check owned claims
        owned = proto.claim_manager.get_owned_claims()
        assert len(owned) == 1

    def test_receive_peer_claim(self):
        priv_me, pub_me, _, id_me = _keygen()
        priv_peer, pub_peer, _, id_peer = _keygen()

        keyring = PeerKeyRing()
        keyring.add_key(id_peer, pub_peer)

        proto = PeerProtocol(
            agent_id=id_me,
            keyring=keyring,
            claim_config={"lease_duration": 10.0},
        )

        # Create and sign a claim from the peer
        claim = Claim.new(
            claimant_id=id_peer,
            subject_device_id="device-y",
            evidence={"sensor": "disk"},
            seq=1,
        )
        claim.sign(priv_peer)

        # Inject into exchange
        proto.claim_exchange.receive_claim(claim)

        # Process
        accepted = proto.receive_claims()
        assert len(accepted) == 1
        assert accepted[0].claimant_id == id_peer


class TestPartitionFenceIntegration:
    def test_isolation_fences_actions(self):
        fence = PartitionFence(my_agent_id="me")
        fence._prev_alive_count = 3
        fence.check_isolation(peers_alive=0, total_peers=3)
        assert fence.is_fenced(auth_lease_valid=True) is True

    def test_recovery_lifts_fence(self):
        fence = PartitionFence(my_agent_id="me")
        fence._prev_alive_count = 3
        fence.check_isolation(peers_alive=0, total_peers=3)
        fence.check_recovery(peers_alive=2)
        assert fence.is_fenced(auth_lease_valid=True) is False


class TestQuorumIntegration:
    def test_quorum_with_real_peer_state(self):
        engine = QuorumEngine(my_agent_id="me")
        verdict = engine.disambiguate(
            suspect_device_id="device-x",
            peer_views={
                "peer-a": {"can_reach_suspect": False, "is_alive": True},
                "peer-b": {"can_reach_suspect": False, "is_alive": True},
            },
            my_peers_alive=2,
            total_peers=3,
        )
        assert verdict == QuorumVerdict.DEVICE_DOWN

    def test_suspicion_triggers_claim_threshold(self):
        tracker = SuspicionTracker(
            my_agent_id="me", claim_threshold=0.7, decay_rate=0.0
        )
        tracker.update_local("fan_0", 0.8, now=100.0)
        tracker.receive_peer("fan_0", 0.9, "peer-a", now=100.0)
        triggered = tracker.tick(now=100.0)
        assert "fan_0" in triggered
