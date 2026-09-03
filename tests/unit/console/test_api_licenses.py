"""License API endpoint tests via httpx.AsyncClient."""

import time

import pytest
import httpx

from harkeniq_console.app import create_app
from harkeniq_console.config import ConsoleConfig
from harkeniq_console.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_console.licensing import build_license_payload, generate_keypair, sign_license
from harkeniq_console.runtime import AppState


@pytest.fixture
async def client():
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sm = make_sessionmaker(engine)
    config = ConsoleConfig(insecure=True)
    state = AppState(config=config, engine=engine, sessionmaker=sm)
    app = create_app(state)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as c:
        yield c
    await engine.dispose()


@pytest.fixture
async def tenant_id(client):
    """Create a tenant and return its ID."""
    resp = await client.post(
        "/api/admin/tenants/",
        json={
            "name": "Acme Corp",
            "slug": "acme",
            "billing_country": "US",
            # A23-5: a tenant is born strict, so it is born with an
            # administrator or not at all (A23.14 D3).
            "admin_email": "owner@acme.com",
        },
    )
    assert resp.status_code == 200
    return resp.json()["id"]


class TestIssueLicense:
    async def test_issue_license_201(self, client, tenant_id):
        resp = await client.post(
            f"/api/tenants/{tenant_id}/licenses/",
            json={"plan": "approve", "node_commit": 100, "valid_months": 12},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["tenant_id"] == tenant_id
        assert data["plan"] == "approve"
        assert data["node_commit"] == 100
        assert data["fingerprint"]
        assert data["status"] == "active"

    async def test_issue_license_tenant_not_found(self, client):
        resp = await client.post(
            "/api/tenants/nonexistent/licenses/",
            json={"plan": "approve", "node_commit": 100, "valid_months": 12},
        )
        assert resp.status_code == 404


class TestListLicenses:
    async def test_list_licenses_empty(self, client, tenant_id):
        resp = await client.get(f"/api/tenants/{tenant_id}/licenses/")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0

    async def test_list_licenses_after_issue(self, client, tenant_id):
        await client.post(
            f"/api/tenants/{tenant_id}/licenses/",
            json={"plan": "approve", "node_commit": 100, "valid_months": 12},
        )
        resp = await client.get(f"/api/tenants/{tenant_id}/licenses/")
        data = resp.json()
        assert data["total"] == 1
        assert len(data["items"]) == 1
        assert data["items"][0]["plan"] == "approve"

    async def test_list_licenses_filter_status(self, client, tenant_id):
        # Issue two licenses
        r1 = await client.post(
            f"/api/tenants/{tenant_id}/licenses/",
            json={"plan": "approve", "node_commit": 100, "valid_months": 12},
        )
        await client.post(
            f"/api/tenants/{tenant_id}/licenses/",
            json={"plan": "observe", "node_commit": 10, "valid_months": 6},
        )
        lic_id = r1.json()["id"]
        # Revoke the first
        await client.post(
            f"/api/tenants/{tenant_id}/licenses/{lic_id}/revoke",
            json={"reason": "testing"},
        )
        # Filter
        resp_active = await client.get(
            f"/api/tenants/{tenant_id}/licenses/?status=active",
        )
        resp_revoked = await client.get(
            f"/api/tenants/{tenant_id}/licenses/?status=revoked",
        )
        assert resp_active.json()["total"] == 1
        assert resp_revoked.json()["total"] == 1


class TestGetLicense:
    async def test_get_license_detail(self, client, tenant_id):
        r = await client.post(
            f"/api/tenants/{tenant_id}/licenses/",
            json={"plan": "enterprise", "node_commit": 500, "valid_months": 24},
        )
        lic_id = r.json()["id"]
        resp = await client.get(
            f"/api/tenants/{tenant_id}/licenses/{lic_id}",
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["plan"] == "enterprise"
        assert data["node_commit"] == 500
        assert data["valid_from"] is not None
        assert data["valid_until"] is not None

    async def test_get_license_not_found_404(self, client, tenant_id):
        resp = await client.get(
            f"/api/tenants/{tenant_id}/licenses/bad-id",
        )
        assert resp.status_code == 404


class TestDownloadLicense:
    async def test_download_license(self, client, tenant_id):
        r = await client.post(
            f"/api/tenants/{tenant_id}/licenses/",
            json={"plan": "approve", "node_commit": 100, "valid_months": 12},
        )
        lic_id = r.json()["id"]
        resp = await client.get(
            f"/api/tenants/{tenant_id}/licenses/{lic_id}/download",
        )
        assert resp.status_code == 200
        assert "application/octet-stream" in resp.headers["content-type"]
        assert "attachment" in resp.headers.get("content-disposition", "")
        # Content is the signed license key
        content = resp.text
        assert "." in content  # Two base64url parts separated by dot

    async def test_download_license_not_found(self, client, tenant_id):
        resp = await client.get(
            f"/api/tenants/{tenant_id}/licenses/bad-id/download",
        )
        assert resp.status_code == 404


class TestRevokeLicense:
    async def test_revoke_license(self, client, tenant_id):
        r = await client.post(
            f"/api/tenants/{tenant_id}/licenses/",
            json={"plan": "approve", "node_commit": 100, "valid_months": 12},
        )
        lic_id = r.json()["id"]
        resp = await client.post(
            f"/api/tenants/{tenant_id}/licenses/{lic_id}/revoke",
            json={"reason": "policy change"},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "revoked"
        assert resp.json()["revoke_reason"] == "policy change"

    async def test_revoke_already_revoked_409(self, client, tenant_id):
        r = await client.post(
            f"/api/tenants/{tenant_id}/licenses/",
            json={"plan": "approve", "node_commit": 100, "valid_months": 12},
        )
        lic_id = r.json()["id"]
        await client.post(
            f"/api/tenants/{tenant_id}/licenses/{lic_id}/revoke",
            json={"reason": "first"},
        )
        resp = await client.post(
            f"/api/tenants/{tenant_id}/licenses/{lic_id}/revoke",
            json={"reason": "second"},
        )
        assert resp.status_code == 409


class TestValidateLicense:
    """P0 2026-08-29: /api/licenses/validate verifies the Ed25519
    signature against the deployment's signing key (it used to check only
    ``exp``, so any self-minted payload validated). Tokens must be signed
    with the DEPLOYMENT key now — a fresh keypair is the forgery case."""

    @pytest.fixture
    async def env(self):
        engine = make_engine("sqlite+aiosqlite:///:memory:")
        await create_all(engine)
        sm = make_sessionmaker(engine)
        config = ConsoleConfig(insecure=True)
        state = AppState(config=config, engine=engine, sessionmaker=sm)
        app = create_app(state)
        # Seed the deployment signing keypair the way the insecure-mode
        # issue path stores it.
        from harkeniq_console.db.repos import SettingsRepo

        priv, pub = generate_keypair()
        async with sm() as session:
            await SettingsRepo(session).set(
                "license_signing_keypair",
                {"private_key": priv.decode(), "public_key": pub.decode()},
                updated_by="test",
            )
            await session.commit()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as c:
            yield c, priv
        await engine.dispose()

    async def test_validate_license_valid(self, env):
        client, priv = env
        payload = build_license_payload("t1", "approve", 100, 12)
        signed = sign_license(priv, payload)
        resp = await client.post(
            "/api/licenses/validate",
            json={"license_key": signed},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is True
        assert data["payload"]["plan"] == "approve"
        assert data["errors"] == []

    async def test_validate_license_expired(self, env):
        client, priv = env
        payload = build_license_payload("t1", "approve", 100, 12)
        payload["exp"] = int(time.time()) - 3600  # expired
        signed = sign_license(priv, payload)
        resp = await client.post(
            "/api/licenses/validate",
            json={"license_key": signed},
        )
        data = resp.json()
        assert data["valid"] is False
        assert any("expired" in e.lower() for e in data["errors"])

    async def test_validate_license_bad_format(self, env):
        client, _ = env
        resp = await client.post(
            "/api/licenses/validate",
            json={"license_key": "not-a-valid-license"},
        )
        data = resp.json()
        assert data["valid"] is False
        assert len(data["errors"]) > 0

    async def test_forged_license_is_refused(self, env):
        """The pre-fix hole: a token signed by ANY key with a future exp
        came back valid. It must fail signature verification now."""
        client, _deployment_key = env
        attacker_priv, _ = generate_keypair()
        payload = build_license_payload("t1", "enterprise", 10000, 120)
        forged = sign_license(attacker_priv, payload)
        resp = await client.post(
            "/api/licenses/validate",
            json={"license_key": forged},
        )
        data = resp.json()
        assert data["valid"] is False
        assert any("signature" in e.lower() for e in data["errors"])

    async def test_revoked_license_is_refused(self, env):
        client, _priv = env
        # Issue through the real API (uses the seeded deployment key),
        # download the token, revoke, then validate.
        t = await client.post(
            "/api/admin/tenants/",
            json={
                "name": "Rev Corp", "slug": "rev", "billing_country": "US",
                # A23-5: creation fails closed without an owner (A23.14 D3).
                "admin_email": "owner@rev.com",
            },
        )
        tenant_id = t.json()["id"]
        issued = await client.post(
            f"/api/tenants/{tenant_id}/licenses/",
            json={"plan": "approve", "node_commit": 10, "valid_months": 12},
        )
        lic_id = issued.json()["id"]
        download = await client.get(
            f"/api/tenants/{tenant_id}/licenses/{lic_id}/download"
        )
        token = download.text.strip().strip('"')
        ok = await client.post(
            "/api/licenses/validate", json={"license_key": token},
        )
        assert ok.json()["valid"] is True
        await client.post(
            f"/api/tenants/{tenant_id}/licenses/{lic_id}/revoke",
            json={"reason": "compromise"},
        )
        resp = await client.post(
            "/api/licenses/validate", json={"license_key": token},
        )
        data = resp.json()
        assert data["valid"] is False
        assert any("revoked" in e.lower() for e in data["errors"])
