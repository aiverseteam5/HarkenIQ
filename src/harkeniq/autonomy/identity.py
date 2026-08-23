"""Per-agent Ed25519 identity (spec A2.4).

Each agent generates an Ed25519 keypair on first boot.  The agent_id is
derived from the public key (SHA-256[:16] hex), so identity follows the
key.  The SM pins the public key at registration and issues signed
authorization leases.  The agent pins the SM's public key and verifies
lease signatures locally.

Reuses the Ed25519 patterns from Console licensing.py:
- cryptography.hazmat.primitives.asymmetric.ed25519
- canonical JSON for signing payloads
- base64url compact wire format
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

from cryptography.exceptions import InvalidSignature
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

logger = logging.getLogger("harkeniq.autonomy.identity")


# ---------------------------------------------------------------------------
# Helpers (same patterns as licensing.py)
# ---------------------------------------------------------------------------


def _canonical_json(d: dict) -> bytes:
    """Deterministic JSON bytes (sorted keys, no whitespace)."""
    return json.dumps(d, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _derive_fernet_key(checkpoint_path: str) -> bytes:
    """Derive a Fernet key from machine-id + checkpoint path.

    This is a fallback when HARKEN_IDENTITY_KEY is not set.  It binds the
    encrypted private key to this specific machine, so a checkpoint file
    copied elsewhere cannot be decrypted without the same machine-id.
    """
    machine_id = ""
    try:
        machine_id = open("/etc/machine-id").read().strip()
    except OSError:
        pass
    seed = f"{machine_id}:{checkpoint_path}".encode()
    # Fernet requires 32 bytes URL-safe base64.  SHA-256 gives 32 raw bytes.
    import base64

    raw = hashlib.sha256(seed).digest()
    return base64.urlsafe_b64encode(raw)


def _get_fernet(checkpoint_path: str = "") -> Fernet:
    """Return a Fernet instance for encrypting the private key at rest."""
    env_key = os.environ.get("HARKEN_IDENTITY_KEY")
    if env_key:
        return Fernet(env_key.encode() if isinstance(env_key, str) else env_key)
    return Fernet(_derive_fernet_key(checkpoint_path))


# ---------------------------------------------------------------------------
# AgentIdentity
# ---------------------------------------------------------------------------


@dataclass
class AgentIdentity:
    """Per-agent Ed25519 identity."""

    agent_id: str  # SHA-256(public_key_pem)[:16] hex
    _private_key: Ed25519PrivateKey = field(repr=False)
    _public_key: Ed25519PublicKey = field(repr=False)
    sm_public_key_pem: Optional[bytes] = None
    sm_certificate: Optional[bytes] = None
    revoked: bool = False

    @property
    def public_key_pem(self) -> bytes:
        return self._public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    @classmethod
    def generate(cls) -> AgentIdentity:
        """Generate a new Ed25519 keypair.  Called once on first boot."""
        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key()
        public_pem = public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        agent_id = _sha256_hex(public_pem)[:16]
        logger.info("Generated new agent identity: %s", agent_id)
        return cls(
            agent_id=agent_id,
            _private_key=private_key,
            _public_key=public_key,
        )

    def sign(self, message: bytes) -> bytes:
        """Sign a message with the agent's private key."""
        return self._private_key.sign(message)

    def verify_sm_signature(self, message: bytes, signature: bytes) -> bool:
        """Verify a message was signed by the pinned SM public key."""
        if self.sm_public_key_pem is None:
            return False
        try:
            sm_key = serialization.load_pem_public_key(self.sm_public_key_pem)
            sm_key.verify(signature, message)
            return True
        except InvalidSignature:
            return False

    def set_sm_public_key(self, pem: bytes) -> None:
        """Pin the SM's public key (called once during registration)."""
        if self.sm_public_key_pem is not None:
            logger.warning(
                "SM public key already pinned; ignoring new key "
                "(re-enroll to change SM)"
            )
            return
        # Validate it's a real public key
        serialization.load_pem_public_key(pem)
        self.sm_public_key_pem = pem
        logger.info("Pinned SM public key for agent %s", self.agent_id)

    def is_valid(self) -> bool:
        """Identity is usable: not revoked and SM key pinned."""
        return not self.revoked and self.sm_public_key_pem is not None

    # -- persistence --------------------------------------------------------

    def save(self, db, checkpoint_path: str = "") -> None:
        """Persist identity to checkpoint SQLite.

        ``db`` is a sqlite3.Connection (from CheckpointManager).
        """
        fernet = _get_fernet(checkpoint_path)
        private_pem = self._private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        encrypted = fernet.encrypt(private_pem)
        db.execute(
            "INSERT OR REPLACE INTO agent_identity "
            "(agent_id, public_key_pem, private_key_enc, sm_public_key_pem, "
            " sm_certificate, revoked, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))",
            (
                self.agent_id,
                self.public_key_pem,
                encrypted,
                self.sm_public_key_pem,
                self.sm_certificate,
                1 if self.revoked else 0,
            ),
        )
        db.commit()

    @classmethod
    def load(cls, db, checkpoint_path: str = "") -> Optional[AgentIdentity]:
        """Load identity from checkpoint SQLite.  Returns None if not found."""
        try:
            row = db.execute(
                "SELECT agent_id, public_key_pem, private_key_enc, "
                "sm_public_key_pem, sm_certificate, revoked "
                "FROM agent_identity LIMIT 1"
            ).fetchone()
        except Exception:
            # Table may not exist in old checkpoints
            return None
        if row is None:
            return None

        agent_id, pub_pem, priv_enc, sm_pub, sm_cert, revoked_int = row
        fernet = _get_fernet(checkpoint_path)
        try:
            private_pem = fernet.decrypt(priv_enc)
        except InvalidToken:
            logger.error(
                "Cannot decrypt agent private key — machine-id or "
                "HARKEN_IDENTITY_KEY changed.  Agent must re-enroll."
            )
            return None

        private_key = serialization.load_pem_private_key(private_pem, password=None)
        public_key = serialization.load_pem_public_key(pub_pem)
        identity = cls(
            agent_id=agent_id,
            _private_key=private_key,
            _public_key=public_key,
            sm_public_key_pem=sm_pub if sm_pub else None,
            sm_certificate=sm_cert if sm_cert else None,
            revoked=bool(revoked_int),
        )
        logger.info("Loaded agent identity: %s (revoked=%s)", agent_id, identity.revoked)
        return identity

    def mark_revoked(self, db) -> None:
        """Persist revoked state.  Survives restart."""
        self.revoked = True
        db.execute(
            "UPDATE agent_identity SET revoked = 1 WHERE agent_id = ?",
            (self.agent_id,),
        )
        db.commit()
        logger.warning("Agent %s marked as revoked", self.agent_id)
