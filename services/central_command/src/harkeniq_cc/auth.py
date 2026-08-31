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

#: Role -> atomic permission grants, mirroring the Console's
#: ROLE_PERMISSIONS (harkeniq_console/permissions.py) for the shared
#: tenant roles. P0 2026-08-29 (final-assessment C1): the previous model
#: granted only "*" (admins) or the literal string "view", so the
#: fleet.view / action.approve / site.manage guards on every route were
#: satisfiable by nobody below site_admin — operators could not approve
#: and viewers could not view, contradicting spec §4 and R-C4. One
#: vocabulary now serves both services; tests pin parity with the
#: Console's map so they cannot drift apart silently.
#:
#: platform_support is deliberately ABSENT: vendor staff have no live L3
#: access by default (A12.1) — CC's realm pinning keeps them out in real
#: deployments, and in the single-realm demo an unlisted role falls
#: through pick_role to "viewer" rather than gaining staff powers here.
ROLE_PERMISSIONS: dict[str, list[str]] = {
    "platform_super_admin": ["*"],
    "tenant_owner": [
        "tenant.view", "user.manage", "user.view", "role.manage",
        "site.manage", "site.view", "fleet.view", "action.approve",
        "incident.view", "incident.acknowledge", "billing.manage",
        "billing.view", "license.view", "support.create", "support.view",
        "audit.view", "audit.export", "skill.submit", "skill.install",
    ],
    "site_admin": [
        "site.manage", "site.view", "fleet.view", "action.approve",
        "incident.view", "incident.acknowledge", "user.view",
    ],
    "operator": [
        "fleet.view", "action.approve", "incident.view",
        "incident.acknowledge", "support.create", "support.view",
        "skill.submit",
    ],
    # A13 (OQ-24): read-only everything + audit.export, nothing else.
    "auditor": [
        "fleet.view", "incident.view", "billing.view", "audit.view",
        "audit.export", "user.view", "site.view", "license.view",
        "support.view",
    ],
    "viewer": ["fleet.view", "incident.view"],
}


@dataclass
class UserContext:
    user_id: str
    email: str
    tenant_id: str
    role: str
    permissions: list[str] = field(default_factory=list)
    is_platform_user: bool = False
    #: A3 (spec A20): "user" for a human, "agent" for an authenticated
    #: Operational Agent. ONE context type carries both deliberately -- a
    #: separate machine context would be a second authorization model by
    #: the back door, and every guard downstream would have to learn
    #: about it. What differs is `permissions` (capped by the A20.3
    #: ceiling) and how scope resolves, not the shape of the principal.
    species: str = "user"
    #: For a machine principal: the identity row that authenticated it.
    #: Carried so the scope dependency can resolve agent grants without
    #: a second lookup, and so audit can name the identity.
    identity_id: str = ""


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
    from harkeniq_cc.machine_identity import is_machine_client_id

    _validator = KeycloakTokenValidator(
        internal_base_url=keycloak_url,
        public_base_url=keycloak_public_url or keycloak_url,
        client_id=client_id,
        realm_allowed=lambda r: r == realm,
        # A3: a machine identity presents its OWN service-account client,
        # so `azp` will not be the console client. This admits the SHAPE;
        # the identity row decides whether it authenticates at all, and
        # the A20.3 ceiling decides what it may then do.
        client_allowed=is_machine_client_id,
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

    # A3: a machine identity presents its own service-account client, so
    # the SHAPE of `azp` decides which resolution path runs. Neither path
    # is a second authorization model: both produce one `UserContext`
    # that every downstream guard reads identically.
    from harkeniq_cc.machine_identity import is_machine_client_id

    if is_machine_client_id(validated.client_id):
        return await _machine_principal(request, validated)

    role = pick_role(validated.roles, _RANKED_ROLES, default="viewer")
    return UserContext(
        user_id=validated.subject,
        email=validated.email,
        tenant_id=request.app.state.cc.config.tenant_id,
        role=role,
        permissions=list(ROLE_PERMISSIONS.get(role, ROLE_PERMISSIONS["viewer"])),
        is_platform_user=role == "platform_super_admin",
        species="user",
    )


async def _machine_principal(request: Request, validated) -> UserContext:
    """Resolve an authenticated Operational Agent (A3, spec A20).

    Central Command's identity row is authoritative over the token. A
    valid, unexpired, correctly-signed JWT is refused the moment the row
    says revoked or retired -- which is what makes revocation immediate
    rather than waiting out a 300-second token lifetime.

    The permissions attached here are the A20.3 intersection, never a
    role and never `["*"]`. Resolved the way agents are in-process, an
    authenticated agent would satisfy every route guard in the platform,
    including approving its own proposals.
    """
    from harkeniq_cc.db.repos import AgentIdentityRepo, AuditRepo, OperationalAgentRepo
    from harkeniq_cc.machine_identity import (
        SPECIES_AGENT,
        authenticate,
        machine_permissions,
    )
    from harkeniq_cc.operational_agent import attribution_key, bound_reads

    state = request.app.state.cc
    tenant_id = state.config.tenant_id
    realm = getattr(state.config, "keycloak_realm", "") or ""

    async with state.sessionmaker() as session:
        identities = AgentIdentityRepo(session)
        identity = await identities.get_by_subject(realm, validated.subject)
        agent = None
        if identity is not None:
            agent = await OperationalAgentRepo(session).get(
                identity.tenant_id, identity.agent_id,
            )

        allowed, reason = authenticate(
            identity, agent, tenant_id=tenant_id, realm=realm,
        )
        if not allowed:
            # Audited: a refused machine credential is a security event,
            # and "the agent went quiet" is otherwise indistinguishable
            # from "the agent had nothing to do".
            if identity is not None:
                await AuditRepo(session).append(
                    actor=f"machine:{validated.subject}",
                    action="agent_identity.auth_failed",
                    subject=identity.agent_id,
                    tenant_id=identity.tenant_id or tenant_id,
                    detail={"reason": reason,
                            "client_id": validated.client_id},
                )
                await session.commit()
            logger.info("machine token rejected: %s", reason)
            raise HTTPException(status_code=401, detail="invalid token")

        caps = await OperationalAgentRepo(session).list_capabilities(agent.id)
        # A20.3: the INTERSECTION. Never a role, never ["*"].
        permissions = machine_permissions(bound_reads(caps))
        await identities.touch(
            identity, request.headers.get("user-agent", "")[:255],
        )
        await session.commit()

        return UserContext(
            # The AGENT id, not the Keycloak subject: `cc_scope_grants`
            # rows for an agent are keyed on the agent id, so this is
            # what makes the ONE scope resolver work unchanged.
            user_id=identity.agent_id,
            email=attribution_key(identity.agent_id, agent.version),
            tenant_id=identity.tenant_id,
            role="",  # A machine principal holds no ROLE. It has a ceiling.
            permissions=permissions,
            is_platform_user=False,
            species=SPECIES_AGENT,
            identity_id=identity.id,
        )
