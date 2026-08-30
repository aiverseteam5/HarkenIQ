"""Keycloak Admin REST API client.

Wraps the Keycloak Admin REST API to automate tenant realm provisioning.
Uses ``httpx.AsyncClient`` (async-native) rather than the sync-only
python-keycloak library.

When a new tenant is created the Console automatically:
1. Creates a Keycloak realm for the tenant
2. Creates an OIDC client in that realm
3. Creates the tenant owner user
4. Assigns the tenant_owner role
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

import httpx

logger = logging.getLogger("harkeniq.console.keycloak")

# Default roles provisioned in every tenant realm (spec §4).
DEFAULT_REALM_ROLES = [
    "tenant_owner",
    "site_admin",
    "operator",
    "auditor",
    "viewer",
]

#: E1.4: 2.0s was too short for realm provisioning. Creating a realm and
#: its five roles and its client is six sequential admin calls, and a
#: Keycloak under load takes longer than that per call -- so provisioning
#: failed with an httpx timeout, whose str() is EMPTY, producing
#: "Keycloak realm creation failed: " with nothing after the colon.
_TIMEOUT = 10.0
_MAX_RETRIES = 1  # retry once on 5xx


class KeycloakError(Exception):
    """Keycloak admin operation failed."""

    def __init__(self, message: str, status_code: int = 0) -> None:
        self.message = message
        self.status_code = status_code
        super().__init__(message)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_id_from_location(headers: httpx.Headers) -> str:
    """Extract the resource UUID from a Keycloak ``Location`` header."""
    location = headers.get("location", "")
    if location:
        return location.rstrip("/").rsplit("/", 1)[-1]
    return ""


def _raise_for_status(resp: httpx.Response, context: str) -> None:
    """Translate Keycloak HTTP errors into ``KeycloakError``."""
    if resp.is_success:
        return
    code = resp.status_code
    try:
        body = resp.json()
        detail = body.get("errorMessage") or body.get("error_description") or body.get("error", "")
    except Exception:
        detail = resp.text[:300]

    if code == 409:
        raise KeycloakError(f"{context} already exists", status_code=409)
    if code == 404:
        raise KeycloakError(f"{context} not found", status_code=404)
    if code in (401, 403):
        raise KeycloakError(
            f"admin authentication failed: {detail}", status_code=code,
        )
    raise KeycloakError(
        f"{context} failed ({code}): {detail}", status_code=code,
    )


# ---------------------------------------------------------------------------
# Real client
# ---------------------------------------------------------------------------

class KeycloakAdminClient:
    """Async wrapper around the Keycloak Admin REST API."""

    def __init__(
        self,
        keycloak_url: str,
        admin_user: str,
        admin_password: str,
    ) -> None:
        # Strip trailing slash for consistent URL building.
        self.url = keycloak_url.rstrip("/")
        self.admin_user = admin_user
        self.admin_password = admin_password
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    # -- token management ---------------------------------------------------

    async def get_admin_token(self) -> str:
        """Obtain (or return cached) master-realm admin access token.

        The token is refreshed when it expires or is within 60 s of expiry.
        """
        now = time.monotonic()
        if self._token and self._token_expires_at - 60 > now:
            return self._token

        token_url = f"{self.url}/realms/master/protocol/openid-connect/token"
        data = {
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": self.admin_user,
            "password": self.admin_password,
        }
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(token_url, data=data)

        _raise_for_status(resp, "admin token request")

        payload = resp.json()
        self._token = payload["access_token"]
        expires_in = payload.get("expires_in", 300)
        self._token_expires_at = time.monotonic() + expires_in
        logger.debug("admin token acquired, expires_in=%s", expires_in)
        return self._token

    # -- internal request helpers -------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        context: str = "keycloak request",
    ) -> httpx.Response:
        """Issue an authenticated admin request with retry on 5xx."""
        token = await self.get_admin_token()
        headers = {"Authorization": f"Bearer {token}"}
        url = f"{self.url}{path}"

        attempt = 0
        last_resp: httpx.Response | None = None
        while attempt <= _MAX_RETRIES:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                last_resp = await client.request(
                    method, url, json=json, headers=headers,
                )
            if last_resp.status_code < 500:
                break
            attempt += 1
            if attempt <= _MAX_RETRIES:
                logger.warning(
                    "%s returned %s, retrying (%d/%d)",
                    context, last_resp.status_code, attempt, _MAX_RETRIES,
                )

        assert last_resp is not None
        _raise_for_status(last_resp, context)
        return last_resp

    # -- realm management ---------------------------------------------------

    async def create_realm(self, tenant_slug: str) -> str:
        """Create a Keycloak realm for a new tenant.

        Provisions the five default tenant roles and returns the realm name.
        """
        body = {
            "realm": tenant_slug,
            "enabled": True,
            "displayName": f"HarkenIQ - {tenant_slug}",
            "sslRequired": "none",
            "registrationAllowed": False,
            "loginWithEmailAllowed": True,
        }
        await self._request(
            "POST", "/admin/realms",
            json=body, context=f"create realm '{tenant_slug}'",
        )
        logger.info("realm '%s' created", tenant_slug)

        # Provision default roles. Idempotent, and reusable for a realm
        # whose provisioning failed part-way through.
        await self.ensure_realm_roles(tenant_slug)
        logger.info(
            "default roles provisioned in realm '%s': %s",
            tenant_slug, DEFAULT_REALM_ROLES,
        )
        return tenant_slug

    async def ensure_realm_roles(self, realm: str) -> list[str]:
        """Make sure the five tenant roles exist. Idempotent. E1.4.

        Separate from `create_realm` on purpose: provisioning is six
        sequential admin calls, and a failure part-way through leaves a
        realm that exists with roles that do not. Treating "the realm
        exists" as "the realm is provisioned" is how a tenant ends up
        with a realm nobody can hold a role in -- which is exactly what
        happened on the first live run.
        """
        created: list[str] = []
        for role_name in DEFAULT_REALM_ROLES:
            try:
                await self._request(
                    "POST", f"/admin/realms/{realm}/roles",
                    json={"name": role_name},
                    context=f"create role '{role_name}' in '{realm}'",
                )
                created.append(role_name)
            except KeycloakError as exc:
                if exc.status_code != 409:
                    raise
        return created

    # -- client management --------------------------------------------------

    async def create_client(
        self,
        realm: str,
        client_id: str,
        redirect_uris: list[str],
    ) -> str:
        """Register an OIDC client in a tenant realm.

        Returns the Keycloak-internal client UUID.
        """
        body = {
            "clientId": client_id,
            "enabled": True,
            "publicClient": True,
            "standardFlowEnabled": True,
            "directAccessGrantsEnabled": True,
            "redirectUris": redirect_uris,
            "webOrigins": ["*"],
        }
        resp = await self._request(
            "POST", f"/admin/realms/{realm}/clients",
            json=body, context=f"create client '{client_id}' in '{realm}'",
        )

        # Keycloak returns the UUID in the Location header.
        client_uuid = _extract_id_from_location(resp.headers)
        if client_uuid:
            logger.info(
                "client '%s' created in realm '%s' (uuid=%s)",
                client_id, realm, client_uuid,
            )
            return client_uuid

        # Fallback: query by clientId.
        list_resp = await self._request(
            "GET", f"/admin/realms/{realm}/clients?clientId={client_id}",
            context=f"lookup client '{client_id}' in '{realm}'",
        )
        clients = list_resp.json()
        if clients:
            client_uuid = clients[0]["id"]
            logger.info(
                "client '%s' resolved via query (uuid=%s)", client_id, client_uuid,
            )
            return client_uuid

        raise KeycloakError(
            f"client '{client_id}' created but UUID could not be determined",
        )

    # -- user management ----------------------------------------------------

    async def create_user(
        self,
        realm: str,
        email: str,
        temporary_password: str = "",
    ) -> str:
        """Create a user in a realm. Returns the Keycloak user UUID."""
        password = temporary_password or "changeme"
        body = {
            "username": email,
            "email": email,
            "enabled": True,
            "emailVerified": True,
            "credentials": [
                {
                    "type": "password",
                    "value": password,
                    "temporary": True,
                },
            ],
        }
        resp = await self._request(
            "POST", f"/admin/realms/{realm}/users",
            json=body, context=f"create user '{email}' in '{realm}'",
        )
        user_id = _extract_id_from_location(resp.headers)
        if not user_id:
            raise KeycloakError(
                f"user '{email}' created but UUID could not be determined",
            )
        logger.info("user '%s' created in realm '%s' (id=%s)", email, realm, user_id)
        return user_id

    async def assign_realm_role(
        self,
        realm: str,
        user_id: str,
        role_name: str,
    ) -> None:
        """Assign a realm-level role to a user."""
        # Look up role representation.
        role_resp = await self._request(
            "GET", f"/admin/realms/{realm}/roles/{role_name}",
            context=f"get role '{role_name}' in '{realm}'",
        )
        role = role_resp.json()

        # POST role mapping.
        await self._request(
            "POST",
            f"/admin/realms/{realm}/users/{user_id}/role-mappings/realm",
            json=[{"id": role["id"], "name": role["name"]}],
            context=f"assign role '{role_name}' to user '{user_id}'",
        )
        logger.info(
            "role '%s' assigned to user '%s' in realm '%s'",
            role_name, user_id, realm,
        )

    async def delete_user(self, realm: str, user_id: str) -> None:
        """Remove a user from a realm."""
        await self._request(
            "DELETE", f"/admin/realms/{realm}/users/{user_id}",
            context=f"delete user '{user_id}' in '{realm}'",
        )
        logger.info("user '%s' deleted from realm '%s'", user_id, realm)

    async def list_realm_users(self, realm: str) -> list[dict]:
        """List all users in a realm."""
        resp = await self._request(
            "GET", f"/admin/realms/{realm}/users",
            context=f"list users in '{realm}'",
        )
        return resp.json()

    # -- realm teardown (testing) -------------------------------------------

    async def delete_realm(self, realm: str) -> None:
        """Delete a realm. Intended for cleanup/testing."""
        await self._request(
            "DELETE", f"/admin/realms/{realm}",
            context=f"delete realm '{realm}'",
        )
        logger.info("realm '%s' deleted", realm)


# ---------------------------------------------------------------------------
# Mock client (unit-test double — no real Keycloak required)
# ---------------------------------------------------------------------------

class MockKeycloakAdminClient:
    """In-memory fake of ``KeycloakAdminClient`` for unit tests.

    Stores state in plain dicts so assertions are straightforward.
    All return values match the real client's types.
    """

    def __init__(self) -> None:
        self._realms: dict[str, dict] = {}
        # {realm: {user_id: user_dict}}
        self._users: dict[str, dict[str, dict]] = {}
        # {realm: {client_uuid: client_dict}}
        self._clients: dict[str, dict[str, dict]] = {}
        # {(realm, user_id): [role_name, ...]}
        self._role_mappings: dict[tuple[str, str], list[str]] = {}
        # {realm: [role_name, ...]}
        self._realm_roles: dict[str, list[str]] = {}

    async def get_admin_token(self) -> str:  # noqa: D102
        return "mock-admin-token"

    async def create_realm(self, tenant_slug: str) -> str:  # noqa: D102
        if tenant_slug in self._realms:
            raise KeycloakError(
                f"realm '{tenant_slug}' already exists", status_code=409,
            )
        self._realms[tenant_slug] = {
            "realm": tenant_slug,
            "enabled": True,
            "displayName": f"HarkenIQ - {tenant_slug}",
        }
        self._users[tenant_slug] = {}
        self._clients[tenant_slug] = {}
        self._realm_roles[tenant_slug] = list(DEFAULT_REALM_ROLES)
        return tenant_slug

    async def ensure_realm_roles(self, realm: str) -> list[str]:  # noqa: D102
        if realm not in self._realms:
            raise KeycloakError(f"realm '{realm}' not found", status_code=404)
        existing = set(self._realm_roles.setdefault(realm, []))
        created = [r for r in DEFAULT_REALM_ROLES if r not in existing]
        self._realm_roles[realm] = sorted(existing | set(DEFAULT_REALM_ROLES))
        return created

    async def create_client(
        self,
        realm: str,
        client_id: str,
        redirect_uris: list[str],
    ) -> str:  # noqa: D102
        if realm not in self._realms:
            raise KeycloakError(f"realm '{realm}' not found", status_code=404)
        client_uuid = uuid.uuid4().hex
        self._clients[realm][client_uuid] = {
            "id": client_uuid,
            "clientId": client_id,
            "redirectUris": redirect_uris,
        }
        return client_uuid

    async def create_user(
        self,
        realm: str,
        email: str,
        temporary_password: str = "",
    ) -> str:  # noqa: D102
        if realm not in self._realms:
            raise KeycloakError(f"realm '{realm}' not found", status_code=404)
        # Detect duplicate email.
        for u in self._users[realm].values():
            if u["email"] == email:
                raise KeycloakError(
                    f"user '{email}' already exists", status_code=409,
                )
        user_id = uuid.uuid4().hex
        self._users[realm][user_id] = {
            "id": user_id,
            "username": email,
            "email": email,
            "enabled": True,
        }
        self._role_mappings[(realm, user_id)] = []
        return user_id

    async def assign_realm_role(
        self,
        realm: str,
        user_id: str,
        role_name: str,
    ) -> None:  # noqa: D102
        if realm not in self._realms:
            raise KeycloakError(f"realm '{realm}' not found", status_code=404)
        if user_id not in self._users.get(realm, {}):
            raise KeycloakError(f"user '{user_id}' not found", status_code=404)
        if role_name not in self._realm_roles.get(realm, []):
            raise KeycloakError(f"role '{role_name}' not found", status_code=404)
        key = (realm, user_id)
        if role_name not in self._role_mappings.get(key, []):
            self._role_mappings.setdefault(key, []).append(role_name)

    async def delete_user(self, realm: str, user_id: str) -> None:  # noqa: D102
        if realm not in self._realms:
            raise KeycloakError(f"realm '{realm}' not found", status_code=404)
        if user_id not in self._users.get(realm, {}):
            raise KeycloakError(f"user '{user_id}' not found", status_code=404)
        del self._users[realm][user_id]
        self._role_mappings.pop((realm, user_id), None)

    async def list_realm_users(self, realm: str) -> list[dict]:  # noqa: D102
        if realm not in self._realms:
            raise KeycloakError(f"realm '{realm}' not found", status_code=404)
        return list(self._users[realm].values())

    async def delete_realm(self, realm: str) -> None:  # noqa: D102
        if realm not in self._realms:
            raise KeycloakError(f"realm '{realm}' not found", status_code=404)
        del self._realms[realm]
        self._users.pop(realm, None)
        self._clients.pop(realm, None)
        self._realm_roles.pop(realm, None)
        # Clean up role mappings for this realm.
        to_remove = [k for k in self._role_mappings if k[0] == realm]
        for k in to_remove:
            del self._role_mappings[k]
