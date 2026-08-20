"""Keycloak administration client.

All methods are stubs — will be implemented in Phase 2 when the
Keycloak integration lands. Each method documents the intended
behaviour so the API layer can be wired ahead of time.
"""

from __future__ import annotations


class KeycloakAdminClient:
    """Wraps python-keycloak for realm/user/role management."""

    def __init__(self, url: str, admin_user: str, admin_password: str) -> None:
        self.url = url
        self.admin_user = admin_user
        self.admin_password = admin_password

    async def create_realm(self, tenant_slug: str) -> str:
        """Create a Keycloak realm for a new tenant. Returns realm name."""
        raise NotImplementedError("Keycloak create_realm — Phase 2")

    async def create_client(
        self, realm: str, client_id: str, redirect_uris: list[str]
    ) -> str:
        """Register an OIDC client in a tenant realm. Returns client UUID."""
        raise NotImplementedError("Keycloak create_client — Phase 2")

    async def create_user(
        self, realm: str, email: str, temporary_password: str
    ) -> str:
        """Create a user in a realm. Returns Keycloak user ID."""
        raise NotImplementedError("Keycloak create_user — Phase 2")

    async def assign_realm_role(
        self, realm: str, user_id: str, role_name: str
    ) -> None:
        """Assign a realm-level role to a user."""
        raise NotImplementedError("Keycloak assign_realm_role — Phase 2")

    async def delete_user(self, realm: str, user_id: str) -> None:
        """Remove a user from a realm."""
        raise NotImplementedError("Keycloak delete_user — Phase 2")

    async def list_realm_users(self, realm: str) -> list:
        """List all users in a realm."""
        raise NotImplementedError("Keycloak list_realm_users — Phase 2")
