"""Keycloak JWT validation and FastAPI auth dependencies (QA-005: real).

Secure mode validates RS256 tokens against the configured realm's JWKS via
``harkeniq.security.oidc`` (the pre-QA build raised HTTP 501 here, and
``configure_auth`` was never called by anything). Insecure mode keeps the
lab context — the only bypass, and it must be set explicitly.

CC is single-tenant (one CC per tenant, spec §3): it accepts tokens from
exactly one realm — the platform realm for vendor operators, or the
tenant's own realm — set by ``HARKEN_CC_KEYCLOAK_REALM``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from fastapi import HTTPException, Request

from harkeniq.security.oidc import (
    KeycloakTokenValidator,
    TokenValidationError,
    pick_role,
)

logger = logging.getLogger("harkeniq.cc.auth")

#: Roles CC recognizes, highest privilege first (spec §4 subset relevant
#: to L3 surfaces).
_RANKED_ROLES = [
    "platform_super_admin",
    "tenant_owner",
    "site_admin",
    "operator",
    "auditor",
    "viewer",
]
#: Roles allowed to mutate (approve/deny, policies); everything else views.
_ADMIN_ROLES = {"platform_super_admin", "tenant_owner", "site_admin"}
#: Extra grants for non-admin roles, by role. The auditor's job is the
#: audit trail (spec §4 role 6); operator/viewer hold plain "view" and are
#: refused audit reads — review 2026-08-28: CC's audit routes were gated
#: on authentication alone, so any authenticated viewer could read the
#: audit log through the Console proxy.
_EXTRA_PERMISSIONS = {"auditor": ["audit.view"]}


@dataclass
class UserContext:
    user_id: str
    email: str
    tenant_id: str
    role: str
    permissions: list[str] = field(default_factory=list)
    is_platform_user: bool = False


# Module-level auth state; set by configure_auth at app startup.
_validator: Optional[KeycloakTokenValidator] = None
_insecure: bool = False
_realm: str = ""


def configure_auth(
    keycloak_url: str,
    realm: str,
    client_id: str,
    insecure: bool = False,
    keycloak_public_url: str = "",
) -> None:
    """Called once at app startup to configure auth."""
    global _validator, _insecure, _realm  # noqa: PLW0603
    _insecure = insecure
    _realm = realm
    if insecure:
        _validator = None
        return
    _validator = KeycloakTokenValidator(
        internal_base_url=keycloak_url,
        public_base_url=keycloak_public_url or keycloak_url,
        client_id=client_id,
        realm_allowed=lambda r: r == realm,
    )


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
    if _validator is None:
        # configure_auth was never called — fail closed, loudly.
        raise HTTPException(status_code=500, detail="auth not configured")

    try:
        validated = await _validator.validate(token)
    except TokenValidationError as e:
        logger.info("token rejected: %s", e)
        raise HTTPException(status_code=401, detail="invalid token")
    except Exception:
        logger.exception("token validation errored")
        raise HTTPException(status_code=401, detail="invalid token")

    role = pick_role(validated.roles, _RANKED_ROLES, default="viewer")
    return UserContext(
        user_id=validated.subject,
        email=validated.email,
        tenant_id=request.app.state.cc.config.tenant_id,
        role=role,
        permissions=(
            ["*"] if role in _ADMIN_ROLES
            else ["view", *_EXTRA_PERMISSIONS.get(role, [])]
        ),
        is_platform_user=role == "platform_super_admin",
    )
