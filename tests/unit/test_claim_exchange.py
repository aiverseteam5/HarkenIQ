"""Tests for ClaimExchange reliability layer (R3b-2 Phase 3)."""

from __future__ import annotations

import hashlib

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from harkeniq.autonomy.claim import Claim, ClaimAck
from harkeniq.autonomy.claim_exchange import ClaimExchange
from harkeniq.autonomy.peer_keyring import PeerKeyRing


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


def _make_exchange(
    keyring=None,
    reachable=2,
    peer_ids=None,
    retry_interval=0.1,
    max_retries=3,
):
    if keyring is None:
        keyring = PeerKeyRing()
    if peer_ids is None:
        peer_ids = ["peer-a", "peer-b"]
    return ClaimExchange(
        keyring=keyring,
        get_reachable_peers=lambda: reachable,
        get_peer_ids=lambda: peer_ids,
        retry_interval=retry_interval,
        max_retries=max_retries,
    )


def _signed_claim(claimant_priv, claimant_id, subject="device-x", seq=1):
    claim = Claim.new(
        claimant_id=claimant_id,
        subject_device_id=subject,
        evidence={"sensor": "fan_0"},
        seq=seq,
    )
    claim.sign(claimant_priv)
    return claim


# -- Broadcast tests --------------------------------------------------------


class TestClaimBroadcast:
    def test_broadcast_queues_claim(self):
        priv, _, _, agent_id = _keygen()
        exchange = _make_exchange()
        claim = _signed_claim(priv, agent_id)
        result = exchange.broadcast(claim)
        assert result == claim.claim_id
        assert exchange.outbound_count == 1

    def test_broadcast_returns_none_when_isolated(self):
        """R-M19: Isolated node cannot claim."""
        priv, _, _, agent_id = _keygen()
        exchange = _make_exchange(reachable=0)
        claim = _signed_claim(priv, agent_id)
        result = exchange.broadcast(claim)
        assert result is None
        assert exchange.outbound_count == 0

    def test_broadcast_returns_none_no_peer_ids(self):
        priv, _, _, agent_id = _keygen()
        exchange = _make_exchange(reachable=2, peer_ids=[])
        claim = _signed_claim(priv, agent_id)
        result = exchange.broadcast(claim)
        assert result is None

    def test_claims_to_send_returns_immediately(self):
        priv, _, _, agent_id = _keygen()
        exchange = _make_exchange()
        claim = _signed_claim(priv, agent_id)
        exchange.broadcast(claim)
        to_send = exchange.get_claims_to_send(now=0.0)
        assert len(to_send) == 1
        assert to_send[0].claim_id == claim.claim_id

    def test_retransmit_on_missing_ack(self):
        priv, _, _, agent_id = _keygen()
        exchange = _make_exchange(retry_interval=1.0)
        claim = _signed_claim(priv, agent_id)
        exchange.broadcast(claim)

        # First send
        to_send = exchange.get_claims_to_send(now=0.0)
        assert len(to_send) == 1

        # Too early for retry
        to_send = exchange.get_claims_to_send(now=0.5)
        assert len(to_send) == 0

        # After retry interval
        to_send = exchange.get_claims_to_send(now=1.1)
        assert len(to_send) == 1

    def test_stops_after_all_acked(self):
        priv_me, _, _, my_id = _keygen()
        priv_a, pub_a, _, id_a = _keygen()

        keyring = PeerKeyRing()
        keyring.add_key(id_a, pub_a)
        exchange = _make_exchange(keyring=keyring, peer_ids=[id_a])

        claim = _signed_claim(priv_me, my_id)
        exchange.broadcast(claim)

        # Send first
        exchange.get_claims_to_send(now=0.0)

        # Receive ack
        ack = ClaimAck(claim_id=claim.claim_id, acker_id=id_a, accepted=True)
        ack.sign(priv_a)
        exchange.receive_ack(ack)
        assert exchange.is_fully_acked(claim.claim_id)

        # No more retransmits needed — claim removed on next tick
        to_send = exchange.get_claims_to_send(now=10.0)
        assert len(to_send) == 0

    def test_exhausted_after_max_retries(self):
        priv, _, _, agent_id = _keygen()
        exchange = _make_exchange(max_retries=2, retry_interval=0.1)
        claim = _signed_claim(priv, agent_id)
        exchange.broadcast(claim)

        # Burn through retries
        exchange.get_claims_to_send(now=0.0)   # retry 2 -> 1
        exchange.get_claims_to_send(now=0.2)   # retry 1 -> 0
        exchange.get_claims_to_send(now=0.4)   # exhausted, removed
        assert exchange.outbound_count == 0


# -- Receive tests ----------------------------------------------------------


class TestClaimReceive:
    def test_accept_valid_signed_claim(self):
        priv_peer, pub_peer, _, peer_id = _keygen()
        keyring = PeerKeyRing()
        keyring.add_key(peer_id, pub_peer)
        exchange = _make_exchange(keyring=keyring)

        claim = _signed_claim(priv_peer, peer_id)
        result = exchange.receive_claim(claim)
        assert result is True
        assert exchange.inbound_count == 1

    def test_reject_invalid_signature(self):
        priv_peer, pub_peer, _, peer_id = _keygen()
        priv_other, _, _, _ = _keygen()
        keyring = PeerKeyRing()
        keyring.add_key(peer_id, pub_peer)
        exchange = _make_exchange(keyring=keyring)

        claim = _signed_claim(priv_other, peer_id)  # wrong signer
        result = exchange.receive_claim(claim)
        assert result is False
        assert exchange.inbound_count == 0

    def test_reject_unknown_peer(self):
        priv_peer, _, _, peer_id = _keygen()
        exchange = _make_exchange()  # empty keyring
        claim = _signed_claim(priv_peer, peer_id)
        result = exchange.receive_claim(claim)
        assert result is False

    def test_deduplicate(self):
        priv_peer, pub_peer, _, peer_id = _keygen()
        keyring = PeerKeyRing()
        keyring.add_key(peer_id, pub_peer)
        exchange = _make_exchange(keyring=keyring)

        claim = _signed_claim(priv_peer, peer_id)
        assert exchange.receive_claim(claim) is True
        assert exchange.receive_claim(claim) is False  # duplicate
        assert exchange.inbound_count == 1

    def test_get_pending_inbound_clears_buffer(self):
        priv_peer, pub_peer, _, peer_id = _keygen()
        keyring = PeerKeyRing()
        keyring.add_key(peer_id, pub_peer)
        exchange = _make_exchange(keyring=keyring)

        claim = _signed_claim(priv_peer, peer_id)
        exchange.receive_claim(claim)

        pending = exchange.get_pending_inbound()
        assert len(pending) == 1
        assert pending[0].claim_id == claim.claim_id

        # Buffer cleared
        assert exchange.get_pending_inbound() == []


# -- Ack tests -------------------------------------------------------------


class TestClaimAckExchange:
    def test_ack_for_unknown_claim_ignored(self):
        priv, pub, _, peer_id = _keygen()
        keyring = PeerKeyRing()
        keyring.add_key(peer_id, pub)
        exchange = _make_exchange(keyring=keyring)

        ack = ClaimAck(claim_id="nonexistent", acker_id=peer_id, accepted=True)
        ack.sign(priv)
        assert exchange.receive_ack(ack) is False

    def test_ack_removes_pending(self):
        priv_me, _, _, my_id = _keygen()
        priv_a, pub_a, _, id_a = _keygen()
        priv_b, pub_b, _, id_b = _keygen()

        keyring = PeerKeyRing()
        keyring.add_key(id_a, pub_a)
        keyring.add_key(id_b, pub_b)
        exchange = _make_exchange(keyring=keyring, peer_ids=[id_a, id_b])

        claim = _signed_claim(priv_me, my_id)
        exchange.broadcast(claim)

        ack_a = ClaimAck(claim_id=claim.claim_id, acker_id=id_a, accepted=True)
        ack_a.sign(priv_a)
        assert exchange.receive_ack(ack_a) is True
        assert exchange.is_fully_acked(claim.claim_id) is False

        ack_b = ClaimAck(claim_id=claim.claim_id, acker_id=id_b, accepted=True)
        ack_b.sign(priv_b)
        assert exchange.receive_ack(ack_b) is True
        assert exchange.is_fully_acked(claim.claim_id) is True


# -- Cancel tests -----------------------------------------------------------


class TestClaimCancel:
    def test_cancel_outbound(self):
        priv, _, _, agent_id = _keygen()
        exchange = _make_exchange()
        claim = _signed_claim(priv, agent_id)
        exchange.broadcast(claim)
        assert exchange.outbound_count == 1
        exchange.cancel_outbound(claim.claim_id)
        assert exchange.outbound_count == 0
