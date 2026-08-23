"""R3a S1: Agent identity, authorization lease, and tier gating tests.

Tests the Ed25519 identity lifecycle, SM-signed lease verification,
lease expiry cascade, risk-degraded behavior, and tier calculation.
"""

import hashlib
import json
import sqlite3
import time

import pytest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

from harkeniq.autonomy.identity import AgentIdentity, _canonical_json
from harkeniq.autonomy.lease import AuthorizationLease, InvalidLease, build_lease_payload
from harkeniq.autonomy.tier import TierLevel, calculate_tier
from harkeniq.models import Peer, PeerStatus
from harkeniq.state.checkpoint import CheckpointManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sm_keypair():
    """Generate a SM-side Ed25519 keypair for test signing."""
    private = Ed25519PrivateKey.generate()
    public_pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private, public_pem


def _sign_lease(sm_private, payload_bytes):
    """SM signs a lease payload."""
    return payload_bytes + sm_private.sign(payload_bytes)


def _make_lease_raw(sm_private, agent_id, **overrides):
    """Build and sign a lease for testing."""
    now = time.time()
    payload = {
        "v": 1,
        "agent_id": agent_id,
        "action_classes": ["IDENTIFY_LED", "FAN_RESET"],
        "risk_ceiling": "low",
        "budget_remaining": {"IDENTIFY_LED": -1, "FAN_RESET": 5},
        "lease_expiry": now + 300,
        "grace_expiry": now + 360,
        "suppression_domains": [],
        "stop_switch": False,
        "issued_at": now,
    }
    payload.update(overrides)
    payload_bytes = _canonical_json(payload)
    return _sign_lease(sm_private, payload_bytes)


# ===========================================================================
# Identity tests
# ===========================================================================


class TestIdentityGeneration:
    def test_generate_creates_keypair(self):
        identity = AgentIdentity.generate()
        assert identity.agent_id
        assert len(identity.agent_id) == 16
        assert identity.public_key_pem
        assert not identity.revoked

    def test_agent_id_derived_from_public_key(self):
        identity = AgentIdentity.generate()
        expected = hashlib.sha256(identity.public_key_pem).hexdigest()[:16]
        assert identity.agent_id == expected

    def test_two_identities_are_different(self):
        a = AgentIdentity.generate()
        b = AgentIdentity.generate()
        assert a.agent_id != b.agent_id
        assert a.public_key_pem != b.public_key_pem


class TestIdentitySignVerify:
    def test_sign_and_verify_with_sm_key(self):
        identity = AgentIdentity.generate()
        sm_private, sm_public_pem = _sm_keypair()
        identity.set_sm_public_key(sm_public_pem)

        message = b"test message"
        signature = sm_private.sign(message)
        assert identity.verify_sm_signature(message, signature)

    def test_reject_wrong_sm_signature(self):
        identity = AgentIdentity.generate()
        _, sm_public_pem = _sm_keypair()
        identity.set_sm_public_key(sm_public_pem)

        # Sign with a different key
        other_private = Ed25519PrivateKey.generate()
        message = b"test message"
        signature = other_private.sign(message)
        assert not identity.verify_sm_signature(message, signature)

    def test_verify_fails_without_sm_key(self):
        identity = AgentIdentity.generate()
        assert not identity.verify_sm_signature(b"msg", b"sig" * 10)

    def test_agent_signs_outcomes(self):
        identity = AgentIdentity.generate()
        message = b"outcome data"
        sig = identity.sign(message)
        # Verify with the public key directly
        public_key = serialization.load_pem_public_key(identity.public_key_pem)
        public_key.verify(sig, message)  # raises if invalid

    def test_sm_key_pinned_once(self):
        identity = AgentIdentity.generate()
        _, sm_pub1 = _sm_keypair()
        _, sm_pub2 = _sm_keypair()
        identity.set_sm_public_key(sm_pub1)
        identity.set_sm_public_key(sm_pub2)  # ignored (logs warning)
        # Still uses the first key
        assert identity.sm_public_key_pem == sm_pub1


class TestIdentityPersistence:
    def test_save_and_load_roundtrip(self, tmp_path):
        cp = CheckpointManager(tmp_path / "test.db")
        identity = AgentIdentity.generate()
        sm_private, sm_pub = _sm_keypair()
        identity.set_sm_public_key(sm_pub)
        identity.sm_certificate = b"test-cert"
        identity.save(cp.conn, str(tmp_path / "test.db"))

        loaded = AgentIdentity.load(cp.conn, str(tmp_path / "test.db"))
        assert loaded is not None
        assert loaded.agent_id == identity.agent_id
        assert loaded.public_key_pem == identity.public_key_pem
        assert loaded.sm_public_key_pem == sm_pub
        assert loaded.sm_certificate == b"test-cert"
        assert not loaded.revoked

    def test_revoked_persists(self, tmp_path):
        cp = CheckpointManager(tmp_path / "test.db")
        identity = AgentIdentity.generate()
        identity.save(cp.conn, str(tmp_path / "test.db"))
        identity.mark_revoked(cp.conn)

        loaded = AgentIdentity.load(cp.conn, str(tmp_path / "test.db"))
        assert loaded is not None
        assert loaded.revoked is True
        assert not loaded.is_valid()

    def test_load_returns_none_for_empty_db(self, tmp_path):
        cp = CheckpointManager(tmp_path / "test.db")
        assert AgentIdentity.load(cp.conn) is None


class TestIdentityValidity:
    def test_valid_when_sm_key_pinned_and_not_revoked(self):
        identity = AgentIdentity.generate()
        _, sm_pub = _sm_keypair()
        identity.set_sm_public_key(sm_pub)
        assert identity.is_valid()

    def test_invalid_without_sm_key(self):
        identity = AgentIdentity.generate()
        assert not identity.is_valid()

    def test_invalid_when_revoked(self):
        identity = AgentIdentity.generate()
        _, sm_pub = _sm_keypair()
        identity.set_sm_public_key(sm_pub)
        identity.revoked = True
        assert not identity.is_valid()


# ===========================================================================
# Lease tests
# ===========================================================================


class TestLeaseParsing:
    def test_parse_valid_lease(self):
        sm_private, sm_pub = _sm_keypair()
        identity = AgentIdentity.generate()
        identity.set_sm_public_key(sm_pub)
        raw = _make_lease_raw(sm_private, identity.agent_id)
        lease = AuthorizationLease.parse(raw, identity)
        assert lease.agent_id == identity.agent_id
        assert "IDENTIFY_LED" in lease.action_classes
        assert not lease.stop_switch

    def test_reject_invalid_signature(self):
        sm_private, sm_pub = _sm_keypair()
        identity = AgentIdentity.generate()
        identity.set_sm_public_key(sm_pub)

        other_private = Ed25519PrivateKey.generate()
        raw = _make_lease_raw(other_private, identity.agent_id)
        with pytest.raises(InvalidLease, match="signature"):
            AuthorizationLease.parse(raw, identity)

    def test_reject_agent_id_mismatch(self):
        sm_private, sm_pub = _sm_keypair()
        identity = AgentIdentity.generate()
        identity.set_sm_public_key(sm_pub)
        raw = _make_lease_raw(sm_private, "wrong-agent-id")
        with pytest.raises(InvalidLease, match="mismatch"):
            AuthorizationLease.parse(raw, identity)

    def test_reject_truncated_lease(self):
        identity = AgentIdentity.generate()
        with pytest.raises(InvalidLease, match="too short"):
            AuthorizationLease.parse(b"short", identity)


class TestLeaseExpiry:
    def test_valid_before_expiry(self):
        sm_private, sm_pub = _sm_keypair()
        identity = AgentIdentity.generate()
        identity.set_sm_public_key(sm_pub)
        raw = _make_lease_raw(sm_private, identity.agent_id,
                              lease_expiry=time.time() + 300,
                              grace_expiry=time.time() + 360)
        lease = AuthorizationLease.parse(raw, identity)
        assert lease.is_valid()
        assert not lease.is_in_grace()
        assert not lease.is_fully_expired()

    def test_grace_period_after_expiry(self):
        sm_private, sm_pub = _sm_keypair()
        identity = AgentIdentity.generate()
        identity.set_sm_public_key(sm_pub)
        now = time.time()
        raw = _make_lease_raw(sm_private, identity.agent_id,
                              lease_expiry=now - 10,
                              grace_expiry=now + 50)
        lease = AuthorizationLease.parse(raw, identity)
        assert not lease.is_valid()
        assert lease.is_in_grace()
        assert not lease.is_fully_expired()

    def test_fully_expired(self):
        sm_private, sm_pub = _sm_keypair()
        identity = AgentIdentity.generate()
        identity.set_sm_public_key(sm_pub)
        now = time.time()
        raw = _make_lease_raw(sm_private, identity.agent_id,
                              lease_expiry=now - 100,
                              grace_expiry=now - 40)
        lease = AuthorizationLease.parse(raw, identity)
        assert not lease.is_valid()
        assert not lease.is_in_grace()
        assert lease.is_fully_expired()


class TestLeaseAuthorization:
    def _make_lease(self, sm_connected=True, **overrides):
        sm_private, sm_pub = _sm_keypair()
        identity = AgentIdentity.generate()
        identity.set_sm_public_key(sm_pub)
        raw = _make_lease_raw(sm_private, identity.agent_id, **overrides)
        lease = AuthorizationLease.parse(raw, identity)
        return lease, sm_connected

    def test_execute_allowed_for_permitted_action(self):
        lease, _ = self._make_lease()
        assert lease.allows_action("IDENTIFY_LED", "none", True) == "execute"

    def test_deny_for_unlisted_action(self):
        lease, _ = self._make_lease()
        assert lease.allows_action("POWER_CYCLE", "medium", True) == "deny"

    def test_propose_when_budget_exhausted(self):
        lease, _ = self._make_lease(
            budget_remaining={"IDENTIFY_LED": -1, "FAN_RESET": 0}
        )
        assert lease.allows_action("FAN_RESET", "low", True) == "propose"

    def test_risk_degradation_medium_risk_sm_disconnected(self):
        """A2.2: medium-risk actions propose-only when SM disconnected."""
        sm_private, sm_pub = _sm_keypair()
        identity = AgentIdentity.generate()
        identity.set_sm_public_key(sm_pub)
        raw = _make_lease_raw(
            sm_private, identity.agent_id,
            action_classes=["POWER_CYCLE"],
            risk_ceiling="medium",
            budget_remaining={"POWER_CYCLE": 5},
        )
        lease = AuthorizationLease.parse(raw, identity)
        # Connected: execute allowed
        assert lease.allows_action("POWER_CYCLE", "medium", True) == "execute"
        # Disconnected: propose only
        assert lease.allows_action("POWER_CYCLE", "medium", False) == "propose"

    def test_low_risk_allowed_when_sm_disconnected(self):
        """A2.2: low-risk actions continue within budget when SM disconnected."""
        lease, _ = self._make_lease()
        assert lease.allows_action("IDENTIFY_LED", "none", False) == "execute"
        assert lease.allows_action("FAN_RESET", "low", False) == "execute"

    def test_stop_switch_denies_everything(self):
        lease, _ = self._make_lease(stop_switch=True)
        assert lease.allows_action("IDENTIFY_LED", "none", True) == "deny"
        assert lease.allows_action("FAN_RESET", "low", True) == "deny"

    def test_grace_period_proposes(self):
        now = time.time()
        lease, _ = self._make_lease(
            lease_expiry=now - 10, grace_expiry=now + 50
        )
        assert lease.allows_action("IDENTIFY_LED", "none", True) == "propose"

    def test_fully_expired_denies(self):
        now = time.time()
        lease, _ = self._make_lease(
            lease_expiry=now - 100, grace_expiry=now - 40
        )
        assert lease.allows_action("IDENTIFY_LED", "none", True) == "deny"


class TestBuildLeasePayload:
    def test_builds_canonical_json(self):
        payload = build_lease_payload(
            agent_id="test-agent",
            action_classes=["FAN_RESET", "IDENTIFY_LED"],
            risk_ceiling="low",
            budget_remaining={"FAN_RESET": 5, "IDENTIFY_LED": -1},
        )
        data = json.loads(payload)
        assert data["agent_id"] == "test-agent"
        assert data["action_classes"] == ["FAN_RESET", "IDENTIFY_LED"]  # sorted
        assert data["v"] == 1
        assert data["lease_expiry"] > time.time()
        assert data["grace_expiry"] > data["lease_expiry"]


# ===========================================================================
# Tier tests
# ===========================================================================


class TestTierCalculation:
    def test_t1_with_two_alive_peers(self):
        peers = [
            Peer(peer_id="a", host="10.0.0.1", port=5150, status=PeerStatus.ALIVE),
            Peer(peer_id="b", host="10.0.0.2", port=5150, status=PeerStatus.ALIVE),
        ]
        assert calculate_tier(peers) == TierLevel.T1

    def test_t2_with_one_alive_peer(self):
        peers = [
            Peer(peer_id="a", host="10.0.0.1", port=5150, status=PeerStatus.ALIVE),
            Peer(peer_id="b", host="10.0.0.2", port=5150, status=PeerStatus.UNRESPONSIVE),
        ]
        assert calculate_tier(peers) == TierLevel.T2

    def test_t2_with_no_peers(self):
        assert calculate_tier([]) == TierLevel.T2

    def test_t2_with_all_unresponsive(self):
        peers = [
            Peer(peer_id="a", host="10.0.0.1", port=5150, status=PeerStatus.UNRESPONSIVE),
            Peer(peer_id="b", host="10.0.0.2", port=5150, status=PeerStatus.UNRESPONSIVE),
        ]
        assert calculate_tier(peers) == TierLevel.T2

    def test_t1_with_three_alive_peers(self):
        peers = [
            Peer(peer_id="a", host="10.0.0.1", port=5150, status=PeerStatus.ALIVE),
            Peer(peer_id="b", host="10.0.0.2", port=5150, status=PeerStatus.ALIVE),
            Peer(peer_id="c", host="10.0.0.3", port=5150, status=PeerStatus.ALIVE),
        ]
        assert calculate_tier(peers) == TierLevel.T1
