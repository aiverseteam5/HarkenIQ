"""Site/domain HTTP API via ASGITransport; bearer auth contract."""

import httpx
import pytest

from harkeniq_sm.app import create_app
from harkeniq_sm.config import SMConfig
from harkeniq_sm.ingest import IngestService
from harkeniq_sm.runtime import AppState

TOKEN = "site-secret"

YAML_DOC = """
fault_domains:
  - name: pdu-a
    kind: power
    members: [a1, a2]
"""


def _state(db, **overrides):
    config = SMConfig(site_name="site-test", **overrides)
    state = AppState(config=config)
    state.sessionmaker = db
    state.ingest = IngestService(db, config)
    return state


@pytest.fixture
async def client(db):
    state = _state(db, insecure=True)
    transport = httpx.ASGITransport(app=create_app(state))
    async with httpx.AsyncClient(transport=transport, base_url="http://sm") as c:
        yield c


class TestAuth:
    async def test_token_required_when_configured(self, db):
        state = _state(db, site_token=TOKEN)
        transport = httpx.ASGITransport(app=create_app(state))
        async with httpx.AsyncClient(transport=transport, base_url="http://sm") as c:
            assert (await c.get("/api/coverage")).status_code == 401
            assert (await c.get("/api/fault-domains")).status_code == 401
            assert (await c.get("/healthz")).status_code == 200  # probe stays open
            ok = await c.get(
                "/api/coverage", headers={"authorization": f"Bearer {TOKEN}"}
            )
            assert ok.status_code == 200


class TestSiteYamlApi:
    async def test_put_then_get(self, client):
        put = await client.put("/api/site/yaml", content=YAML_DOC)
        assert put.status_code == 200
        assert put.json()["imported"]["domains"] == 1
        got = await client.get("/api/site/yaml")
        assert got.status_code == 200
        assert "pdu-a" in got.text


class TestDomainApi:
    async def test_list_confirm_and_members(self, client):
        await client.put("/api/site/yaml", content=YAML_DOC)
        domains = (await client.get("/api/fault-domains")).json()
        assert len(domains) == 1
        domain = domains[0]
        assert domain["status"] == "confirmed"  # yaml import confirms
        assert domain["members"] == ["a1", "a2"]

        patched = await client.patch(
            f"/api/fault-domains/{domain['id']}",
            json={"members": ["a1"], "actor": "op"},
        )
        assert patched.status_code == 200
        assert patched.json()["members"] == ["a1"]

    async def test_confirm_inferred_domain(self, db, client):
        from harkeniq_sm.db.repos import DomainRepo, SiteRepo

        async with db() as session:
            site = await SiteRepo(session).get_or_create("site-test")
            domain = await DomainRepo(session).create(site.id, "cooling-rack-1", "cooling")
            await session.commit()
            domain_id = domain.id

        patched = await client.patch(
            f"/api/fault-domains/{domain_id}",
            json={"confirm": True, "actor": "op@site"},
        )
        body = patched.json()
        assert body["status"] == "confirmed"
        assert body["confidence"] == 1.0
        assert body["source"] == "operator"
        assert body["confirmed_by"] == "op@site"

    async def test_unknown_domain_404(self, client):
        assert (
            await client.patch("/api/fault-domains/nope", json={"confirm": True})
        ).status_code == 404

    async def test_unknown_member_422(self, client):
        await client.put("/api/site/yaml", content=YAML_DOC)
        domain = (await client.get("/api/fault-domains")).json()[0]
        resp = await client.patch(
            f"/api/fault-domains/{domain['id']}", json={"members": ["ghost"]}
        )
        assert resp.status_code == 422
