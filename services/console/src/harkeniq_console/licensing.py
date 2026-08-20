"""Ed25519 license signing and verification.

Handles signing and verifying license keys for air-gapped and connected
deployments.  Uses Ed25519 via the ``cryptography`` library for compact,
high-security signatures without the complexity of X.509 or JWT.

Token format (two base64url parts separated by a dot):

    base64url(canonical_json_payload) . base64url(ed25519_signature)
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives import serialization
from cryptography.exceptions import InvalidSignature


# ---------------------------------------------------------------------------
# Plan -> default feature map
# ---------------------------------------------------------------------------

_PLAN_FEATURES: dict[str, list[str]] = {
    "observe": ["agent"],
    "approve": ["agent", "site_manager", "central_command", "approvals"],
    "enterprise": [
        "agent",
        "site_manager",
        "central_command",
        "approvals",
        "sovereign",
        "compliance",
    ],
}


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass
class LicensePayload:
    """Structured license claim set."""

    iss: str = "harkeniq-console"
    sub: str = ""           # tenant_id
    plan: str = ""
    node_commit: int = 0
    iat: int = 0            # unix timestamp
    exp: int = 0            # unix timestamp
    features: list[str] = field(default_factory=list)
    fingerprint: str = ""   # sha256 of canonical payload (computed)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _b64url_encode(data: bytes) -> str:
    """Base64url-encode *data*, stripping padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    """Base64url-decode *s*, restoring padding as needed."""
    pad = 4 - len(s) % 4
    if pad != 4:
        s += "=" * pad
    return base64.urlsafe_b64decode(s)


def _canonical_json(d: dict) -> bytes:
    """Return deterministic JSON bytes (sorted keys, no extra whitespace)."""
    return json.dumps(d, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    """Return the hex SHA-256 digest of *data*."""
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Key generation
# ---------------------------------------------------------------------------


def generate_keypair() -> tuple[bytes, bytes]:
    """Generate an Ed25519 key pair.

    Returns:
        A tuple ``(private_key_pem, public_key_pem)`` where both values are
        PEM-encoded ``bytes``.
    """
    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------


def sign_license(private_key_pem: bytes, payload: dict) -> str:
    """Sign a license payload with an Ed25519 private key.

    Steps:
        1. Compute the fingerprint (SHA-256 of canonical JSON of *payload*
           **without** a ``fingerprint`` key).
        2. Add ``fingerprint`` to the payload.
        3. Canonical-JSON encode the full payload (with fingerprint).
        4. Sign with Ed25519.
        5. Return ``base64url(json_payload) + "." + base64url(signature)``.

    Args:
        private_key_pem: PEM-encoded Ed25519 private key.
        payload: License claim dict (see :class:`LicensePayload`).

    Returns:
        A compact signed license token string.
    """
    # Load the private key
    private_key = serialization.load_pem_private_key(private_key_pem, password=None)

    # 1. Compute fingerprint over the payload *without* the fingerprint field
    payload_clean = {k: v for k, v in payload.items() if k != "fingerprint"}
    fingerprint = _sha256_hex(_canonical_json(payload_clean))

    # 2. Add fingerprint
    payload_with_fp = {**payload_clean, "fingerprint": fingerprint}

    # 3. Canonical JSON encode
    payload_bytes = _canonical_json(payload_with_fp)

    # 4. Sign
    signature = private_key.sign(payload_bytes)

    # 5. Assemble token
    return _b64url_encode(payload_bytes) + "." + _b64url_encode(signature)


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def verify_license(public_key_pem: bytes, signed_license: str) -> dict:
    """Verify and decode a signed license token.

    Checks:
        1. Token format (exactly two dot-separated parts).
        2. Ed25519 signature validity.
        3. Fingerprint integrity.
        4. Expiry (``exp`` > current time).

    Args:
        public_key_pem: PEM-encoded Ed25519 public key.
        signed_license: The compact token returned by :func:`sign_license`.

    Returns:
        The verified payload as a ``dict``.

    Raises:
        ValueError: On any verification failure (bad format, invalid
            signature, expired, fingerprint mismatch).
    """
    # 1. Split
    parts = signed_license.split(".")
    if len(parts) != 2:
        raise ValueError(
            f"Invalid license format: expected 2 dot-separated parts, got {len(parts)}"
        )

    try:
        payload_bytes = _b64url_decode(parts[0])
        signature = _b64url_decode(parts[1])
    except Exception as exc:
        raise ValueError(f"Invalid base64url encoding: {exc}") from exc

    # 2. Load public key and verify signature
    try:
        public_key = serialization.load_pem_public_key(public_key_pem)
    except Exception as exc:
        raise ValueError(f"Invalid public key: {exc}") from exc

    try:
        public_key.verify(signature, payload_bytes)
    except InvalidSignature:
        raise ValueError("Invalid license signature")

    # 3. Parse JSON
    try:
        payload = json.loads(payload_bytes)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid license payload JSON: {exc}") from exc

    # 4. Verify fingerprint
    fp = payload.get("fingerprint")
    if fp is None:
        raise ValueError("License payload missing fingerprint")

    payload_without_fp = {k: v for k, v in payload.items() if k != "fingerprint"}
    expected_fp = _sha256_hex(_canonical_json(payload_without_fp))
    if fp != expected_fp:
        raise ValueError(
            "License fingerprint mismatch — payload may have been tampered with"
        )

    # 5. Check expiry
    exp = payload.get("exp")
    if exp is None:
        raise ValueError("License payload missing expiry (exp)")
    if exp <= time.time():
        raise ValueError("License has expired")

    return payload


# ---------------------------------------------------------------------------
# Payload builder
# ---------------------------------------------------------------------------


def build_license_payload(
    tenant_id: str,
    plan: str,
    node_commit: int,
    valid_months: int,
    features: Optional[list[str]] = None,
) -> dict:
    """Build a license payload dict ready for signing.

    Args:
        tenant_id: Tenant identifier (becomes ``sub``).
        plan: One of ``"observe"``, ``"approve"``, ``"enterprise"``.
        node_commit: Committed node count for billing.
        valid_months: Validity period in calendar months.
        features: Explicit feature list.  If *None*, defaults are derived
            from *plan* (see :data:`_PLAN_FEATURES`).

    Returns:
        A ``dict`` suitable for passing to :func:`sign_license`.
    """
    now = int(time.time())

    if features is None:
        features = list(_PLAN_FEATURES.get(plan, []))

    # Approximate months as 30 days each
    exp = now + valid_months * 30 * 24 * 60 * 60

    return {
        "iss": "harkeniq-console",
        "sub": tenant_id,
        "plan": plan,
        "node_commit": node_commit,
        "iat": now,
        "exp": exp,
        "features": features,
    }
