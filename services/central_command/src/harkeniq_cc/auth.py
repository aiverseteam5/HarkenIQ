"""Keycloak JWT validation and FastAPI auth dependencies."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from fastapi import HTTPException, Request

logger = logging.getLogger("harkeniq.cc.auth")


@dataclass
class UserContext:
    user_id: str
    email: str
    tenant_id: str
    role: str
    permissions: list[str] = field(default_factory=list)
    is_platform_user: bool = False


class KeycloakAuth:
    """Validate JWTs against a Keycloak realm's JWKS endpoint."""

    def __init__(self, keycloak_url: str, realm: str, client_id: str) -> None:
        self.keycloak_url = keycloak_url.rstrip("/")
        self.realm = realm
        self.client_id = client_id
        self._jwks_cache: Optional[dict] = None

    async def verify_token(self, token: str) -> dict:
        """Validate JWT against Keycloak JWKS endpoint, cache JWKS.

        Returns the decoded token claims. Full implementation pending
        Keycloak integration in R2b Phase 2.
        """
        # TODO: fetch JWKS from
        # {keycloak_url}/realms/{realm}/protocol/openid-connect/certs
        # and validate using python-jose. Stub raises until Keycloak is wired.
        raise NotImplementedError(
            "Keycloak JWT validation not yet implemented; use insecure mode for lab"
        )


# Module-level auth instance; set by app startup.
_auth: Optional[KeycloakAuth] = None
_insecure: bool = False


def configure_auth(keycloak_url: str, realm: str, client_id: str, insecure: bool = False) -> None:
    """Called once at app startup to configure auth."""
    global _auth, _insecure  # noqa: PLW0603
    _insecure = insecure
    if not insecure:
        _auth = KeycloakAuth(keycloak_url, realm, client_id)


async def get_current_user(request: Request) -> UserContext:
    """FastAPI dependency: extract Bearer token, validate, return UserContext."""
    if _insecure:
        return UserContext(
            user_id="lab-user",
            email="lab@harkeniq.local",
            tenant_id=request.app.state.cc.config.tenant_id or "lab-tenant",
            role="admin",
            permissions=["*"],
            is_platform_user=False,
        )

    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing Bearer token")

    token = auth_header[7:]
    if _auth is None:
        raise HTTPException(status_code=500, detail="auth not configured")

    try:
        claims = await _auth.verify_token(token)
    except NotImplementedError:
        raise HTTPException(
            status_code=501, detail="Keycloak auth not yet implemented"
        )
    except Exception:
        logger.exception("JWT validation failed")
        raise HTTPException(status_code=401, detail="invalid token")

    return UserContext(
        user_id=claims.get("sub", ""),
        email=claims.get("email", ""),
        tenant_id=claims.get("tenant_id", ""),
        role=claims.get("realm_access", {}).get("roles", ["viewer"])[0],
        permissions=claims.get("permissions", []),
        is_platform_user=claims.get("is_platform_user", False),
    )
