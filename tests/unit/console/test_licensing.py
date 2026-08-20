"""Ed25519 licensing: sign, verify, payload builder."""

import time

import pytest

from harkeniq_console.licensing import (
    LicensePayload,
    _PLAN_FEATURES,
    build_license_payload,
    generate_keypair,
    sign_license,
    verify_license,
)


@pytest.fixture
def keypair():
    priv, pub = generate_keypair()
    return priv, pub


# ── key generation ─────────────────────────────────────────────────

class TestGenerateKeypair:
    def test_generate_keypair_pem_format(self):
        priv, pub = generate_keypair()
        assert priv.startswith(b"-----BEGIN PRIVATE KEY-----")
        assert pub.startswith(b"-----BEGIN PUBLIC KEY-----")

    def test_generate_keypair_different_each_call(self):
        priv1, pub1 = generate_keypair()
        priv2, pub2 = generate_keypair()
        assert priv1 != priv2
        assert pub1 != pub2


# ── sign + verify round-trip ───────────────────────────────────────

class TestSignVerify:
    def test_sign_verify_roundtrip(self, keypair):
        priv, pub = keypair
        payload = build_license_payload("t1", "approve", 100, 12)
        token = sign_license(priv, payload)
        result = verify_license(pub, token)
        assert result["sub"] == "t1"
        assert result["plan"] == "approve"
        assert result["node_commit"] == 100

    def test_verify_wrong_key(self, keypair):
        priv, _ = keypair
        _, other_pub = generate_keypair()
        payload = build_license_payload("t1", "approve", 100, 12)
        token = sign_license(priv, payload)
        with pytest.raises(ValueError, match="signature"):
            verify_license(other_pub, token)

    def test_verify_expired(self, keypair):
        priv, pub = keypair
        payload = build_license_payload("t1", "approve", 100, 12)
        # Force expiry in the past
        payload["exp"] = int(time.time()) - 3600
        token = sign_license(priv, payload)
        with pytest.raises(ValueError, match="expired"):
            verify_license(pub, token)

    def test_verify_bad_format_no_dot(self, keypair):
        _, pub = keypair
        with pytest.raises(ValueError, match="2 dot-separated"):
            verify_license(pub, "nodothere")

    def test_verify_bad_format_three_parts(self, keypair):
        _, pub = keypair
        with pytest.raises(ValueError, match="2 dot-separated"):
            verify_license(pub, "a.b.c")

    def test_verify_bad_signature(self, keypair):
        priv, pub = keypair
        payload = build_license_payload("t1", "approve", 100, 12)
        token = sign_license(priv, payload)
        parts = token.split(".")
        # Tamper with the signature part
        tampered = parts[0] + "." + parts[1][:-4] + "XXXX"
        with pytest.raises(ValueError):
            verify_license(pub, tampered)

    def test_verify_bad_fingerprint(self, keypair):
        """Sign a payload, decode it, alter a field, re-encode with the
        same signature — fingerprint check should catch it."""
        priv, pub = keypair
        import base64
        import json

        payload = build_license_payload("t1", "approve", 100, 12)
        token = sign_license(priv, payload)
        parts = token.split(".")
        # Decode the payload, change a field, re-sign but keep old signature
        payload_bytes = base64.urlsafe_b64decode(parts[0] + "==")
        payload_dict = json.loads(payload_bytes)
        # The fingerprint won't match if we re-sign with the right key
        # but a different payload.  Easiest: just swap sub
        payload_dict["sub"] = "HACKED"
        new_payload_bytes = json.dumps(payload_dict, sort_keys=True, separators=(",", ":")).encode()
        import hashlib
        from cryptography.hazmat.primitives import serialization
        # Sign the tampered payload but use the original fingerprint (mismatch)
        private_key = serialization.load_pem_private_key(priv, password=None)
        new_sig = private_key.sign(new_payload_bytes)
        new_b64 = base64.urlsafe_b64encode(new_payload_bytes).rstrip(b"=").decode()
        new_sig_b64 = base64.urlsafe_b64encode(new_sig).rstrip(b"=").decode()
        tampered_token = new_b64 + "." + new_sig_b64
        # This should pass signature check but fail fingerprint check
        with pytest.raises(ValueError, match="fingerprint"):
            verify_license(pub, tampered_token)


# ── sign output format ─────────────────────────────────────────────

class TestSignFormat:
    def test_sign_output_format(self, keypair):
        priv, _ = keypair
        payload = build_license_payload("t1", "observe", 10, 1)
        token = sign_license(priv, payload)
        parts = token.split(".")
        assert len(parts) == 2
        assert len(parts[0]) > 0
        assert len(parts[1]) > 0

    def test_sign_deterministic_fingerprint(self, keypair):
        priv, pub = keypair
        payload = build_license_payload("t1", "approve", 100, 12)
        token = sign_license(priv, payload)
        result = verify_license(pub, token)
        # Same payload (sans fingerprint) → same fingerprint
        import hashlib
        import json
        clean = {k: v for k, v in result.items() if k != "fingerprint"}
        canonical = json.dumps(clean, sort_keys=True, separators=(",", ":")).encode()
        expected_fp = hashlib.sha256(canonical).hexdigest()
        assert result["fingerprint"] == expected_fp


# ── verify returns all fields ──────────────────────────────────────

class TestVerifyOutput:
    def test_verify_returns_all_fields(self, keypair):
        priv, pub = keypair
        payload = build_license_payload("tenant-abc", "enterprise", 500, 24)
        token = sign_license(priv, payload)
        result = verify_license(pub, token)
        for key in ("iss", "sub", "plan", "node_commit", "iat", "exp", "features", "fingerprint"):
            assert key in result
        assert result["iss"] == "harkeniq-console"
        assert result["sub"] == "tenant-abc"
        assert result["plan"] == "enterprise"
        assert result["node_commit"] == 500


# ── payload builder ────────────────────────────────────────────────

class TestBuildLicensePayload:
    def test_build_license_payload_observe(self):
        p = build_license_payload("t1", "observe", 10, 12)
        assert p["features"] == ["agent"]
        assert p["plan"] == "observe"

    def test_build_license_payload_approve(self):
        p = build_license_payload("t1", "approve", 100, 12)
        assert set(p["features"]) == {"agent", "site_manager", "central_command", "approvals"}

    def test_build_license_payload_enterprise(self):
        p = build_license_payload("t1", "enterprise", 1000, 12)
        expected = {"agent", "site_manager", "central_command", "approvals", "sovereign", "compliance"}
        assert set(p["features"]) == expected

    def test_build_license_payload_dates(self):
        before = int(time.time())
        p = build_license_payload("t1", "approve", 100, 6)
        after = int(time.time())
        assert before <= p["iat"] <= after
        expected_exp = p["iat"] + 6 * 30 * 24 * 60 * 60
        assert p["exp"] == expected_exp

    def test_build_license_payload_custom_features(self):
        p = build_license_payload("t1", "approve", 100, 12, features=["custom-a", "custom-b"])
        assert p["features"] == ["custom-a", "custom-b"]

    def test_build_license_payload_unknown_plan_empty_features(self):
        p = build_license_payload("t1", "unknown-plan", 10, 1)
        assert p["features"] == []


# ── dataclass ──────────────────────────────────────────────────────

class TestLicensePayloadDataclass:
    def test_license_payload_fields_exist(self):
        lp = LicensePayload()
        assert hasattr(lp, "iss")
        assert hasattr(lp, "sub")
        assert hasattr(lp, "plan")
        assert hasattr(lp, "node_commit")
        assert hasattr(lp, "iat")
        assert hasattr(lp, "exp")
        assert hasattr(lp, "features")
        assert hasattr(lp, "fingerprint")
        assert lp.iss == "harkeniq-console"
