"""Dual-realm Keycloak JWT authentication.

Two modes:
- Platform realm auth (super admin, support staff)
- Tenant realm auth (tenant_id derived from JWT realm claim)

Insecure mode returns a mock context for lab/test use.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from fastapi import HTTPException, Request


@dataclass
class UserContext:
    """Authenticated user identity extracted from JWT."""

    user_id: str
    email: str
    tenant_id: Optional[str]
    role: str
    permissions: list[str] = field(default_factory=list)
    is_platform_user: bool = False


_INSECURE_CONTEXT = UserContext(
    user_id="insecure-dev",
    email="dev@harkeniq.local",
    tenant_id=None,
    role="platform_super_admin",
    permissions=[],
    is_platform_user=True,
)


# JWKS cache — populated lazily on first request per realm.
_jwks_cache: dict[str, dict] = {}


async def get_current_user(request: Request) -> UserContext:
    """Extract user context from JWT or return mock in insecure mode."""
    config = request.app.state.console.config
    if config.insecure:
        return _INSECURE_CONTEXT

    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")

    token = auth_header[7:]

    # Phase 2: decode JWT, validate against Keycloak JWKS, extract claims.
    # For now, reject all tokens in secure mode until Keycloak integration
    # is complete.
    raise HTTPException(
        status_code=501,
        detail="JWT validation not yet implemented; use insecure mode for lab",
    )
