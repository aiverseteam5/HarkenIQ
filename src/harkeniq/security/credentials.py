"""Credential provider interface and implementations (R3b-3, OQ-14).

Three implementations of the CredentialProvider protocol:
  - LocalCredentialProvider: reads from agent config (existing behavior)
  - VaultCredentialProvider: HashiCorp Vault KV v2 via httpx (no SDK)
  - MockCredentialProvider: deterministic responses for CI/testing

CredentialProviderChain: tries providers in order, first success wins.
Vault → Local ordering ensures agents always have credentials even when
Vault is down (R-H7: never remotely disable on-prem agents).

Follows the same httpx async pattern as LLMProvider (sm/llm_provider.py).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable

logger = logging.getLogger("harkeniq.security.credentials")


@dataclass
class Credential:
    """BMC credential for a device."""

    username: str
    password: str
    device_id: str = ""
    source: str = ""     # "local" | "vault" | "mock"
    ttl: float = 0.0     # time-to-live in seconds (0 = no expiry)
    fetched_at: float = field(default_factory=time.time)

    @property
    def is_expired(self) -> bool:
        if self.ttl <= 0:
            return False
        return (time.time() - self.fetched_at) > self.ttl


@runtime_checkable
class CredentialProvider(Protocol):
    """Abstract credential provider interface."""

    async def get_credentials(self, device_id: str) -> Optional[Credential]:
        """Fetch credentials for a device. Returns None on failure."""
        ...

    async def store_credentials(
        self, device_id: str, username: str, password: str
    ) -> bool:
        """Store credentials for a device. Returns True on success."""
        ...

    @property
    def provider_name(self) -> str:
        ...


class LocalCredentialProvider:
    """Reads credentials from agent config (existing behavior, formalized).

    The agent's bmc.username and bmc.password config values are the
    local encrypted credentials that every agent has (A1.3).
    """

    def __init__(self, config: Optional[dict] = None) -> None:
        config = config or {}
        bmc = config.get("bmc") or {}
        self._username = bmc.get("username", "")
        self._password = bmc.get("password", "")
        # Per-device overrides (optional)
        self._device_creds: dict[str, tuple[str, str]] = {}

    @property
    def provider_name(self) -> str:
        return "local"

    async def get_credentials(self, device_id: str) -> Optional[Credential]:
        # Check per-device overrides first
        if device_id in self._device_creds:
            u, p = self._device_creds[device_id]
            return Credential(
                username=u, password=p,
                device_id=device_id, source="local",
            )
        if not self._username:
            return None
        return Credential(
            username=self._username,
            password=self._password,
            device_id=device_id,
            source="local",
        )

    async def store_credentials(
        self, device_id: str, username: str, password: str
    ) -> bool:
        self._device_creds[device_id] = (username, password)
        return True


class VaultCredentialProvider:
    """HashiCorp Vault KV v2 credential provider via httpx.

    Stores and retrieves BMC credentials from Vault at path
    ``secret/data/bmc/{device_id}``. Uses the same async httpx pattern
    as LLMProvider (no SDK dependency).

    Config:
        vault_url: Vault API base URL (e.g., "http://127.0.0.1:8200")
        vault_token: Vault authentication token
        mount_path: KV v2 mount path (default: "secret")
        timeout: HTTP request timeout in seconds (default: 10)
    """

    def __init__(
        self,
        vault_url: str,
        vault_token: str,
        mount_path: str = "secret",
        timeout: float = 10.0,
    ) -> None:
        self._url = vault_url.rstrip("/")
        self._token = vault_token
        self._mount = mount_path
        self._timeout = timeout

    @property
    def provider_name(self) -> str:
        return "vault"

    def _headers(self) -> dict[str, str]:
        return {
            "X-Vault-Token": self._token,
            "Content-Type": "application/json",
        }

    async def get_credentials(self, device_id: str) -> Optional[Credential]:
        import httpx
        url = f"{self._url}/v1/{self._mount}/data/bmc/{device_id}"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(url, headers=self._headers())
                resp.raise_for_status()
                data = resp.json()
            secret_data = data.get("data", {}).get("data", {})
            username = secret_data.get("username", "")
            password = secret_data.get("password", "")
            if not username:
                logger.warning("Vault returned empty username for %s", device_id)
                return None
            ttl = float(secret_data.get("ttl", 0))
            return Credential(
                username=username,
                password=password,
                device_id=device_id,
                source="vault",
                ttl=ttl,
            )
        except httpx.TimeoutException:
            logger.warning("Vault request timed out for %s", device_id)
            return None
        except httpx.HTTPStatusError as e:
            logger.warning(
                "Vault HTTP error for %s: %d %s",
                device_id, e.response.status_code, e.response.text[:200],
            )
            return None
        except Exception as e:
            logger.warning("Vault error for %s: %s", device_id, e)
            return None

    async def store_credentials(
        self, device_id: str, username: str, password: str
    ) -> bool:
        import httpx
        url = f"{self._url}/v1/{self._mount}/data/bmc/{device_id}"
        payload = {
            "data": {
                "username": username,
                "password": password,
            }
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    url, headers=self._headers(), json=payload,
                )
                resp.raise_for_status()
            logger.info("Stored credentials in Vault for %s", device_id)
            return True
        except httpx.TimeoutException:
            logger.warning("Vault store timed out for %s", device_id)
            return False
        except httpx.HTTPStatusError as e:
            logger.warning(
                "Vault store error for %s: %d",
                device_id, e.response.status_code,
            )
            return False
        except Exception as e:
            logger.warning("Vault store error for %s: %s", device_id, e)
            return False


class MockCredentialProvider:
    """Deterministic credential provider for CI/testing."""

    def __init__(self, creds: Optional[dict[str, tuple[str, str]]] = None) -> None:
        self._creds = creds or {}
        self._stored: dict[str, tuple[str, str]] = {}

    @property
    def provider_name(self) -> str:
        return "mock"

    async def get_credentials(self, device_id: str) -> Optional[Credential]:
        # Check stored first (from store_credentials), then defaults
        if device_id in self._stored:
            u, p = self._stored[device_id]
        elif device_id in self._creds:
            u, p = self._creds[device_id]
        else:
            # Default mock credentials
            u, p = "mock-admin", "mock-password"
        return Credential(
            username=u, password=p,
            device_id=device_id, source="mock",
        )

    async def store_credentials(
        self, device_id: str, username: str, password: str
    ) -> bool:
        self._stored[device_id] = (username, password)
        return True


class CredentialProviderChain:
    """Tries providers in order; first success wins.

    Default order: Vault → Local ensures agents always have credentials
    even when Vault is down (R-H7: never remotely disable on-prem agents).
    """

    def __init__(self, providers: list) -> None:
        self._providers = providers

    @property
    def provider_name(self) -> str:
        names = [p.provider_name for p in self._providers]
        return f"chain({','.join(names)})"

    async def get_credentials(self, device_id: str) -> Optional[Credential]:
        for provider in self._providers:
            try:
                cred = await provider.get_credentials(device_id)
                if cred is not None:
                    return cred
            except Exception as e:
                logger.warning(
                    "Provider %s failed for %s: %s",
                    provider.provider_name, device_id, e,
                )
        logger.error("All credential providers failed for %s", device_id)
        return None

    async def store_credentials(
        self, device_id: str, username: str, password: str
    ) -> bool:
        # Store in the first provider that supports it
        for provider in self._providers:
            try:
                if await provider.store_credentials(device_id, username, password):
                    return True
            except Exception:
                continue
        return False
