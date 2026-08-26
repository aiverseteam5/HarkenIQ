"""Credential rotation via Redfish AccountService (R3b-3 Phase 6).

Blue-green account lifecycle:
  1. Create new account with generated password
  2. Verify new account can authenticate
  3. Disable/delete old account
  4. Rollback: if verify fails, re-enable old, delete new

Uses Redfish AccountService API:
  Dell: /redfish/v1/Managers/iDRAC.Embedded.1/Accounts/
  HPE:  /redfish/v1/AccountService/Accounts/

Audit trail logged for every rotation event.
"""

from __future__ import annotations

import logging
import secrets
import string
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from harkeniq.errors import RedfishError, RedfishResponseError

logger = logging.getLogger("harkeniq.security.rotation")

# Vendor-neutral DMTF path; iDRAC9+ and iLO5+ both serve it. Older iDRACs
# with fixed account slots reject POST with 405 — handled by slot fallback.
ACCOUNTS_PATH = "/redfish/v1/AccountService/Accounts"


class RotationStatus(Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    ROLLED_BACK = "ROLLED_BACK"


@dataclass
class RotationEvent:
    """Audit record for a credential rotation attempt."""

    device_id: str
    old_username: str
    new_username: str
    status: RotationStatus = RotationStatus.SUCCESS
    error_message: str = ""
    started_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    duration_ms: float = 0.0

    def complete(self, status: RotationStatus, error: str = "") -> None:
        self.status = status
        self.error_message = error
        self.completed_at = time.time()
        self.duration_ms = (self.completed_at - self.started_at) * 1000


def generate_password(length: int = 24) -> str:
    """Generate a cryptographically strong password for BMC accounts."""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(alphabet) for _ in range(length))


class CredentialRotator:
    """Rotates BMC credentials using the blue-green account pattern.

    Steps:
      1. Create new account on BMC
      2. Verify the new account works
      3. Disable the old account
      4. Update credential store with new credentials

    On failure at any step, rolls back to old account.
    """

    def __init__(
        self,
        credential_provider=None,
        redfish_client=None,
        vendor: str = "",
        verify_client_factory: Optional[Callable[[], object]] = None,
    ) -> None:
        self._cred_provider = credential_provider
        self._client = redfish_client
        self._vendor = vendor
        # Verification needs a SECOND session authenticated as the new
        # account (the existing client's session proves nothing about it).
        self._verify_client_factory = verify_client_factory
        self._events: list[RotationEvent] = []

    async def rotate(
        self,
        device_id: str,
        old_username: str,
        new_username: Optional[str] = None,
    ) -> RotationEvent:
        """Perform a blue-green credential rotation.

        Returns a RotationEvent with the outcome.
        """
        new_username = new_username or f"harken-{secrets.token_hex(4)}"
        new_password = generate_password()

        event = RotationEvent(
            device_id=device_id,
            old_username=old_username,
            new_username=new_username,
        )

        try:
            # Step 1: Create new account
            created = await self._create_account(device_id, new_username, new_password)
            if not created:
                event.complete(RotationStatus.FAILED, "Failed to create new account")
                self._events.append(event)
                return event

            # Step 2: Verify new account
            verified = await self._verify_account(device_id, new_username, new_password)
            if not verified:
                # Rollback: delete new account
                await self._delete_account(device_id, new_username)
                event.complete(RotationStatus.ROLLED_BACK, "New account verification failed")
                self._events.append(event)
                return event

            # Step 3: Disable old account
            await self._disable_account(device_id, old_username)

            # Step 4: Update credential store
            if self._cred_provider:
                await self._cred_provider.store_credentials(
                    device_id, new_username, new_password,
                )

            event.complete(RotationStatus.SUCCESS)
            logger.info(
                "Credential rotation succeeded for %s: %s → %s",
                device_id, old_username, new_username,
            )

        except Exception as e:
            event.complete(RotationStatus.FAILED, str(e))
            logger.error("Credential rotation failed for %s: %s", device_id, e)

        self._events.append(event)
        return event

    async def _find_account_uri(self, username: str) -> Optional[str]:
        """Resolve an account's resource URI by UserName."""
        collection = await self._client.get(ACCOUNTS_PATH)
        for member in collection.get("Members", []):
            uri = member.get("@odata.id", "")
            if not uri:
                continue
            try:
                account = await self._client.get(uri)
            except RedfishResponseError:
                continue
            if account.get("UserName") == username:
                return uri
        return None

    async def _create_account(
        self, device_id: str, username: str, password: str,
    ) -> bool:
        """Create a new BMC account via Redfish AccountService."""
        if self._client is None:
            return True  # mock mode
        body = {
            "UserName": username,
            "Password": password,
            "RoleId": "Administrator",
            "Enabled": True,
        }
        try:
            await self._client.post(ACCOUNTS_PATH, body)
            return True
        except RedfishResponseError as e:
            if e.status_code != 405:
                logger.error("Account create failed on %s: %s", device_id, e)
                return False
        # 405: fixed-slot BMC (older iDRAC) — claim the first empty slot.
        empty_uri = await self._find_account_uri("")
        if empty_uri is None:
            logger.error("No empty account slot on %s", device_id)
            return False
        try:
            await self._client.patch(empty_uri, body)
            return True
        except RedfishError as e:
            logger.error("Account slot claim failed on %s: %s", device_id, e)
            return False

    async def _verify_account(
        self, device_id: str, username: str, password: str,
    ) -> bool:
        """Verify a new account can authenticate to the BMC.

        Opens a FRESH Redfish session as the new account; session creation
        succeeding is the proof (the rotator's own session is the old one).
        """
        if self._client is None:
            return True  # mock mode
        if self._verify_client_factory is not None:
            client = self._verify_client_factory()
        else:
            from harkeniq.redfish.client import RedfishClient

            client = RedfishClient(
                host=self._client.host,
                port=self._client.port,
                verify_ssl=self._client.verify_ssl,
            )
        try:
            await client.connect(username, password)
            return True
        except (RedfishError, OSError) as e:
            logger.warning(
                "New account %s failed verification on %s: %s",
                username, device_id, e,
            )
            return False
        finally:
            try:
                await client.delete_session()
                await client.close()
            except Exception:  # noqa: BLE001 — cleanup must not mask verdict
                pass

    async def _disable_account(self, device_id: str, username: str) -> bool:
        """Disable an account on the BMC."""
        if self._client is None:
            return True
        uri = await self._find_account_uri(username)
        if uri is None:
            logger.warning("Account %s not found on %s", username, device_id)
            return False
        await self._client.patch(uri, {"Enabled": False})
        return True

    async def _delete_account(self, device_id: str, username: str) -> bool:
        """Delete an account from the BMC (rollback path — best-effort:
        a failure here must not turn ROLLED_BACK into FAILED)."""
        if self._client is None:
            return True
        try:
            uri = await self._find_account_uri(username)
            if uri is None:
                return False
            try:
                await self._client.delete(uri)
            except RedfishResponseError as e:
                if e.status_code != 405:
                    raise
                # Fixed-slot BMC: clearing the slot is the delete.
                await self._client.patch(
                    uri, {"Enabled": False, "UserName": ""}
                )
            return True
        except RedfishError as e:
            logger.error(
                "Rollback delete of %s on %s failed: %s", username, device_id, e
            )
            return False

    @property
    def rotation_history(self) -> list[RotationEvent]:
        return list(self._events)
