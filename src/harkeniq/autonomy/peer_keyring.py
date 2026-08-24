"""Peer public key ring for mesh claim verification (spec A2.4, R3b-2).

Stores peer Ed25519 public keys distributed by the SM during registration.
Used to verify Ed25519-signed claims from peer agents.  The SM signs the
key bundle with its own Ed25519 key so agents never trust unverified keys.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

logger = logging.getLogger("harkeniq.autonomy.keyring")


def _canonical_json(d: dict) -> bytes:
    return json.dumps(d, sort_keys=True, separators=(",", ":")).encode("utf-8")


class PeerKeyRing:
    """Stores and verifies peer agent Ed25519 public keys.

    Keys are distributed by the SM (trust root) during agent registration.
    The key bundle is SM-signed so agents never trust unverified keys.
    """

    def __init__(self) -> None:
        self._keys: dict[str, Ed25519PublicKey] = {}

    def load_from_bundle(
        self,
        peer_keys: dict[str, bytes],
        signature: bytes,
        sm_public_key: Ed25519PublicKey,
        exclude_self: str = "",
    ) -> int:
        """Load peer keys from an SM-signed bundle.

        Args:
            peer_keys: {agent_id: public_key_pem} mapping from SM.
            signature: SM Ed25519 signature over canonical JSON of peer_keys.
            sm_public_key: SM's public key (already pinned during registration).
            exclude_self: agent_id to exclude (own key).

        Returns:
            Number of peer keys loaded.

        Raises:
            InvalidSignature: if the SM signature is invalid.
            ValueError: if a key cannot be parsed or agent_id doesn't match.
        """
        # Verify SM signature over the key bundle
        # Build canonical form: {agent_id: hex(pem)} sorted
        canonical = _canonical_json(
            {k: v.hex() for k, v in sorted(peer_keys.items())}
        )
        sm_public_key.verify(signature, canonical)

        loaded = 0
        for agent_id, pem in peer_keys.items():
            if agent_id == exclude_self:
                continue
            # Validate agent_id matches the public key hash
            expected_id = hashlib.sha256(pem).hexdigest()[:16]
            if agent_id != expected_id:
                logger.warning(
                    "Peer key agent_id mismatch: %s != hash %s, skipping",
                    agent_id, expected_id,
                )
                continue
            try:
                public_key = serialization.load_pem_public_key(pem)
                if not isinstance(public_key, Ed25519PublicKey):
                    logger.warning("Peer %s key is not Ed25519, skipping", agent_id)
                    continue
                self._keys[agent_id] = public_key
                loaded += 1
            except Exception as e:
                logger.warning("Cannot load peer %s key: %s", agent_id, e)
        logger.info("Loaded %d peer keys into keyring", loaded)
        return loaded

    def add_key(self, agent_id: str, public_key: Ed25519PublicKey) -> None:
        """Add a single verified peer key (for testing or manual injection)."""
        self._keys[agent_id] = public_key

    def get_key(self, agent_id: str) -> Optional[Ed25519PublicKey]:
        """Look up a peer's public key."""
        return self._keys.get(agent_id)

    def verify(self, agent_id: str, message: bytes, signature: bytes) -> bool:
        """Verify a message signed by a peer agent.

        Returns False if the agent_id is unknown or the signature is invalid.
        """
        key = self._keys.get(agent_id)
        if key is None:
            return False
        try:
            key.verify(signature, message)
            return True
        except InvalidSignature:
            return False

    def known_peers(self) -> list[str]:
        """Return all known peer agent IDs."""
        return list(self._keys.keys())

    def __len__(self) -> int:
        return len(self._keys)

    def __contains__(self, agent_id: str) -> bool:
        return agent_id in self._keys
