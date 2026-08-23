"""SM-side agent identity management (spec A2.4).

Stores agent public keys, issues SM-signed certificates and authorization
leases.  The SM generates its own Ed25519 keypair on first start and
persists it in the platform_settings table.

Uses the same signing patterns as Console licensing.py.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from sqlalchemy import select, text

from harkeniq_sm.db.models import AgentIdentityRow

logger = logging.getLogger("harkeniq.sm.identity")

# Default lease parameters (A2.2)
DEFAULT_LEASE_DURATION = 300.0  # 5 minutes
DEFAULT_GRACE_DURATION = 60.0   # 1 minute


def _canonical_json(d: dict) -> bytes:
    return json.dumps(d, sort_keys=True, separators=(",", ":")).encode("utf-8")


class AgentIdentityService:
    """SM-side agent identity and lease management."""

    def __init__(self, sessionmaker, sm_private_key: Ed25519PrivateKey):
        self.sessionmaker = sessionmaker
        self._sm_private_key = sm_private_key
        self._sm_public_key = sm_private_key.public_key()

    @property
    def sm_public_key_pem(self) -> bytes:
        return self._sm_public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )

    async def register_agent(
        self,
        agent_id: str,
        public_key_pem: bytes,
        site_name: str,
        tenant_id: str = "",
    ) -> tuple[bytes, bytes]:
        """Store agent public key, issue SM certificate.

        Returns (sm_public_key_pem, agent_certificate).
        """
        # Validate the public key
        try:
            serialization.load_pem_public_key(public_key_pem)
        except Exception as e:
            raise ValueError(f"Invalid agent public key: {e}") from e

        # Verify agent_id matches the public key
        expected_id = hashlib.sha256(public_key_pem).hexdigest()[:16]
        if agent_id != expected_id:
            raise ValueError(
                f"agent_id {agent_id} does not match public key hash {expected_id}"
            )

        # Store in database
        async with self.sessionmaker() as session:
            existing = await session.execute(
                select(AgentIdentityRow).where(
                    AgentIdentityRow.agent_id == agent_id
                )
            )
            row = existing.scalar_one_or_none()
            if row is not None:
                # Re-registration with same key: update timestamp
                if row.public_key_pem == public_key_pem:
                    row.revoked_at = None  # clear any prior revocation
                    logger.info("Agent %s re-registered (same key)", agent_id)
                else:
                    raise ValueError(
                        f"Agent {agent_id} already registered with a different key. "
                        "Re-enroll with a new provisioning token."
                    )
            else:
                row = AgentIdentityRow(
                    agent_id=agent_id,
                    public_key_pem=public_key_pem,
                )
                session.add(row)
                logger.info("Registered new agent identity: %s", agent_id)

            # Sign the agent certificate
            cert_payload = _canonical_json({
                "agent_id": agent_id,
                "public_key_sha256": hashlib.sha256(public_key_pem).hexdigest(),
                "site": site_name,
                "tenant": tenant_id,
                "issued_at": time.time(),
            })
            certificate = cert_payload + self._sm_private_key.sign(cert_payload)
            row.certificate = certificate
            await session.commit()

        return self.sm_public_key_pem, certificate

    async def issue_lease(
        self,
        agent_id: str,
        action_classes: Optional[list[str]] = None,
        risk_ceiling: str = "low",
        budget_remaining: Optional[dict[str, int]] = None,
        suppression_domains: Optional[list[str]] = None,
        stop_switch: bool = False,
        lease_duration: float = DEFAULT_LEASE_DURATION,
        grace_duration: float = DEFAULT_GRACE_DURATION,
    ) -> tuple[bytes, int]:
        """Sign and return an authorization lease for the agent.

        Returns (lease_bytes, lease_expiry_unix) where lease_bytes is
        canonical_json + ed25519_signature (64 bytes).
        """
        if action_classes is None:
            action_classes = ["IDENTIFY_LED", "COLLECT_DIAGNOSTICS", "FAN_RESET"]
        if budget_remaining is None:
            budget_remaining = {a: -1 for a in action_classes}

        now = time.time()
        payload = {
            "v": 1,
            "agent_id": agent_id,
            "action_classes": sorted(action_classes),
            "risk_ceiling": risk_ceiling,
            "budget_remaining": budget_remaining,
            "lease_expiry": now + lease_duration,
            "grace_expiry": now + lease_duration + grace_duration,
            "suppression_domains": sorted(suppression_domains or []),
            "stop_switch": stop_switch,
            "issued_at": now,
        }
        payload_bytes = _canonical_json(payload)
        signature = self._sm_private_key.sign(payload_bytes)
        lease_expiry_unix = int(now + lease_duration)
        return payload_bytes + signature, lease_expiry_unix

    async def verify_agent_signature(
        self, agent_id: str, message: bytes, signature: bytes
    ) -> bool:
        """Verify a message was signed by the agent's registered public key."""
        async with self.sessionmaker() as session:
            result = await session.execute(
                select(AgentIdentityRow.public_key_pem).where(
                    AgentIdentityRow.agent_id == agent_id
                )
            )
            pem = result.scalar_one_or_none()
            if pem is None:
                return False

        try:
            public_key = serialization.load_pem_public_key(pem)
            public_key.verify(signature, message)
            return True
        except InvalidSignature:
            return False

    async def revoke_agent(self, agent_id: str) -> bool:
        """Mark an agent identity as revoked."""
        async with self.sessionmaker() as session:
            result = await session.execute(
                select(AgentIdentityRow).where(
                    AgentIdentityRow.agent_id == agent_id
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                return False
            from datetime import datetime, timezone
            row.revoked_at = datetime.now(timezone.utc)
            await session.commit()
            logger.warning("Revoked agent identity: %s", agent_id)
            return True


# ---------------------------------------------------------------------------
# SM keypair bootstrap
# ---------------------------------------------------------------------------


def load_or_generate_sm_keypair(sessionmaker_sync) -> Ed25519PrivateKey:
    """Load SM Ed25519 keypair from DB, or generate and store one.

    Uses the platform_settings pattern (key-value table) from the existing
    SM config. Called once at SM startup.
    """
    # Use sync session for startup initialization
    with sessionmaker_sync() as session:
        # Try to load existing keypair
        result = session.execute(
            text("SELECT value FROM agent_meta WHERE key = 'sm_private_key_pem'")
        )
        row = result.fetchone()
        if row is not None:
            private_pem = row[0].encode() if isinstance(row[0], str) else row[0]
            logger.info("Loaded SM identity keypair from database")
            return serialization.load_pem_private_key(private_pem, password=None)

        # Generate new keypair
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
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        session.execute(
            text(
                "INSERT OR REPLACE INTO agent_meta (key, value, updated_at) "
                "VALUES (:k, :v, :t)"
            ),
            {"k": "sm_private_key_pem", "v": private_pem.decode(), "t": now},
        )
        session.execute(
            text(
                "INSERT OR REPLACE INTO agent_meta (key, value, updated_at) "
                "VALUES (:k, :v, :t)"
            ),
            {"k": "sm_public_key_pem", "v": public_pem.decode(), "t": now},
        )
        session.commit()
        logger.info("Generated new SM identity keypair")
        return private_key
