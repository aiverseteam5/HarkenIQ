"""S4: real incidents with their diagnosis at Central Command.

What this pins:
  * the diagnosis actually crosses the wire (it used to stop at the SM);
  * correlation hierarchy survives, so one root cause is one incident;
  * resolution is inferred by ABSENCE, per D3, and scoped to one site;
  * LLM-generated text is marked as generated, because a future agent
    reading this contract is itself a language model;
  * prior learning from S3 is attached — "has the fleet seen this before";
  * tenant isolation.

Uses REAL ORM rows, not hand-rolled doubles: in S3 a double that carried a
field the persisted model does not have hid a 500 until it reached a live
stack.
"""

from __future__ import annotations

import httpx
import pytest

from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.app import create_app
from harkeniq_cc.auth import ROLE_PERMISSIONS, UserContext, configure_auth
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.models import CCFleetCache, CCIncident, CCLearnedSignal, CCSite
from harkeniq_cc.db.repos import IncidentRepo
from harkeniq_cc.fleet_poller import _ingest_incidents
from harkeniq_cc.runtime import AppState

from tests.unit.cc.conftest import seed_tenant_admin

TENANT = "t1"
OTHER = "t2"

LLM_EXPLANATION = {
    "provider": "llm",
    "summary": "Fan 1A has stopped; the chassis is losing thermal headroom.",
    "confidence": 0.82,
    "evidence_cited": ["fan:Fan1A reading 0 RPM", "thermal margin falling"],
    "reasoning_steps": ["fan reads zero", "neighbouring fans compensating"],
    "suggested_action": "Collect diagnostics, then replace the fan module.",
    "similar_past_incidents": [],
}


def _snapshot_incident(iid="inc-1", parent=None, agent="a1", **over):
    payload = {
        "incident_id": iid,
        "kind": "device",
        "status": "open",
        "title": "fan CRITICAL",
        "device_agent_id": agent,
        "subsystem": "fan",
        "opened_at_unix": 1787000000,
        "parent_incident_id": parent or "",
        "confidence": 1.0,
        "inferred": False,
        "correlation_meta": {},
        "explanation": {},
    }
    payload.update(over)
    return payload


async def _stack(role: str = "operator", tenant: str = TENANT):
    config = CCConfig(tenant_id=tenant, insecure=True)
    configure_auth("", "", "", insecure=True)
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sessionmaker = make_sessionmaker(engine)
    state = AppState(config=config, engine=engine, sessionmaker=sessionmaker)
    app = create_app(state)
    # A23-5: a rowless tenant is STRICT now (A23.11), so this
    # fixture seeds the founding administrator that tenant
    # birth seeds (A23.14 D4) instead of leaning on the
    # `legacy_open` synthesis a missing row used to give.
    await seed_tenant_admin(sessionmaker, tenant, "kc-1", role=role)

    async def _fake():
        return UserContext(
            user_id="kc-1", email=f"{role}@example.com", tenant_id=tenant,
            role=role,
            permissions=list(ROLE_PERMISSIONS.get(role, ROLE_PERMISSIONS["viewer"])),
            is_platform_user=False,
        )

    app.dependency_overrides[get_current_user] = _fake
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    )
    return client, engine, sessionmaker


async def _site(sessionmaker, tenant=TENANT, name="BLR-1"):
    async with sessionmaker() as session:
        site = CCSite(
            tenant_id=tenant, site_name=name,
            sm_endpoint="sm:50051", sm_token="tok",
        )
        session.add(site)
        await session.commit()
        return site.id


class TestIngestion:
    async def test_the_diagnosis_crosses_the_wire(self, db):
        """Before S4 the LLM explanation stopped at the Site Manager, so the
        tenant surface could say WHAT was wrong but never WHY."""
        async with db() as session:
            site = CCSite(tenant_id=TENANT, site_name="s", sm_endpoint="e",
                          sm_token="t")
            session.add(site)
            await session.flush()
            await _ingest_incidents(session, TENANT, site.id, [
                _snapshot_incident(explanation=LLM_EXPLANATION),
            ])
            await session.commit()
            row = await IncidentRepo(session).get(TENANT, "inc-1")

        assert row is not None
        assert row.explanation["provider"] == "llm"
        assert "thermal headroom" in row.explanation["summary"]

    async def test_a_later_poll_can_deliver_a_diagnosis_that_was_not_ready(self, db):
        """The LLM enriches asynchronously at the site, so the incident can
        legitimately arrive before its explanation does."""
        async with db() as session:
            site = CCSite(tenant_id=TENANT, site_name="s", sm_endpoint="e",
                          sm_token="t")
            session.add(site)
            await session.flush()
            sid = site.id
            await _ingest_incidents(session, TENANT, sid, [_snapshot_incident()])
            await session.commit()
            assert (await IncidentRepo(session).get(TENANT, "inc-1")).explanation in (None, {})

            await _ingest_incidents(session, TENANT, sid, [
                _snapshot_incident(explanation=LLM_EXPLANATION),
            ])
            await session.commit()
            row = await IncidentRepo(session).get(TENANT, "inc-1")
        assert row.explanation["summary"]

    async def test_resolution_is_inferred_by_absence(self, db):
        """D3: the snapshot carries only OPEN incidents, so one that stops
        appearing has cleared at the site."""
        async with db() as session:
            site = CCSite(tenant_id=TENANT, site_name="s", sm_endpoint="e",
                          sm_token="t")
            session.add(site)
            await session.flush()
            sid = site.id
            await _ingest_incidents(session, TENANT, sid, [
                _snapshot_incident("inc-1"), _snapshot_incident("inc-2"),
            ])
            await session.commit()

            await _ingest_incidents(session, TENANT, sid, [
                _snapshot_incident("inc-2"),
            ])
            await session.commit()
            repo = IncidentRepo(session)
            gone = await repo.get(TENANT, "inc-1")
            still = await repo.get(TENANT, "inc-2")

        assert gone.status == "resolved" and gone.resolved_at is not None
        assert still.status == "open"
        # The row is kept: an incident that happened is part of the record.
        assert gone.title == "fan CRITICAL"

    async def test_absence_inference_never_crosses_sites(self, db):
        """Another site's incidents are absent from THIS snapshot for the
        obvious reason; resolving them would be a data-loss bug."""
        async with db() as session:
            a = CCSite(tenant_id=TENANT, site_name="A", sm_endpoint="e", sm_token="t")
            b = CCSite(tenant_id=TENANT, site_name="B", sm_endpoint="e", sm_token="t")
            session.add_all([a, b])
            await session.flush()
            await _ingest_incidents(session, TENANT, a.id, [_snapshot_incident("at-a")])
            await _ingest_incidents(session, TENANT, b.id, [_snapshot_incident("at-b")])
            await session.commit()

            # Site A polls again with nothing open.
            await _ingest_incidents(session, TENANT, a.id, [])
            await session.commit()
            repo = IncidentRepo(session)
            at_a = await repo.get(TENANT, "at-a")
            at_b = await repo.get(TENANT, "at-b")

        assert at_a.status == "resolved"
        assert at_b.status == "open", "site B's incident must be untouched"

    async def test_a_reappearing_incident_reopens(self, db):
        async with db() as session:
            site = CCSite(tenant_id=TENANT, site_name="s", sm_endpoint="e",
                          sm_token="t")
            session.add(site)
            await session.flush()
            sid = site.id
            await _ingest_incidents(session, TENANT, sid, [_snapshot_incident()])
            await session.commit()
            await _ingest_incidents(session, TENANT, sid, [])
            await session.commit()
            await _ingest_incidents(session, TENANT, sid, [_snapshot_incident()])
            await session.commit()
            row = await IncidentRepo(session).get(TENANT, "inc-1")
        assert row.status == "open" and row.resolved_at is None


class TestProvenanceIsExplicit:
    """A future Operational Agent reading this contract is itself a language
    model, and the diagnosis text was generated from device telemetry, which
    is attacker-influenceable if a BMC is compromised. The contract must say
    so rather than leaving the consumer to infer it."""

    async def test_llm_text_is_marked_generated_and_untrusted(self):
        client, engine, sm = await _stack()
        try:
            site_id = await _site(sm)
            async with sm() as session:
                await _ingest_incidents(session, TENANT, site_id, [
                    _snapshot_incident(explanation=LLM_EXPLANATION),
                ])
                await session.commit()

            body = (await client.get("/api/incidents/")).json()
            diag = body["incidents"][0]["diagnosis"]
            assert diag["origin"] == "llm"
            assert diag["trust"] == "untrusted_generated"
            # Model-authored fields are grouped, not scattered among facts.
            assert "summary" in diag["generated"]
            assert "suggested_action" in diag["generated"]
            # Platform-produced references stay outside the generated block.
            assert diag["evidence_cited"]
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_deterministic_reasoning_is_not_labelled_untrusted(self):
        client, engine, sm = await _stack()
        try:
            site_id = await _site(sm)
            async with sm() as session:
                await _ingest_incidents(session, TENANT, site_id, [
                    _snapshot_incident(explanation={
                        "provider": "knowledge_base", "summary": "seen before",
                        "confidence": 0.9,
                    }),
                ])
                await session.commit()
            diag = (await client.get("/api/incidents/")).json()["incidents"][0]["diagnosis"]
            assert diag["origin"] == "knowledge_base"
            assert diag["trust"] == "deterministic"
        finally:
            await client.aclose()
            await engine.dispose()


class TestHierarchyAndScope:
    async def test_children_nest_under_their_parent(self):
        """SM consolidates correlated faults into one parent; flattening
        would show N incidents for one root cause."""
        client, engine, sm = await _stack()
        try:
            site_id = await _site(sm)
            async with sm() as session:
                await _ingest_incidents(session, TENANT, site_id, [
                    _snapshot_incident("parent", kind="shared_power",
                                       title="PDU fault", agent=""),
                    _snapshot_incident("child-1", parent="parent", agent="a1"),
                    _snapshot_incident("child-2", parent="parent", agent="a2"),
                ])
                await session.commit()

            body = (await client.get("/api/incidents/")).json()
            parents = [i for i in body["incidents"] if i["is_parent"]]
            assert len(parents) == 1
            assert parents[0]["child_count"] == 2
            assert {c["incident_id"] for c in parents[0]["children"]} == {
                "child-1", "child-2"
            }
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_inferred_domains_are_flagged(self):
        client, engine, sm = await _stack()
        try:
            site_id = await _site(sm)
            async with sm() as session:
                await _ingest_incidents(session, TENANT, site_id, [
                    _snapshot_incident(inferred=True, confidence=0.6),
                ])
                await session.commit()
            item = (await client.get("/api/incidents/")).json()["incidents"][0]
            assert item["inferred"] is True
            assert item["confidence"] == 0.6
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_another_tenants_incidents_are_invisible(self):
        client, engine, sm = await _stack()
        try:
            mine = await _site(sm)
            theirs = await _site(sm, tenant=OTHER, name="THEIRS")
            async with sm() as session:
                await _ingest_incidents(session, TENANT, mine, [
                    _snapshot_incident("mine"),
                ])
                await _ingest_incidents(session, OTHER, theirs, [
                    _snapshot_incident("theirs"),
                ])
                await session.commit()
            body = (await client.get("/api/incidents/")).json()
            assert {i["incident_id"] for i in body["incidents"]} == {"mine"}
        finally:
            await client.aclose()
            await engine.dispose()


class TestDetailJoinsLearningAndGovernance:
    async def test_prior_learning_from_s3_is_attached(self):
        """S3 -> S4: has the fleet seen this before?"""
        client, engine, sm = await _stack()
        try:
            site_id = await _site(sm)
            async with sm() as session:
                session.add(CCFleetCache(
                    site_id=site_id, agent_id="a1", agent_name="srv-1",
                    vendor="Dell", model="R750", health="critical",
                    observation="observed", service_tag="T1",
                ))
                session.add(CCLearnedSignal(
                    tenant_id=TENANT, signal_key="cohort:dell/r750:SEL_CLEAR",
                    scope_type="cohort", scope_ref="dell/r750",
                    action_type="SEL_CLEAR", vendor="Dell", model="R750",
                    statement="SEL_CLEAR on Dell R750 fails 75% of the time.",
                    evidence={"failure_rate": 0.75}, confidence=0.6,
                    source_pattern_id="pat-1",
                ))
                await _ingest_incidents(session, TENANT, site_id, [
                    _snapshot_incident(explanation=LLM_EXPLANATION),
                ])
                await session.commit()

            detail = (await client.get("/api/incidents/inc-1")).json()
            assert detail["prior_learning"], "the incident must carry what we learned"
            assert "75%" in detail["prior_learning"][0]["statement"]
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_a_suggested_action_is_quoted_not_made_invocable(self):
        """The suggestion is generated text. Acting on it goes through the
        device's agent and its own gates, never from this surface."""
        client, engine, sm = await _stack()
        try:
            site_id = await _site(sm)
            async with sm() as session:
                await _ingest_incidents(session, TENANT, site_id, [
                    _snapshot_incident(explanation=LLM_EXPLANATION),
                ])
                await session.commit()
            rec = (await client.get("/api/incidents/inc-1")).json()["recommended_next"]
            assert rec["capability"] == "propose_action"
            assert rec["requires_approval"] is True
            assert rec["available"] is False
            assert rec["unavailable_reason"]
            # No token, no pre-authorised handle.
            assert "token" not in rec and "authorization" not in rec
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_unknown_incident_is_404_not_a_leak(self):
        client, engine, sm = await _stack()
        try:
            assert (await client.get("/api/incidents/nope")).status_code == 404
        finally:
            await client.aclose()
            await engine.dispose()


class TestGovernance:
    @pytest.mark.parametrize("role,expected", [
        ("tenant_owner", 200), ("site_admin", 200), ("operator", 200),
        ("viewer", 200), ("auditor", 200),
    ])
    async def test_every_tenant_role_may_read_incidents(self, role, expected):
        client, engine, sm = await _stack(role)
        try:
            assert (await client.get("/api/incidents/")).status_code == expected
        finally:
            await client.aclose()
            await engine.dispose()

    async def test_incidents_are_read_only(self):
        client, engine, sm = await _stack()
        try:
            for verb in ("post", "put", "patch", "delete"):
                resp = await getattr(client, verb)("/api/incidents/")
                assert resp.status_code in (404, 405), verb
        finally:
            await client.aclose()
            await engine.dispose()
