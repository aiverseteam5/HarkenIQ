"""QA-019 (second half): CC-side license load + verify.

The roundtrip tests sign with the CONSOLE's licensing module and verify
with CC's — locking the two implementations to the same token format.
"""

from __future__ import annotations

import time

import pytest

from harkeniq_cc.config import CCConfig
from harkeniq_cc.license import (
    LicenseError,
    LicenseInfo,
    load_license,
    verify_license_token,
)
from harkeniq_console.licensing import (
    build_license_payload,
    generate_keypair,
    sign_license,
)

TENANT = "tenant-x"


@pytest.fixture(scope="module")
def keypair():
    return generate_keypair()  # (private_pem, public_pem)


def _token(keypair, tenant=TENANT, valid_months=12):
    payload = build_license_payload(
        tenant_id=tenant, plan="enterprise", node_commit=100,
        valid_months=valid_months,
    )
    return sign_license(keypair[0], payload)


class TestVerifyToken:
    def test_console_signed_token_verifies(self, keypair):
        payload = verify_license_token(keypair[1], _token(keypair))
        assert payload["sub"] == TENANT
        assert payload["plan"] == "enterprise"
        assert payload["fingerprint"]

    def test_tampered_payload_refused(self, keypair):
        token = _token(keypair)
        import base64, json
        raw, sig = token.split(".")
        pad = 4 - len(raw) % 4
        payload = json.loads(base64.urlsafe_b64decode(raw + "=" * (pad % 4)))
        payload["node_commit"] = 100000
        forged = base64.urlsafe_b64encode(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).rstrip(b"=").decode()
        with pytest.raises(LicenseError, match="signature"):
            verify_license_token(keypair[1], f"{forged}.{sig}")

    def test_wrong_key_refused(self, keypair):
        other = generate_keypair()
        with pytest.raises(LicenseError, match="signature"):
            verify_license_token(other[1], _token(keypair))

    def test_garbage_refused(self, keypair):
        with pytest.raises(LicenseError, match="format"):
            verify_license_token(keypair[1], "not-a-license")


class TestLoadLicense:
    def _config(self, tmp_path, keypair, token, tenant=TENANT, **overrides):
        lic = tmp_path / "cc.lic"
        lic.write_text(token)
        key = tmp_path / "verify.pem"
        key.write_bytes(keypair[1])
        defaults = dict(
            tenant_id=tenant,
            license_key_path=str(lic),
            license_verify_key_path=str(key),
        )
        defaults.update(overrides)
        return CCConfig(**defaults)

    def test_valid_license_loads(self, tmp_path, keypair):
        config = self._config(tmp_path, keypair, _token(keypair))
        info = load_license(config)
        assert isinstance(info, LicenseInfo)
        assert info.status == "verified"
        assert info.payload["sub"] == TENANT

    def test_unconfigured_returns_none(self):
        assert load_license(CCConfig(tenant_id=TENANT, insecure=True)) is None
        # Secure mode also starts (loud warning), the lab-compose posture
        assert load_license(CCConfig(tenant_id=TENANT)) is None

    def test_missing_verify_key_fails_closed(self, tmp_path, keypair):
        config = self._config(
            tmp_path, keypair, _token(keypair), license_verify_key_path="",
        )
        with pytest.raises(LicenseError, match="verify_key_path"):
            load_license(config)

    def test_tenant_mismatch_refused(self, tmp_path, keypair):
        config = self._config(
            tmp_path, keypair, _token(keypair, tenant="someone-else"),
        )
        with pytest.raises(LicenseError, match="tenant"):
            load_license(config)

    def test_expired_license_grace_posture(self, tmp_path, keypair, caplog):
        """Expired-but-authentic runs in grace posture (delinquency is
        Console-enforced; R-H7 forbids auto-disabling on-prem infra)."""
        payload = build_license_payload(
            tenant_id=TENANT, plan="enterprise", node_commit=100,
            valid_months=1,
        )
        payload["exp"] = time.time() - 3600
        token = sign_license(keypair[0], payload)
        config = self._config(tmp_path, keypair, token)
        with caplog.at_level("ERROR"):
            info = load_license(config)
        assert info is not None
        assert info.status == "expired"
        assert "grace" in caplog.text

    def test_unreadable_file_refused(self, tmp_path, keypair):
        key = tmp_path / "verify.pem"
        key.write_bytes(keypair[1])
        config = CCConfig(
            tenant_id=TENANT,
            license_key_path=str(tmp_path / "missing.lic"),
            license_verify_key_path=str(key),
        )
        with pytest.raises(LicenseError, match="cannot read license file"):
            load_license(config)


class TestSiteRegistrationUsesLicense:
    @pytest.fixture
    async def env(self, keypair):
        from httpx import ASGITransport, AsyncClient
        from harkeniq_cc.app import create_app
        from harkeniq_cc.auth import configure_auth
        from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
        from harkeniq_cc.runtime import AppState

        config = CCConfig(tenant_id=TENANT, insecure=True)
        configure_auth("", "", "", insecure=True)
        engine = make_engine("sqlite+aiosqlite:///:memory:")
        await create_all(engine)
        state = AppState(
            config=config, engine=engine,
            sessionmaker=make_sessionmaker(engine),
        )
        token = _token(keypair)
        payload = verify_license_token(keypair[1], token)
        state.license = LicenseInfo(
            payload=payload, fingerprint=payload["fingerprint"],
            status="verified",
        )
        app = create_app(state)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://cc") as c:
            yield c, state
        await engine.dispose()

    async def test_license_fingerprint_overrides_body(self, env):
        client, state = env
        # RegisterSite RPC will fail (no SM) but the site row records the
        # LICENSE fingerprint, not the caller's string-free default.
        resp = await client.post("/api/sites/register", json={
            "site_name": "site-1", "sm_endpoint": "127.0.0.1:1",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert body["site"]["license_fingerprint"] == state.license.fingerprint

    async def test_mismatched_body_fingerprint_rejected(self, env):
        client, _ = env
        resp = await client.post("/api/sites/register", json={
            "site_name": "site-2", "sm_endpoint": "127.0.0.1:1",
            "license_fingerprint": "hand-typed-wrong",
        })
        assert resp.status_code == 400
        assert "does not match" in resp.json()["detail"]
