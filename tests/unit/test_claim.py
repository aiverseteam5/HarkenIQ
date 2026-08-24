"""Tests for Claim data model, wire format, and tiebreak (R3b-2 Phase 2)."""

from __future__ import annotations

import hashlib
import json
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from harkeniq.autonomy.claim import (
    Claim,
    ClaimAck,
    ClaimLease,
    DEFAULT_CLAIM_LEASE_DURATION,
    deterministic_tiebreak,
)
from harkeniq.models import ClaimStatus


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


# -- Claim serialization tests ----------------------------------------------


class TestClaimSerialization:
    def test_round_trip(self):
        priv, pub, _, agent_id = _keygen()
        claim = _make_claim(claimant_id=agent_id)
        claim.sign(priv)

        data = claim.serialize()
        recovered = Claim.deserialize(data)
        assert recovered.claim_id == claim.claim_id
        assert recovered.claimant_id == agent_id
        assert recovered.subject_device_id == "device-x"
        assert recovered.evidence == {"sensor": "fan_0", "reading": "0 RPM"}
        assert recovered.seq == 1

    def test_canonical_json_is_deterministic(self):
        c1 = _make_claim()
        c2 = Claim(
            claim_id=c1.claim_id,
            claimant_id=c1.claimant_id,
            subject_device_id=c1.subject_device_id,
            evidence=c1.evidence,
            seq=c1.seq,
            created_at=c1.created_at,
        )
        assert c1.payload_bytes() == c2.payload_bytes()

    def test_deserialize_too_short(self):
        with pytest.raises(ValueError, match="too short"):
            Claim.deserialize(b"short")

    def test_deserialize_bad_json(self):
        with pytest.raises(ValueError, match="Malformed"):
            Claim.deserialize(b"not-json" + b"\x00" * 64)


# -- Claim signing tests ---------------------------------------------------


class TestClaimSigning:
    def test_sign_and_verify(self):
        priv, pub, _, agent_id = _keygen()
        claim = _make_claim(claimant_id=agent_id)
        claim.sign(priv)
        assert len(claim.signature) == 64
        assert claim.verify(pub) is True

    def test_reject_tampered_claim(self):
        priv, pub, _, agent_id = _keygen()
        claim = _make_claim(claimant_id=agent_id)
        claim.sign(priv)
        claim.evidence = {"tampered": True}  # modify after signing
        assert claim.verify(pub) is False

    def test_reject_wrong_key(self):
        priv_a, _, _, id_a = _keygen()
        _, pub_b, _, _ = _keygen()
        claim = _make_claim(claimant_id=id_a)
        claim.sign(priv_a)
        assert claim.verify(pub_b) is False

    def test_unsigned_claim_fails_verify(self):
        _, pub, _, agent_id = _keygen()
        claim = _make_claim(claimant_id=agent_id)
        # signature is empty bytes
        assert claim.verify(pub) is False


# -- ClaimAck tests ---------------------------------------------------------


class TestClaimAck:
    def test_ack_round_trip(self):
        priv, pub, _, agent_id = _keygen()
        ack = ClaimAck(
            claim_id="test-claim-id",
            acker_id=agent_id,
            accepted=True,
        )
        ack.sign(priv)
        data = ack.serialize()
        recovered = ClaimAck.deserialize(data)
        assert recovered.claim_id == "test-claim-id"
        assert recovered.acker_id == agent_id
        assert recovered.accepted is True
        assert recovered.verify(pub) is True

    def test_reject_ack(self):
        priv, pub, _, agent_id = _keygen()
        ack = ClaimAck(
            claim_id="test-claim-id",
            acker_id=agent_id,
            accepted=False,
        )
        ack.sign(priv)
        assert ack.verify(pub) is True
        assert ack.accepted is False


# -- Deterministic tiebreak tests -------------------------------------------


class TestDeterministicTiebreak:
    def test_lower_agent_id_wins(self):
        c_low = _make_claim(claimant_id="aaaa0000aaaa0000")
        c_high = _make_claim(claimant_id="ffff0000ffff0000")
        winner = deterministic_tiebreak(c_low, c_high)
        assert winner is c_low

    def test_symmetry(self):
        c_low = _make_claim(claimant_id="aaaa0000aaaa0000")
        c_high = _make_claim(claimant_id="ffff0000ffff0000")
        assert deterministic_tiebreak(c_low, c_high) is c_low
        assert deterministic_tiebreak(c_high, c_low) is c_low

    def test_same_agent_id_first_wins(self):
        c1 = _make_claim(claimant_id="abcd1234abcd1234")
        c2 = _make_claim(claimant_id="abcd1234abcd1234")
        winner = deterministic_tiebreak(c1, c2)
        assert winner is c1  # first arg wins on tie


# -- Claim lease tests -----------------------------------------------------


class TestClaimLease:
    def test_from_claim(self):
        claim = _make_claim()
        lease = ClaimLease.from_claim(claim)
        assert lease.claim_id == claim.claim_id
        assert lease.owner_id == claim.claimant_id
        assert lease.subject_device_id == claim.subject_device_id
        assert lease.status == ClaimStatus.ACTIVE
        assert len(lease.evidence_accumulated) == 1

    def test_valid_before_expiry(self):
        claim = _make_claim()
        lease = ClaimLease.from_claim(claim, duration=120.0)
        assert lease.is_valid() is True
        assert lease.is_lapsed() is False

    def test_lapsed_after_expiry(self):
        claim = _make_claim()
        lease = ClaimLease.from_claim(claim, duration=120.0)
        # Simulate expiry
        future = time.time() + 200
        assert lease.is_lapsed(now=future) is True
        assert lease.is_valid(now=future) is False

    def test_renew_resets_expiry(self):
        claim = _make_claim()
        lease = ClaimLease.from_claim(claim, duration=10.0)
        old_expiry = lease.lease_expiry
        lease.renew(duration=120.0)
        assert lease.lease_expiry > old_expiry
        assert lease.status == ClaimStatus.ACTIVE

    def test_lapse_inherits_evidence(self):
        claim = _make_claim()
        lease = ClaimLease.from_claim(claim)
        lease.add_evidence({"extra": "data"})
        evidence = lease.lapse()
        assert lease.status == ClaimStatus.LAPSED
        assert len(evidence) == 2  # original + extra
        assert evidence[1] == {"extra": "data"}

    def test_subject_is_device(self):
        """R-M16: claim subject is always the device, never the link."""
        claim = _make_claim(subject="device-xyz")
        assert claim.subject_device_id == "device-xyz"
        lease = ClaimLease.from_claim(claim)
        assert lease.subject_device_id == "device-xyz"
