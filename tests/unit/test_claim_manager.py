"""Tests for ClaimManager ownership protocol (R3b-2 Phase 4)."""

from __future__ import annotations

import hashlib
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from harkeniq.autonomy.claim import Claim, ClaimLease, deterministic_tiebreak
from harkeniq.autonomy.claim_manager import ClaimManager
from harkeniq.autonomy.peer_protocol import PeerProtocol
from harkeniq.autonomy.peer_keyring import PeerKeyRing
from harkeniq.heartbeat.tracker import PeerTracker
from harkeniq.models import ClaimStatus, PeerStatus


# -- helpers ----------------------------------------------------------------

def _keygen():
    private = Ed25519PrivateKey.generate()
    public = private.public_key()
    pem = public.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    agent_id = hashlib.sha256(pem).hexdigest()[:16]
    return private, public, pem, agent_id


def _make_claim(claimant_id="agent-aaa", subject="device-x", seq=1):
    return Claim.new(
        claimant_id=claimant_id,
        subject_device_id=subject,
        evidence={"sensor": "fan_0", "reading": "0 RPM"},
        seq=seq,
    )


# -- First-claim-wins tests ------------------------------------------------


class TestFirstClaimWins:
    def test_first_claim_accepted(self):
        mgr = ClaimManager(my_agent_id="me")
        claim = _make_claim(claimant_id="agent-a")
        result = mgr.process_claim(claim)
        assert result == "accepted"
        assert mgr.get_active_claim("device-x") is claim

    def test_second_claim_same_subject_rejected(self):
        mgr = ClaimManager(my_agent_id="me")
        c1 = _make_claim(claimant_id="aaaa0000aaaa0000")
        c2 = _make_claim(claimant_id="ffff0000ffff0000")
        mgr.process_claim(c1)
        result = mgr.process_claim(c2)
        assert result == "rejected"
        assert mgr.get_active_claim("device-x") is c1


# -- Deterministic tiebreak tests ------------------------------------------


class TestClaimManagerTiebreak:
    def test_lower_agent_id_wins(self):
        mgr = ClaimManager(my_agent_id="me")
        c_high = _make_claim(claimant_id="ffff0000ffff0000")
        c_low = _make_claim(claimant_id="aaaa0000aaaa0000")
        mgr.process_claim(c_high)
        result = mgr.process_claim(c_low)
        assert result == "superseded"
        assert mgr.get_active_claim("device-x") is c_low

    def test_higher_agent_id_loses(self):
        mgr = ClaimManager(my_agent_id="me")
        c_low = _make_claim(claimant_id="aaaa0000aaaa0000")
        c_high = _make_claim(claimant_id="ffff0000ffff0000")
        mgr.process_claim(c_low)
        result = mgr.process_claim(c_high)
        assert result == "rejected"

    def test_tiebreak_not_based_on_timestamp(self):
        """R-M15: tiebreak on stable identity, not timestamp."""
        mgr = ClaimManager(my_agent_id="me")
        # c_old has lower timestamp but higher agent_id
        c_old = _make_claim(claimant_id="ffff0000ffff0000")
        c_old.created_at = 1000.0
        c_new = _make_claim(claimant_id="aaaa0000aaaa0000")
        c_new.created_at = 2000.0
        mgr.process_claim(c_old)
        result = mgr.process_claim(c_new)
        assert result == "superseded"
        assert mgr.get_active_claim("device-x") is c_new


# -- Claim lease renewal tests ---------------------------------------------


class TestClaimLeaseRenewal:
    def test_renew_resets_expiry(self):
        mgr = ClaimManager(my_agent_id="me", claim_lease_duration=10.0)
        claim = _make_claim(claimant_id="me")
        mgr.process_claim(claim)
        lease = mgr.get_lease(claim.claim_id)
        old_expiry = lease.lease_expiry
        assert mgr.renew_lease(claim.claim_id) is True
        assert lease.lease_expiry > old_expiry

    def test_renew_nonexistent_fails(self):
        mgr = ClaimManager(my_agent_id="me")
        assert mgr.renew_lease("nonexistent") is False

    def test_renew_after_lapse_fails(self):
        mgr = ClaimManager(my_agent_id="me", claim_lease_duration=1.0)
        claim = _make_claim(claimant_id="me")
        mgr.process_claim(claim)
        # Force lapse
        mgr.tick(now=time.time() + 100)
        assert mgr.renew_lease(claim.claim_id) is False


# -- Claim lapse tests -----------------------------------------------------


class TestClaimLapse:
    def test_lapsed_lease_makes_claimable(self):
        mgr = ClaimManager(my_agent_id="me", claim_lease_duration=10.0)
        claim = _make_claim(claimant_id="agent-a")
        mgr.process_claim(claim)
        assert mgr.is_claimable("device-x") is False

        # Force lapse
        lapsed = mgr.tick(now=time.time() + 20)
        assert len(lapsed) == 1
        assert mgr.is_claimable("device-x") is True

    def test_evidence_inherited_by_new_owner(self):
        """R-M17: lapsed lease inherits evidence."""
        mgr = ClaimManager(my_agent_id="me", claim_lease_duration=10.0)
        c1 = _make_claim(claimant_id="agent-a")
        mgr.process_claim(c1)
        # Add extra evidence
        lease = mgr.get_lease(c1.claim_id)
        lease.add_evidence({"follow_up": "data"})

        # Lapse
        mgr.tick(now=time.time() + 20)

        # New claim inherits evidence
        c2 = _make_claim(claimant_id="agent-b")
        result = mgr.process_claim(c2)
        assert result == "accepted"
        new_lease = mgr.get_lease(c2.claim_id)
        # Should have original evidence + follow_up + c2's own evidence
        assert len(new_lease.evidence_accumulated) >= 2


# -- Owner death tests -----------------------------------------------------


class TestOwnerDeath:
    def test_owner_stops_renewing_lease_lapses(self):
        mgr = ClaimManager(my_agent_id="me", claim_lease_duration=10.0)
        claim = _make_claim(claimant_id="dying-agent")
        mgr.process_claim(claim)

        # Owner doesn't renew
        lapsed = mgr.tick(now=time.time() + 20)
        assert len(lapsed) == 1
        assert lapsed[0].owner_id == "dying-agent"
        assert lapsed[0].status == ClaimStatus.LAPSED

    def test_reclaim_after_owner_death(self):
        mgr = ClaimManager(my_agent_id="me", claim_lease_duration=10.0)
        c1 = _make_claim(claimant_id="dying-agent")
        mgr.process_claim(c1)
        mgr.tick(now=time.time() + 20)

        c2 = _make_claim(claimant_id="survivor")
        result = mgr.process_claim(c2)
        assert result == "accepted"
        assert mgr.get_active_claim("device-x") is c2


# -- Subject dedup tests ---------------------------------------------------


class TestSubjectDedup:
    def test_one_claim_per_device(self):
        """R-M16: one active claim per device."""
        mgr = ClaimManager(my_agent_id="me")
        c1 = _make_claim(claimant_id="aaaa0000aaaa0000", subject="device-x")
        c2 = _make_claim(claimant_id="bbbb0000bbbb0000", subject="device-x")
        mgr.process_claim(c1)
        result = mgr.process_claim(c2)
        # c1 wins tiebreak (lower id)
        assert result == "rejected"
        assert mgr.get_active_claim("device-x").claimant_id == "aaaa0000aaaa0000"

    def test_different_devices_independent(self):
        mgr = ClaimManager(my_agent_id="me")
        c1 = _make_claim(subject="device-x")
        c2 = _make_claim(subject="device-y")
        assert mgr.process_claim(c1) == "accepted"
        assert mgr.process_claim(c2) == "accepted"
        assert mgr.get_active_claim("device-x") is c1
        assert mgr.get_active_claim("device-y") is c2


# -- PeerProtocol stubs replaced tests -------------------------------------


class TestPeerProtocolStubs:
    def test_broadcast_claim_no_longer_raises(self):
        """PeerProtocol.broadcast_claim() no longer raises NotImplementedError."""
        pp = PeerProtocol(agent_id="test-agent")
        # Returns None because no identity/exchange, but doesn't raise
        result = pp.broadcast_claim("device-x", {"sensor": "fan"})
        assert result is None  # no identity configured

    def test_receive_claims_no_longer_raises(self):
        pp = PeerProtocol(agent_id="test-agent")
        result = pp.receive_claims()
        assert result == []

    def test_renew_lease_no_longer_raises(self):
        pp = PeerProtocol(agent_id="test-agent")
        result = pp.renew_lease("nonexistent")
        assert result is False

    def test_exchange_suspicion_no_longer_raises(self):
        pp = PeerProtocol(agent_id="test-agent")
        # Should not raise NotImplementedError
        pp.exchange_suspicion("fan", 0.5)


# -- Owned claims tests ----------------------------------------------------


class TestOwnedClaims:
    def test_get_owned_claims(self):
        mgr = ClaimManager(my_agent_id="me")
        c1 = _make_claim(claimant_id="me", subject="device-a")
        c2 = _make_claim(claimant_id="other", subject="device-b")
        mgr.process_claim(c1)
        mgr.process_claim(c2)
        owned = mgr.get_owned_claims()
        assert len(owned) == 1
        assert owned[0].subject_device_id == "device-a"


# -- Resolve tests ---------------------------------------------------------


class TestResolve:
    def test_resolve_removes_active_claim(self):
        mgr = ClaimManager(my_agent_id="me")
        claim = _make_claim(claimant_id="me")
        mgr.process_claim(claim)
        assert mgr.resolve("device-x") is True
        assert mgr.get_active_claim("device-x") is None

    def test_reject_claim_after_resolve(self):
        mgr = ClaimManager(my_agent_id="me")
        c1 = _make_claim(claimant_id="me")
        mgr.process_claim(c1)
        mgr.resolve("device-x")
        c2 = _make_claim(claimant_id="other")
        result = mgr.process_claim(c2)
        assert result == "rejected"
