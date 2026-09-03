"""E0.3: observability that observes, and reads an auditor can reach.

Three ratified items, each closing a gap where something was declared
and unreachable:

  /metrics          MetricsRegistry shipped with R4-0 and had NO callers.
                    Mounting it without counting anything would be the
                    same declared-with-no-writer pattern, so the request
                    counters are asserted to MOVE.
  A13 read gates    approval evidence and approval posture were gated on
                    permissions the auditor does not hold, so the
                    ratified "read-only everything" scope was not real.
  skill bindings    A0 accepted a capability kind and wired it to
                    nothing. Refused now, with the reason.
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

from tests.unit.cc.conftest import seed_tenant_admin

TENANT = "t1"


async def _client(role: str = "tenant_owner"):
    config = CCConfig(tenant_id=TENANT, insecure=True)
    configure_auth("", "", "", insecure=True)
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sessionmaker = make_sessionmaker(engine)
    state = AppState(config=config, engine=engine, sessionmaker=sessionmaker)
    app = create_app(state)

    # A23-5: a rowless tenant is STRICT now (A23.11). The acting
    # principal holds a real founding grant, the way tenant birth
    # seeds one (A23.14 D4), instead of being tenant-wide by the
    # `legacy_open` synthesis a missing row used to give.
    await seed_tenant_admin(sessionmaker, TENANT, f"kc-{role}", role=role)

    async def _fake():
        return UserContext(
            user_id=f"kc-{role}", email=f"{role}@example.com",
            tenant_id=TENANT, role=role,
            permissions=list(ROLE_PERMISSIONS[role]),
        )

    app.dependency_overrides[get_current_user] = _fake
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ), sessionmaker


async def _seed(sessionmaker) -> str:
    async with sessionmaker() as session:
        site = CCSite(tenant_id=TENANT, site_name="DC-1",
                      sm_endpoint="sm:50051", sm_token="tok")
        session.add(site)
        await session.flush()
        session.add(CCFleetCache(
            site_id=site.id, agent_id="node-1", agent_name="n1",
            vendor="Dell", model="R750", device_class="server",
            observation="observed", health="OK",
        ))
        await session.commit()
        return site.id


class TestMetrics:
    @pytest.mark.asyncio
    async def test_metrics_is_served_in_prometheus_text_format(self):
        client, _ = await _client()
        res = await client.get("/metrics")
        assert res.status_code == 200
        body = res.text
        assert "# TYPE harkeniq_http_requests_total counter" in body
        assert "harkeniq_up 1.0" in body
        assert "harkeniq_start_time_seconds" in body
        await client.aclose()

    @pytest.mark.asyncio
    async def test_the_request_counter_actually_moves(self):
        """Mounting an endpoint that always reports zero would be the
        very pattern this slice exists to remove."""
        client, _ = await _client()

        def _requests(text: str) -> float:
            for line in text.splitlines():
                if line.startswith("harkeniq_http_requests_total "):
                    return float(line.split()[1])
            raise AssertionError("counter missing")

        before = _requests((await client.get("/metrics")).text)
        await client.get("/healthz")
        after = _requests((await client.get("/metrics")).text)
        assert after > before
        await client.aclose()

    @pytest.mark.asyncio
    async def test_metrics_needs_no_token_and_leaks_no_tenant(self):
        """Scraped like /healthz, and carrying only service counters."""
        config = CCConfig(tenant_id="a-very-distinctive-tenant", insecure=True)
        configure_auth("", "", "", insecure=True)
        engine = make_engine("sqlite+aiosqlite:///:memory:")
        await create_all(engine)
        state = AppState(config=config, engine=engine,
                         sessionmaker=make_sessionmaker(engine))
        app = create_app(state)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            body = (await client.get("/metrics")).text
        assert "a-very-distinctive-tenant" not in body

    @pytest.mark.asyncio
    async def test_each_service_counts_its_own_requests(self):
        """A module-global registry would make two services in one
        process report each other's traffic."""
        a, _ = await _client()
        b, _ = await _client()
        await a.get("/healthz")
        await a.get("/healthz")

        def _requests(text: str) -> float:
            for line in text.splitlines():
                if line.startswith("harkeniq_http_requests_total "):
                    return float(line.split()[1])
            raise AssertionError("counter missing")

        assert _requests((await a.get("/metrics")).text) > _requests(
            (await b.get("/metrics")).text
        )
        await a.aclose()
        await b.aclose()


class TestAuditorCanReadTheEvidence:
    """A13: read-only everything. These are the three gates it lacked."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", [
        "/api/approvals/",
        "/api/approvals/history",
        "/api/policies/",
        "/api/policies/groups",
    ])
    async def test_auditor_reads(self, path):
        client, sessionmaker = await _client("auditor")
        await _seed(sessionmaker)
        assert (await client.get(path)).status_code == 200
        await client.aclose()

    @pytest.mark.asyncio
    async def test_auditor_reads_the_per_approver_ledger(self):
        """The R-C3 evidence added in E0.1 had the same gate."""
        client, sessionmaker = await _client("auditor")
        await _seed(sessionmaker)
        res = await client.get("/api/approvals/any-action/records")
        assert res.status_code == 200
        await client.aclose()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path,payload", [
        ("/api/policies/", {"name": "x"}),
        ("/api/policies/groups", {"name": "g"}),
        ("/api/approvals/act-1/approve", None),
    ])
    async def test_auditor_still_mutates_nothing(self, path, payload):
        client, sessionmaker = await _client("auditor")
        await _seed(sessionmaker)
        res = await client.post(path, json=payload) if payload else \
            await client.post(path)
        assert res.status_code == 403
        await client.aclose()

    @pytest.mark.asyncio
    async def test_a_viewer_still_cannot_read_approval_evidence(self):
        """The read opened to the auditor, not to everyone."""
        client, sessionmaker = await _client("viewer")
        await _seed(sessionmaker)
        assert (await client.get("/api/approvals/")).status_code == 403
        assert (await client.get("/api/approvals/history")).status_code == 403
        await client.aclose()


class TestSkillBindingIsRefused:
    """E0.3 refused skill bindings and named A2 as the owner of the four
    missing pieces. A2 built all four, so the refusal is gone -- and the
    replacement must not be laxer, only later: a skill is accepted here
    and JUDGED at preflight, where the Registry can be asked whether the
    agent's own devices can perform what it recommends. That question
    needs the agent's scope, so it cannot be answered at binding time."""

    @pytest.mark.asyncio
    async def test_a_skill_binding_is_now_accepted_and_judged_at_preflight(self):
        client, sessionmaker = await _client()
        site_id = await _seed(sessionmaker)
        res = await client.post("/api/operational-agents/", json={
            "name": "with-skill",
            "scopes": [{"scope_type": "site", "scope_ref": site_id}],
            "capabilities": [
                {"kind": "action_class", "capability_ref": "SEL_CLEAR"},
                {"kind": "skill", "capability_ref": "fan-health"},
            ],
        })
        assert res.status_code == 201, res.text
        kinds = {c["kind"] for c in res.json()["capabilities"]}
        assert "skill" in kinds
        await client.aclose()

    @pytest.mark.asyncio
    async def test_an_empty_skill_reference_is_still_refused(self):
        """Accepting the KIND does not mean accepting anything in it."""
        client, sessionmaker = await _client()
        site_id = await _seed(sessionmaker)
        res = await client.post("/api/operational-agents/", json={
            "name": "blank-skill",
            "scopes": [{"scope_type": "site", "scope_ref": site_id}],
            "capabilities": [
                {"kind": "action_class", "capability_ref": "SEL_CLEAR"},
                {"kind": "skill", "capability_ref": "   "},
            ],
        })
        assert res.status_code == 400
        await client.aclose()

    @pytest.mark.asyncio
    async def test_an_agent_without_skill_bindings_is_unaffected(self):
        client, sessionmaker = await _client()
        site_id = await _seed(sessionmaker)
        res = await client.post("/api/operational-agents/", json={
            "name": "no-skill",
            "scopes": [{"scope_type": "site", "scope_ref": site_id}],
            "capabilities": [
                {"kind": "action_class", "capability_ref": "SEL_CLEAR"}
            ],
        })
        assert res.status_code == 201
        assert res.json()["capabilities"], "reads and classes still bind"
        await client.aclose()
