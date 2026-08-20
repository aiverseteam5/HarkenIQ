"""Ed25519 license signing and verification.

All functions are stubs for Phase 2; signatures document the intended
contract so the API layer can be wired ahead of time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class LicensePayload:
    """Structured license claim set."""

    iss: str = "harkeniq-console"
    sub: str = ""          # tenant_id
    plan: str = ""
    node_commit: int = 0
    iat: float = 0.0
    exp: float = 0.0
    fingerprint: str = ""
    features: list[str] = field(default_factory=list)


def generate_keypair() -> tuple[bytes, bytes]:
    """Generate an Ed25519 keypair. Returns (private_pem, public_pem)."""
    raise NotImplementedError("license generate_keypair — Phase 2")


def sign_license(private_key_pem: bytes, payload: dict) -> str:
    """Sign a license payload. Returns a compact signed token."""
    raise NotImplementedError("license sign_license — Phase 2")


def verify_license(public_key_pem: bytes, signed_license: str) -> dict:
    """Verify and decode a signed license token. Returns payload dict."""
    raise NotImplementedError("license verify_license — Phase 2")
