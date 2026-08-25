"""Dual-realm Keycloak JWT authentication (QA-005: real validation).

Two modes:
- Platform realm auth (super admin, support staff): realm ==
  ``config.platform_realm``; ``is_platform_user`` true, no tenant.
- Tenant realm auth: any OTHER realm is treated as a tenant slug (Console
  provisions one realm per tenant, realm name == slug). The slug resolves
  to a tenant row; unknown slugs are rejected, so a stray realm on the
  same Keycloak cannot mint access.

Insecure mode (``HARKEN_CONSOLE_INSECURE=true``) returns a mock
platform-admin context for lab/test use — the same behavior as before,
now the ONLY path that bypasses validation. Secure mode validates RS256
signatures against the realm JWKS (see ``harkeniq.security.oidc``);
the pre-QA build raised HTTP 501 here.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from fastapi import HTTPException, Request

from harkeniq.security.oidc import (
    KeycloakTokenValidator,
    TokenValidationError,
    pick_role,
)

logger = logging.getLogger("harkeniq.console.auth")

#: Highest privilege first — the effective role is the best the token holds.
_PLATFORM_ROLES = ["platform_super_admin", "platform_support"]
_TENANT_ROLES = ["tenant_owner", "site_admin", "operator", "auditor", "viewer"]

#: Tenant slug -> id cache (auth runs per-request; the mapping is stable).
_TENANT_CACHE_TTL_S = 60.0


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

_validator: Optional[KeycloakTokenValidator] = None
_tenant_cache: dict[str, tuple[float, str]] = {}


def _get_validator(config) -> KeycloakTokenValidator:
    global _validator  # noqa: PLW0603 — one validator per process
    if _validator is None:
        public = getattr(config, "keycloak_public_url", "") or config.keycloak_url
        _validator = KeycloakTokenValidator(
            internal_base_url=config.keycloak_url,
            public_base_url=public,
            client_id=config.platform_client_id,
            realm_allowed=lambda realm: True,  # tenant realms checked vs DB below
        )
    return _validator


def reset_validator() -> None:
    """Test hook: drop the cached validator and tenant cache."""
    global _validator  # noqa: PLW0603
    _validator = None
    _tenant_cache.clear()


async def _resolve_tenant_id(request: Request, slug: str) -> Optional[str]:
    cached = _tenant_cache.get(slug)
    if cached and (time.time() - cached[0]) < _TENANT_CACHE_TTL_S:
        return cached[1]
    from harkeniq_console.db.repos import TenantRepo

    state = request.app.state.console
    async with state.sessionmaker() as session:
        tenant = await TenantRepo(session).get_by_slug(slug)
    if tenant is None:
        return None
    _tenant_cache[slug] = (time.time(), tenant.id)
    return tenant.id


async def get_current_user(request: Request) -> UserContext:
    """Extract user context from JWT or return mock in insecure mode."""
    config = request.app.state.console.config
    if config.insecure:
        return _INSECURE_CONTEXT

    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")

    token = auth_header[7:]
    try:
        validated = await _get_validator(config).validate(token)
    except TokenValidationError as e:
        logger.info("token rejected: %s", e)
        raise HTTPException(status_code=401, detail="invalid token")
    except Exception:
        logger.exception("token validation errored")
        raise HTTPException(status_code=401, detail="invalid token")

    if validated.realm == config.platform_realm:
        role = pick_role(validated.roles, _PLATFORM_ROLES, default="")
        if not role:
            raise HTTPException(
                status_code=403, detail="no platform role assigned"
            )
        return UserContext(
            user_id=validated.subject,
            email=validated.email,
            tenant_id=None,
            role=role,
            permissions=[],
            is_platform_user=True,
        )

    tenant_id = await _resolve_tenant_id(request, validated.realm)
    if tenant_id is None:
        logger.info("token from unknown tenant realm %r", validated.realm)
        raise HTTPException(status_code=401, detail="invalid token")
    role = pick_role(validated.roles, _TENANT_ROLES, default="viewer")
    return UserContext(
        user_id=validated.subject,
        email=validated.email,
        tenant_id=tenant_id,
        role=role,
        permissions=[],
        is_platform_user=False,
    )
