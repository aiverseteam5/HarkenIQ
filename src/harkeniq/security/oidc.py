"""Keycloak OIDC token validation shared by Console and CC (QA-005).

Replaces the R2b auth stubs (secure mode raised HTTP 501 in both services;
insecure mode granted every caller platform_super_admin). Validation is
real: RS256 signature against the realm's JWKS, issuer, expiry, and
authorized-party checks.

Design notes:
- Multi-realm: the Console authenticates users from the platform realm AND
  every tenant realm (realm name == tenant slug). The realm is taken from
  the token's ``iss`` and checked against an allow-policy before any
  signature work.
- Split URLs: browsers reach Keycloak at a public address (issuer base,
  e.g. ``http://localhost:8180``) while services fetch JWKS over the
  compose network (internal base, e.g. ``http://keycloak:8080``). Tokens
  carry the PUBLIC issuer; JWKS is fetched via the INTERNAL base.
- JWKS is cached per realm with a TTL; an unknown ``kid`` forces one
  refresh (key rotation) before failing.
- Lazy imports: ``python-jose`` and ``httpx`` are dependencies of both
  service packages, not of the base agent package. The agent never imports
  this module.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional


class TokenValidationError(Exception):
    """Token failed validation; the message is safe to log, not to leak."""


@dataclass
class ValidatedToken:
    """Outcome of a successful validation."""

    realm: str
    claims: dict
    subject: str
    email: str
    roles: list[str] = field(default_factory=list)


class KeycloakTokenValidator:
    """Validate Keycloak-issued JWTs against per-realm JWKS."""

    def __init__(
        self,
        internal_base_url: str,
        public_base_url: str,
        client_id: str,
        realm_allowed: Callable[[str], bool],
        jwks_ttl_s: float = 300.0,
    ) -> None:
        self.internal_base_url = internal_base_url.rstrip("/")
        self.public_base_url = public_base_url.rstrip("/")
        self.client_id = client_id
        self.realm_allowed = realm_allowed
        self.jwks_ttl_s = jwks_ttl_s
        # realm -> (fetched_at, jwks dict)
        self._jwks: dict[str, tuple[float, dict]] = {}

    # -- JWKS ---------------------------------------------------------------

    async def _fetch_jwks(self, realm: str) -> dict:
        import httpx

        url = (
            f"{self.internal_base_url}/realms/{realm}"
            "/protocol/openid-connect/certs"
        )
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            jwks = response.json()
        self._jwks[realm] = (time.time(), jwks)
        return jwks

    async def _jwks_for(self, realm: str, kid: str) -> dict:
        cached = self._jwks.get(realm)
        jwks: Optional[dict] = None
        if cached and (time.time() - cached[0]) < self.jwks_ttl_s:
            jwks = cached[1]
        if jwks is None:
            jwks = await self._fetch_jwks(realm)
        key = _key_by_kid(jwks, kid)
        if key is None:
            # Key rotation: one forced refresh before giving up.
            jwks = await self._fetch_jwks(realm)
            key = _key_by_kid(jwks, kid)
        if key is None:
            raise TokenValidationError(f"unknown signing key {kid!r}")
        return key

    # -- validation ---------------------------------------------------------

    async def validate(self, token: str) -> ValidatedToken:
        from jose import JWTError, jwt

        try:
            header = jwt.get_unverified_header(token)
            unverified = jwt.get_unverified_claims(token)
        except JWTError as e:
            raise TokenValidationError(f"malformed token: {e}") from e

        issuer = str(unverified.get("iss", ""))
        realm_prefix = f"{self.public_base_url}/realms/"
        if not issuer.startswith(realm_prefix):
            raise TokenValidationError(f"unexpected issuer {issuer!r}")
        realm = issuer[len(realm_prefix):]
        if not realm or "/" in realm:
            raise TokenValidationError(f"unparseable realm in issuer {issuer!r}")
        if not self.realm_allowed(realm):
            raise TokenValidationError(f"realm {realm!r} not allowed")

        key = await self._jwks_for(realm, str(header.get("kid", "")))
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=issuer,
                # Keycloak access tokens default aud to "account"; the
                # binding claim for a public client is azp, checked below.
                options={"verify_aud": False},
            )
        except JWTError as e:
            raise TokenValidationError(f"token rejected: {e}") from e

        azp = claims.get("azp") or claims.get("client_id")
        if azp != self.client_id:
            raise TokenValidationError(
                f"token issued to {azp!r}, expected {self.client_id!r}"
            )

        roles = list(claims.get("realm_access", {}).get("roles", []))
        # The realm-role protocol mapper (shipped realm JSON) also exposes
        # a flat multivalued claim.
        extra = claims.get("realm_roles")
        if isinstance(extra, list):
            roles.extend(r for r in extra if r not in roles)
        elif isinstance(extra, str) and extra not in roles:
            roles.append(extra)

        return ValidatedToken(
            realm=realm,
            claims=claims,
            subject=str(claims.get("sub", "")),
            email=str(claims.get("email", claims.get("preferred_username", ""))),
            roles=roles,
        )


def _key_by_kid(jwks: dict, kid: str) -> Optional[dict]:
    for key in jwks.get("keys", []):
        if key.get("kid") == kid:
            return key
    return None


def pick_role(roles: list[str], ranked: list[str], default: str) -> str:
    """Highest-privilege known role from a token's role list."""
    for candidate in ranked:
        if candidate in roles:
            return candidate
    return default
