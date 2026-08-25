"""QA-005: Keycloak token validation — real signatures, every refusal branch.

The R2b auth stubs shipped with secure mode returning HTTP 501; these are
the tests that would have refused to let that land. Tokens here are signed
with a real RSA key and validated through the same code path production
uses; only the JWKS fetch is stubbed.
"""

import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwk, jwt

from harkeniq.security.oidc import (
    KeycloakTokenValidator,
    TokenValidationError,
    pick_role,
)

PUBLIC_BASE = "http://localhost:8180"
INTERNAL_BASE = "http://keycloak:8080"
REALM = "harkeniq-platform"
CLIENT = "harkeniq-console"


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    jwks_key = jwk.construct(public_pem, algorithm="RS256").to_dict()
    jwks_key["kid"] = "test-kid"
    jwks_key["use"] = "sig"
    return private_pem, {"keys": [jwks_key]}


def sign(private_pem, *, realm=REALM, azp=CLIENT, kid="test-kid",
         exp_delta=300, roles=("platform_super_admin",), **extra):
    claims = {
        "iss": f"{PUBLIC_BASE}/realms/{realm}",
        "sub": "user-1",
        "email": "admin@harkeniq.com",
        "azp": azp,
        "exp": int(time.time()) + exp_delta,
        "iat": int(time.time()) - 5,
        "realm_access": {"roles": list(roles)},
    }
    claims.update(extra)
    return jwt.encode(
        claims, private_pem, algorithm="RS256", headers={"kid": kid}
    )


def make_validator(jwks, realm_allowed=lambda r: True):
    validator = KeycloakTokenValidator(
        internal_base_url=INTERNAL_BASE,
        public_base_url=PUBLIC_BASE,
        client_id=CLIENT,
        realm_allowed=realm_allowed,
    )
    fetches = []

    async def fake_fetch(realm):
        fetches.append(realm)
        validator._jwks[realm] = (time.time(), jwks)
        return jwks

    validator._fetch_jwks = fake_fetch
    validator._fetches = fetches
    return validator


class TestValidation:
    @pytest.mark.asyncio
    async def test_valid_token_accepted_with_roles(self, keypair):
        private_pem, jwks = keypair
        validator = make_validator(jwks)
        result = await validator.validate(sign(private_pem))
        assert result.realm == REALM
        assert result.email == "admin@harkeniq.com"
        assert "platform_super_admin" in result.roles

    @pytest.mark.asyncio
    async def test_realm_roles_mapper_claim_merged(self, keypair):
        private_pem, jwks = keypair
        validator = make_validator(jwks)
        token = sign(private_pem, roles=(), realm_roles=["platform_support"])
        result = await validator.validate(token)
        assert "platform_support" in result.roles

    @pytest.mark.asyncio
    async def test_expired_token_rejected(self, keypair):
        private_pem, jwks = keypair
        validator = make_validator(jwks)
        with pytest.raises(TokenValidationError):
            await validator.validate(sign(private_pem, exp_delta=-60))

    @pytest.mark.asyncio
    async def test_wrong_issuer_base_rejected(self, keypair):
        private_pem, jwks = keypair
        validator = make_validator(jwks)
        token = jwt.encode(
            {"iss": "http://evil.example/realms/harkeniq-platform",
             "exp": int(time.time()) + 300, "azp": CLIENT},
            private_pem, algorithm="RS256", headers={"kid": "test-kid"},
        )
        with pytest.raises(TokenValidationError, match="issuer"):
            await validator.validate(token)

    @pytest.mark.asyncio
    async def test_disallowed_realm_rejected_before_signature_work(self, keypair):
        private_pem, jwks = keypair
        validator = make_validator(jwks, realm_allowed=lambda r: r == "other")
        with pytest.raises(TokenValidationError, match="not allowed"):
            await validator.validate(sign(private_pem))
        assert validator._fetches == []  # refused before any JWKS fetch

    @pytest.mark.asyncio
    async def test_wrong_client_azp_rejected(self, keypair):
        private_pem, jwks = keypair
        validator = make_validator(jwks)
        with pytest.raises(TokenValidationError, match="issued to"):
            await validator.validate(sign(private_pem, azp="other-client"))

    @pytest.mark.asyncio
    async def test_forged_signature_rejected(self, keypair):
        _, jwks = keypair
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        other_pem = other.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        validator = make_validator(jwks)
        with pytest.raises(TokenValidationError, match="rejected"):
            await validator.validate(sign(other_pem))

    @pytest.mark.asyncio
    async def test_unknown_kid_forces_one_refresh_then_fails(self, keypair):
        private_pem, jwks = keypair
        validator = make_validator(jwks)
        with pytest.raises(TokenValidationError, match="unknown signing key"):
            await validator.validate(sign(private_pem, kid="rotated-away"))
        # Cache was cold: initial fetch + rotation retry.
        assert len(validator._fetches) == 2

    @pytest.mark.asyncio
    async def test_garbage_token_rejected(self, keypair):
        _, jwks = keypair
        validator = make_validator(jwks)
        with pytest.raises(TokenValidationError, match="malformed"):
            await validator.validate("not-a-jwt")


class TestPickRole:
    def test_highest_privilege_wins(self):
        assert pick_role(
            ["viewer", "site_admin"],
            ["tenant_owner", "site_admin", "viewer"],
            default="viewer",
        ) == "site_admin"

    def test_default_when_no_known_role(self):
        assert pick_role(["custom-x"], ["tenant_owner"], default="viewer") == "viewer"
