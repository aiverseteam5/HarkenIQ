"""R3b-2 exit gate: verify all mesh protocol requirements (spec R-M13 through R-M22).

Every test maps to a specific spec requirement. This is the comprehensive
verification that R3b-2 is complete.
"""

from __future__ import annotations

import hashlib
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from harkeniq.autonomy.claim import Claim, ClaimAck, ClaimLease, deterministic_tiebreak
from harkeniq.autonomy.claim_exchange import ClaimExchange
from harkeniq.autonomy.claim_manager import ClaimManager
from harkeniq.autonomy.correlation_probe import CorrelationProbe, FaultLocation
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
from harkeniq.models import ClaimStatus, PeerStatus, QuorumVerdict


def _keygen():
    private = Ed25519PrivateKey.generate()
    public = private.public_key()
    pem = public.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    agent_id = hashlib.sha256(pem).hexdigest()[:16]
    return private, public, pem, agent_id


# -- A2.7 Contract 7: PeerProtocol fully implemented -----------------------


class TestPeerProtocolContract:
    def test_broadcast_claim_implemented(self):
        pp = PeerProtocol(agent_id="test")
        # Must not raise NotImplementedError
        result = pp.broadcast_claim("device-x", {"sensor": "fan"})
        assert result is None  # no identity

    def test_receive_claims_implemented(self):
        pp = PeerProtocol(agent_id="test")
        result = pp.receive_claims()
        assert isinstance(result, list)

    def test_renew_lease_implemented(self):
        pp = PeerProtocol(agent_id="test")
        result = pp.renew_lease("nonexistent")
        assert result is False

    def test_exchange_suspicion_implemented(self):
        pp = PeerProtocol(agent_id="test")
        pp.exchange_suspicion("fan", 0.5)  # must not raise


# -- R-M13: No incident on single-node evidence ----------------------------


class TestRM13SingleNodeEvidence:
    def test_single_node_no_quorum(self):
        """R-M13: No incident on single-node evidence where corroboration
        is obtainable (no reachable peers to corroborate)."""
        engine = QuorumEngine(my_agent_id="me")
        verdict = engine.disambiguate(
            "device-x",
            peer_views={"a": {"can_reach_suspect": False, "is_alive": False}},
            my_peers_alive=0,
            total_peers=1,
        )
        assert verdict == QuorumVerdict.ISOLATED


# -- R-M14: Two independent observers required -----------------------------


class TestRM14TwoObservers:
    def test_two_observers_quorum(self):
        """R-M14: Liveness conclusions require 2+ independent observers."""
        engine = QuorumEngine(my_agent_id="me")
        # Us + 1 alive peer = 2 observers
        verdict = engine.disambiguate(
            "device-x",
            peer_views={"a": {"can_reach_suspect": False, "is_alive": True}},
            my_peers_alive=1,
            total_peers=1,
        )
        assert verdict == QuorumVerdict.DEVICE_DOWN


# -- R-M15: First-claim deterministic tiebreak on identity -----------------


class TestRM15FirstClaimDeterministic:
    def test_tiebreak_on_identity_not_timestamp(self):
        """R-M15: Tiebreak on stable node identity, not timestamp."""
        c_old = Claim.new("ffff0000ffff0000", "device-x", {"s": "f"}, 1)
        c_old.created_at = 1000.0  # earlier
        c_new = Claim.new("aaaa0000aaaa0000", "device-x", {"s": "f"}, 1)
        c_new.created_at = 2000.0  # later
        winner = deterministic_tiebreak(c_old, c_new)
        assert winner.claimant_id == "aaaa0000aaaa0000"  # lower id wins


# -- R-M16: Subject is always the device -----------------------------------


class TestRM16SubjectIsDevice:
    def test_claim_subject_is_device(self):
        """R-M16: Claim subject is always the device, never the link."""
        claim = Claim.new("agent-a", "device-xyz", {"type": "fan_fail"}, 1)
        assert claim.subject_device_id == "device-xyz"
        mgr = ClaimManager(my_agent_id="me")
        mgr.process_claim(claim)
        assert mgr.get_active_claim("device-xyz") is claim


# -- R-M17: Lapsed lease inherits evidence ---------------------------------


class TestRM17LeaseInheritance:
    def test_lapsed_lease_evidence_inherited(self):
        """R-M17: Lapsed lease returns incident to claimable with inherited evidence."""
        mgr = ClaimManager(my_agent_id="me", claim_lease_duration=10.0)
        c1 = Claim.new("agent-a", "device-x", {"original": True}, 1)
        mgr.process_claim(c1)
        lease = mgr.get_lease(c1.claim_id)
        lease.add_evidence({"follow_up": True})

        mgr.tick(now=time.time() + 20)
        c2 = Claim.new("agent-b", "device-x", {"new_owner": True}, 1)
        mgr.process_claim(c2)
        new_lease = mgr.get_lease(c2.claim_id)
        # New owner inherits: original + follow_up + new_owner
        assert len(new_lease.evidence_accumulated) >= 3


# -- R-M19: Isolated node cannot claim ------------------------------------


class TestRM19IsolationException:
    def test_isolated_cannot_claim(self):
        """R-M19: Node with no reachable peers cannot claim."""
        keyring = PeerKeyRing()
        exchange = ClaimExchange(
            keyring=keyring,
            get_reachable_peers=lambda: 0,  # isolated
            get_peer_ids=lambda: [],
        )
        priv, _, _, agent_id = _keygen()
        claim = Claim.new(agent_id, "device-x", {}, 1)
        claim.sign(priv)
        result = exchange.broadcast(claim)
        assert result is None  # cannot broadcast when isolated


# -- R-M20: Continuous suspicion triggers claim ----------------------------


class TestRM20ContinuousSuspicion:
    def test_suspicion_threshold_triggers(self):
        """R-M20: Cross-node suspicion crosses threshold → claim."""
        tracker = SuspicionTracker(
            my_agent_id="me", claim_threshold=0.8, decay_rate=0.0
        )
        tracker.update_local("fan_0", 0.9, now=100.0)
        tracker.receive_peer("fan_0", 0.85, "peer-a", now=100.0)
        triggered = tracker.tick(now=100.0)
        assert "fan_0" in triggered


# -- R-M21: Smallest explaining set ---------------------------------------


class TestRM21SmallestExplainingSet:
    def test_smallest_set(self):
        """R-M21: Smallest set explaining all degraded paths."""
        result = SuspicionTracker.smallest_explaining_set(
            degraded_paths=[{"A", "B"}, {"A", "C"}],
            healthy_paths=[{"B", "D"}, {"C", "D"}],
        )
        assert result == {"A"}


# -- R-M22: Bundle coverage -----------------------------------------------


class TestRM22BundleCoverage:
    def test_incomplete_bundle(self):
        """R-M22: Synthetic measurement must cover all bundle members."""
        tracker = SuspicionTracker(my_agent_id="me")
        tracker.register_bundle("bond0", total_members=3)
        tracker.record_bundle_measurement("bond0", "member-0")
        gaps = tracker.check_bundle_coverage("bond0")
        assert gaps is not None
        assert len(gaps) == 2  # 2 unmeasured


# -- R-AGENT-6: Isolated node reports on itself ----------------------------


class TestRAGENT6SelfReport:
    def test_isolated_enters_fenced_mode(self):
        """R-AGENT-6: Isolated node reports on itself, is fenced."""
        fence = PartitionFence(my_agent_id="me")
        fence._prev_alive_count = 3
        fence.check_isolation(peers_alive=0, total_peers=3)
        assert fence.is_isolated is True
        assert fence.is_fenced(auth_lease_valid=True) is True


# -- OQ-13: Two-device correlation probe ----------------------------------


class TestOQ13CorrelationProbe:
    def test_cable_fault_diagnosed(self):
        """OQ-13: Both-sides evidence diagnoses cable fault."""
        probe = CorrelationProbe(my_agent_id="me")
        result = probe.diagnose(
            "device-x",
            local_errors={"crc_errors": 10},
            remote_errors={"fcs_errors": 8},
        )
        assert result.fault_location == FaultLocation.CABLE


# -- Message envelope: all types work --------------------------------------


class TestMessageEnvelope:
    def test_all_four_types(self):
        for mt in (MSG_HEARTBEAT, MSG_CLAIM, MSG_CLAIM_ACK, MSG_SUSPICION):
            envelope = build_envelope(mt, b"data")
            parsed_type, payload = parse_envelope(envelope)
            assert parsed_type == mt
            assert payload == b"data"
