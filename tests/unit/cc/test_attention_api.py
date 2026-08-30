"""S2: the attention endpoint — RBAC, tenant isolation, scope filters.

The composer is tested purely in test_attention.py; this covers the parts
only the wired endpoint can prove: who may read it, that one tenant's
devices never appear in another's answer, and that site scoping narrows
within a tenant rather than widening across.
"""

from __future__ import annotations

import httpx
import pytest

from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.app import create_app
from harkeniq_cc.auth import ROLE_PERMISSIONS, UserContext, configure_auth
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.models import CCFleetCache, CCSite
from harkeniq_cc.runtime import AppState

TENANT = "t1"
OTHER = "t2"


async def _stack(role: str = "tenant_owner", tenant: str = TENANT):
    config = CCConfig(tenant_id=tenant, insecure=True)
    configure_auth("", "", "", insecure=True)
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sessionmaker = make_sessionmaker(engine)
    state = AppState(config=config, engine=engine, sessionmaker=sessionmaker)
    app = create_app(state)

    async def _fake():
        return UserContext(
            user_id=f"kc-{role}", email=f"{role}@example.com", tenant_id=tenant,
            role=role,
            permissions=list(ROLE_PERMISSIONS.get(role, ROLE_PERMISSIONS["viewer"])),
            is_platform_user=role == "platform_super_admin",
        )

    app.dependency_overrides[get_current_user] = _fake
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    )
    return client, engine, sessionmaker


async def _seed(sessionmaker, tenant: str, site_name: str, agents: list[str]):
    async with sessionmaker() as session:
        site = CCSite(
            tenant_id=tenant, site_name=site_name,
            sm_endpoint="sm:50051", sm_token="tok",
        )
        session.add(site)
        await session.flush()
        for a in agents:
            session.add(CCFleetCache(
                site_id=site.id, agent_id=a, agent_name=f"srv-{a}",
                vendor="Dell", model="R750", health="ok",
                observation="observed", service_tag=f"TAG-{a}",
            ))
        await session.commit()
        return site.id


class TestRbac:
    @pytest.mark.parametrize("role,expected", [
        ("tenant_owner", 200),
        ("site_admin", 200),
        ("operator", 200),
        ("viewer", 200),
        ("auditor", 200),
    ])
    async def test_every_tenant_role_may_read_attention(self, role, expected):
        """Read-only intelligence: if an operator cannot see what needs
        attention, the surface cannot do its job."""
        client, engine, sm = await _stack(role)
        try:
            await _seed(sm, TENANT, "BLR-1", ["a1"])
            assert (await client.get("/api/attention/")).status_code == expected
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_it_is_read_only(self):
        """No mutation verb exists on this capability."""
        client, engine, sm = await _stack()
        try:
            await _seed(sm, TENANT, "BLR-1", ["a1"])
            for verb in ("post", "put", "patch", "delete"):
                resp = await getattr(client, verb)("/api/attention/")
                assert resp.status_code in (404, 405), verb
        finally:
            await client.aclose()
            await engine.dispose()


class TestTenantIsolation:
    async def test_another_tenants_devices_never_appear(self):
        client, engine, sm = await _stack()
        try:
            await _seed(sm, TENANT, "BLR-1", ["mine-1"])
            await _seed(sm, OTHER, "OTHER-1", ["theirs-1"])
            body = (await client.get("/api/attention/")).json()
            agents = {i["agent_id"] for i in body["items"]}
            assert agents == {"mine-1"}
            assert body["tenant_id"] == TENANT
            assert all(s["site_name"] == "BLR-1" for s in body["sites"])
        finally:
            await client.aclose()
            await engine.dispose()


class TestScoping:
    async def test_site_filter_narrows_within_the_tenant(self):
        client, engine, sm = await _stack()
        try:
            site_a = await _seed(sm, TENANT, "BLR-1", ["a1", "a2"])
            await _seed(sm, TENANT, "PUN-1", ["b1"])
            full = (await client.get("/api/attention/")).json()
            assert len(full["items"]) == 3

            scoped = (await client.get(
                f"/api/attention/?site_id={site_a}"
            )).json()
            assert {i["agent_id"] for i in scoped["items"]} == {"a1", "a2"}
            assert all(i["site_id"] == site_a for i in scoped["items"])
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_unknown_site_returns_empty_not_everything(self):
        """A scope filter that fails open would hand a site-scoped agent the
        whole fleet."""
        client, engine, sm = await _stack()
        try:
            await _seed(sm, TENANT, "BLR-1", ["a1"])
            body = (await client.get(
                "/api/attention/?site_id=does-not-exist"
            )).json()
            assert body["items"] == []
            assert body["summary"]["devices_scored"] == 0
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_limit_truncates_but_rank_stays_tenant_wide(self):
        client, engine, sm = await _stack()
        try:
            await _seed(sm, TENANT, "BLR-1", ["a1", "a2", "a3"])
            body = (await client.get("/api/attention/?limit=2")).json()
            assert body["returned"] == 2
            assert len(body["items"]) == 2
            assert [i["rank"] for i in body["items"]] == [1, 2]
            # The summary still describes the tenant, not the page.
            assert body["summary"]["devices_scored"] == 3
        finally:
            await client.aclose()
            await engine.dispose()


class TestRealOrmShapes:
    """Regression: attention 500'd on the live stack the moment a real
    pattern row existed, because _patterns_for read `pattern_id` while the
    PERSISTED CCFleetPattern keys on `id` (only the in-memory detector
    dataclass has pattern_id). Unit tests missed it because their fakes
    carried pattern_id — a double that did not match the real model.
    This exercises the endpoint with genuine ORM rows."""

    async def test_attention_survives_a_real_persisted_pattern(self):
        from harkeniq_cc.db.models import CCFleetPattern, CCLearnedSignal

        client, engine, sm = await _stack()
        try:
            await _seed(sm, TENANT, "BLR-1", ["a1"])
            async with sm() as session:
                session.add(CCFleetPattern(
                    id="pat-real", tenant_id=TENANT,
                    pattern_type="batch_failure",
                    description="SEL_CLEAR fails on Dell R750",
                    affected_scope={"action_type": "SEL_CLEAR",
                                    "vendor": "Dell", "model": "R750"},
                    confidence=0.6,
                    evidence={"total": 8, "failures": 6, "failure_rate": 0.75},
                ))
                session.add(CCLearnedSignal(
                    tenant_id=TENANT, signal_key="cohort:dell/r750:SEL_CLEAR",
                    scope_type="cohort", scope_ref="dell/r750",
                    action_type="SEL_CLEAR", vendor="Dell", model="R750",
                    statement="SEL_CLEAR on Dell R750 fails 75% of the time.",
                    evidence={"failure_rate": 0.75}, confidence=0.6,
                    source_pattern_id="pat-real",
                ))
                await session.commit()

            resp = await client.get("/api/attention/")
            assert resp.status_code == 200, resp.text
            item = resp.json()["items"][0]
            # The pattern is attached, keyed off the ORM row's real id.
            assert item["evidence"]["fleet_patterns"][0]["pattern_id"] == "pat-real"
            # And the durable knowledge reached the answer.
            assert item["evidence"]["learned_signals"][0]["action_type"] == "SEL_CLEAR"
            assert any("Learned for" in r for r in item["reasons"])
        finally:
            await client.aclose()
            await engine.dispose()


class TestContractShape:
    async def test_item_carries_what_an_agent_needs(self):
        client, engine, sm = await _stack()
        try:
            await _seed(sm, TENANT, "BLR-1", ["a1"])
            item = (await client.get("/api/attention/")).json()["items"][0]
            for key in (
                "agent_id", "agent_name", "site_id", "site_name", "vendor",
                "model", "health", "risk_score", "band", "confidence",
                "factors", "reasons", "evidence", "current_state",
                "recommended_next", "rank",
            ):
                assert key in item, key
            # S3 added learned_signals: durable knowledge from prior
            # outcomes, which is what closes the loop back into attention.
            assert set(item["evidence"]) == {
                "learned_signals", "cves", "warranty", "fleet_patterns",
            }
            assert "capability" in item["recommended_next"]
        finally:
            await client.aclose()
            await engine.dispose()
