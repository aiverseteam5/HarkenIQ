"""CC-side license load + verify (QA-019 second half, R-H6).

The Console issues compact Ed25519-signed license tokens
(``base64url(canonical_json).base64url(signature)`` — see
harkeniq_console.licensing). A tenant downloads the ``.lic`` file and
places it on the CC host; CC loads and verifies it at startup and uses
its fingerprint as the site-registration credential.

Failure posture (deliberate, per the delinquency design + R-H7):
  - Configured but TAMPERED / wrong tenant / unreadable  -> refuse to
    start. That is an integrity failure, not a payment lapse.
  - Configured and EXPIRED -> start, loudly, in grace posture. Expiry
    enforcement is the Console's delinquency state machine (grace ->
    console restriction -> manual suspend); on-prem diagnosis
    infrastructure is never auto-disabled (R-H7).
  - Unconfigured in secure mode -> start with a loud warning (the
    compose lab stack has no license mint at boot); unconfigured in
    insecure mode -> silent.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization

logger = logging.getLogger("harkeniq.cc.license")


class LicenseError(Exception):
    """License integrity/configuration failure — CC refuses to start."""


@dataclass
class LicenseInfo:
    payload: dict
    fingerprint: str
    status: str  # "verified" | "expired"


def _b64url_decode(s: str) -> bytes:
    pad = 4 - len(s) % 4
    if pad != 4:
        s += "=" * pad
    return base64.urlsafe_b64decode(s)


def _canonical_json(d: dict) -> bytes:
    # MUST match harkeniq_console.licensing._canonical_json — the
    # cross-package roundtrip test in tests/unit/cc/test_cc_license.py
    # locks the two implementations together.
    return json.dumps(d, sort_keys=True, separators=(",", ":")).encode("utf-8")


def verify_license_token(public_key_pem: bytes, token: str) -> dict:
    """Verify format, Ed25519 signature, and fingerprint of a license.

    Returns the payload. Does NOT check expiry — the caller decides the
    posture for an expired-but-authentic license (see module docstring).

    Raises:
        LicenseError: on any integrity failure.
    """
    parts = token.split(".")
    if len(parts) != 2:
        raise LicenseError(
            f"invalid license format: expected 2 dot-separated parts, got {len(parts)}"
        )
    try:
        payload_bytes = _b64url_decode(parts[0])
        signature = _b64url_decode(parts[1])
    except Exception as exc:
        raise LicenseError(f"invalid base64url encoding: {exc}") from exc

    try:
        public_key = serialization.load_pem_public_key(public_key_pem)
    except Exception as exc:
        raise LicenseError(f"invalid license verify key: {exc}") from exc
    try:
        public_key.verify(signature, payload_bytes)
    except InvalidSignature:
        raise LicenseError("invalid license signature")

    try:
        payload = json.loads(payload_bytes)
    except json.JSONDecodeError as exc:
        raise LicenseError(f"invalid license payload JSON: {exc}") from exc

    fingerprint = payload.get("fingerprint")
    if not fingerprint:
        raise LicenseError("license payload missing fingerprint")
    without_fp = {k: v for k, v in payload.items() if k != "fingerprint"}
    expected = hashlib.sha256(_canonical_json(without_fp)).hexdigest()
    if fingerprint != expected:
        raise LicenseError(
            "license fingerprint mismatch — payload may have been tampered with"
        )
    return payload


def load_license(config) -> "LicenseInfo | None":
    """Load + verify the license file named by CCConfig.license_key_path.

    Returns LicenseInfo, or None when no license is configured.
    Raises LicenseError on integrity/configuration failures (see module
    docstring for the posture table).
    """
    if not config.license_key_path:
        if not config.insecure:
            logger.warning(
                "No license configured (HARKEN_CC_LICENSE_KEY_PATH empty) — "
                "running WITHOUT a verified license. Site registration will "
                "trust caller-supplied fingerprints."
            )
        return None

    if not config.license_verify_key_path:
        raise LicenseError(
            "license_key_path is set but license_verify_key_path is not — "
            "cannot verify the license (fail-closed)"
        )

    try:
        token = Path(config.license_key_path).read_text().strip()
    except OSError as exc:
        raise LicenseError(f"cannot read license file: {exc}") from exc
    try:
        verify_key = Path(config.license_verify_key_path).read_bytes()
    except OSError as exc:
        raise LicenseError(f"cannot read license verify key: {exc}") from exc

    payload = verify_license_token(verify_key, token)

    tenant = payload.get("sub", "")
    if config.tenant_id and tenant != config.tenant_id:
        raise LicenseError(
            f"license is for tenant {tenant!r}, this CC serves "
            f"{config.tenant_id!r}"
        )

    exp = payload.get("exp")
    status = "verified"
    if not isinstance(exp, (int, float)) or exp <= time.time():
        status = "expired"
        logger.error(
            "License for tenant %r EXPIRED (exp=%s) — running in grace "
            "posture. Renewal is enforced by the Console delinquency flow; "
            "on-prem diagnosis is never auto-disabled (R-H7).",
            tenant, exp,
        )
    else:
        logger.info(
            "License verified: tenant=%s plan=%s fingerprint=%s…",
            tenant, payload.get("plan", ""), payload["fingerprint"][:12],
        )
    return LicenseInfo(
        payload=payload, fingerprint=payload["fingerprint"], status=status,
    )
