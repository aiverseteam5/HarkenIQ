"""Cross-site correlation tests (R4-1, R-C2).

Covers: site-aware OutcomeAggregator, cross_site_batch pattern detection,
outcome/pattern repos, the IntelligenceEngine cycle, and the CC outcomes
API. Exit criterion: cross-site pattern detection identifies batch
failures spanning 2+ sites.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from harkeniq_cc.app import create_app
from harkeniq_cc.auth import configure_auth
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.models import CCOutcomeHistory
from harkeniq_cc.db.repos import FleetPatternRepo, OutcomeHistoryRepo, SiteRepo
from harkeniq_cc.intelligence import IntelligenceEngine
from harkeniq_cc.outcome_aggregator import OutcomeAggregator
from harkeniq_cc.pattern_detector import FleetPattern, PatternDetector
from harkeniq_cc.runtime import AppState

from tests.unit.cc.conftest import seed_tenant_admin

TENANT = "test-tenant"


def _outcomes(action_type, vendor, model, results, site_id=""):
    return [
        {
            "action_type": action_type,
            "vendor": vendor,
            "model": model,
            "outcome": r,
            "site_id": site_id,
        }
        for r in results
    ]


class TestSiteAwareAggregator:
    def test_site_counts_tracked(self):
        agg = OutcomeAggregator()
        agg.ingest(_outcomes("FAN_RESET", "dell", "R750",
                             ["SUCCESS", "FAILURE"], site_id="site-a"))
        agg.ingest(_outcomes("FAN_RESET", "dell", "R750",
                             ["FAILURE"], site_id="site-b"))
        m = agg.get_metrics()[0]
        assert m.site_counts == {"site-a": 2, "site-b": 1}
        assert m.site_failure_counts == {"site-a": 1, "site-b": 1}
        assert m.site_count == 2
        assert m.failing_site_count == 2

    def test_rollback_counts_as_site_failure(self):
        agg = OutcomeAggregator()
        agg.ingest(_outcomes("SEL_CLEAR", "dell", "R750",
                             ["ROLLBACK"], site_id="site-a"))
        m = agg.get_metrics()[0]
        assert m.site_failure_counts == {"site-a": 1}

    def test_missing_site_id_still_aggregates(self):
        # Backward compatibility: R3b-3 callers pass no site_id.
        agg = OutcomeAggregator()
        agg.ingest(_outcomes("FAN_RESET", "dell", "R750",
                             ["SUCCESS", "FAILURE"]))
        m = agg.get_metrics()[0]
        assert m.total_count == 2
        assert m.failure_count == 1
        assert m.site_count == 0
        assert m.failing_site_count == 0


class TestCrossSiteDetection:
    def test_single_site_no_cross_site_pattern(self):
        agg = OutcomeAggregator()
        agg.ingest(_outcomes("FAN_RESET", "dell", "R750",
                             ["FAILURE"] * 4 + ["SUCCESS"] * 4, site_id="site-a"))
        detector = PatternDetector()
        patterns = detector.detect(agg)
        types = {p.pattern_type for p in patterns}
        assert "batch_failure" in types
        assert "cross_site_batch" not in types

    def test_two_sites_detected(self):
        agg = OutcomeAggregator()
        agg.ingest(_outcomes("FAN_RESET", "dell", "R750",
                             ["FAILURE"] * 3 + ["SUCCESS"] * 3, site_id="site-a"))
        agg.ingest(_outcomes("FAN_RESET", "dell", "R750",
                             ["FAILURE"] * 2 + ["SUCCESS"] * 2, site_id="site-b"))
        detector = PatternDetector()
        patterns = detector.detect(agg)
        cross = [p for p in patterns if p.pattern_type == "cross_site_batch"]
        assert len(cross) == 1
        p = cross[0]
        assert p.affected_scope["sites"] == "site-a,site-b"
        assert p.evidence["sites_affected"] == 2
        assert p.evidence["site_failure_counts"] == {"site-a": 3, "site-b": 2}
        assert "across 2 sites" in p.description

    def test_below_failure_threshold_not_detected(self):
        agg = OutcomeAggregator()
        agg.ingest(_outcomes("FAN_RESET", "dell", "R750",
                             ["FAILURE"] + ["SUCCESS"] * 9, site_id="site-a"))
        agg.ingest(_outcomes("FAN_RESET", "dell", "R750",
                             ["FAILURE"] + ["SUCCESS"] * 9, site_id="site-b"))
        detector = PatternDetector()
        patterns = detector.detect(agg)
        assert not [p for p in patterns if p.pattern_type == "cross_site_batch"]

    def test_dedup_across_cycles(self):
        agg = OutcomeAggregator()
        agg.ingest(_outcomes("FAN_RESET", "dell", "R750",
                             ["FAILURE"] * 3, site_id="site-a"))
        agg.ingest(_outcomes("FAN_RESET", "dell", "R750",
                             ["FAILURE"] * 3, site_id="site-b"))
        detector = PatternDetector()
        first = detector.detect(agg)
        second = detector.detect(agg)
        assert [p for p in first if p.pattern_type == "cross_site_batch"]
        assert not [p for p in second if p.pattern_type == "cross_site_batch"]

    def test_multi_site_confidence_exceeds_single_site(self):
        agg = OutcomeAggregator()
        for site in ("site-a", "site-b", "site-c"):
            agg.ingest(_outcomes("FAN_RESET", "dell", "R750",
                                 ["FAILURE"] * 2, site_id=site))
        detector = PatternDetector()
        patterns = detector.detect(agg)
        cross = next(p for p in patterns if p.pattern_type == "cross_site_batch")
        batch = next(p for p in patterns if p.pattern_type == "batch_failure")
        assert cross.confidence > batch.confidence


async def _seed_sites(session):
    repo = SiteRepo(session)
    site_a = await repo.upsert(TENANT, "dc-blr-1", "https://sm1.lab:50051")
    site_b = await repo.upsert(TENANT, "dc-mum-1", "https://sm2.lab:50051")
    return site_a, site_b


def _history_row(site_id, action_type="FAN_RESET", vendor="dell",
                 model="R750", outcome="FAILURE"):
    return CCOutcomeHistory(
        site_id=site_id,
        action_id="act-1",
        action_type=action_type,
        device_agent_id="agent-1",
        vendor=vendor,
        model=model,
        outcome=outcome,
    )


class TestRepos:
    async def test_outcome_dicts_include_site(self, session):
        site_a, site_b = await _seed_sites(session)
        session.add(_history_row(site_a.id))
        session.add(_history_row(site_b.id, outcome="SUCCESS"))
        await session.commit()
        rows = await OutcomeHistoryRepo(session).list_outcome_dicts(TENANT)
        assert len(rows) == 2
        assert {r["site_id"] for r in rows} == {site_a.id, site_b.id}

    async def test_tenant_scoping(self, session):
        site_a, _ = await _seed_sites(session)
        other = await SiteRepo(session).upsert(
            "other-tenant", "dc-x", "https://smx:50051"
        )
        session.add(_history_row(site_a.id))
        session.add(_history_row(other.id))
        await session.commit()
        rows = await OutcomeHistoryRepo(session).list_outcome_dicts(TENANT)
        assert len(rows) == 1
        assert rows[0]["site_id"] == site_a.id

    async def test_since_cursor(self, session):
        site_a, _ = await _seed_sites(session)
        session.add(_history_row(site_a.id))
        await session.commit()
        repo = OutcomeHistoryRepo(session)
        first = await repo.list_outcome_dicts(TENANT)
        cursor = first[-1]["ingested_at"]
        assert await repo.list_outcome_dicts(TENANT, since=cursor) == []

    async def test_pattern_save_idempotent(self, session):
        pattern = FleetPattern(
            pattern_id="pat-test0001",
            pattern_type="cross_site_batch",
            description="test",
            affected_scope={"vendor": "dell", "sites": "a,b"},
            confidence=0.8,
            evidence={"total": 6},
        )
        repo = FleetPatternRepo(session)
        await repo.save(pattern)
        await repo.save(pattern)
        await session.commit()
        rows = await repo.list_patterns()
        assert len(rows) == 1
        assert rows[0].id == "pat-test0001"
        assert rows[0].affected_scope["sites"] == "a,b"

    async def test_pattern_resolve(self, session):
        pattern = FleetPattern(
            pattern_id="pat-test0002", pattern_type="batch_failure",
            description="x", affected_scope={}, confidence=0.5,
        )
        repo = FleetPatternRepo(session)
        await repo.save(pattern)
        await repo.resolve("pat-test0002")
        await session.commit()
        assert await repo.list_patterns(status="active") == []
        resolved = await repo.list_patterns(status="resolved")
        assert len(resolved) == 1


class TestIntelligenceEngine:
    async def test_cycle_detects_and_persists_cross_site(self, session):
        site_a, site_b = await _seed_sites(session)
        for _ in range(3):
            session.add(_history_row(site_a.id))
            session.add(_history_row(site_b.id))
        await session.commit()

        engine = IntelligenceEngine()
        patterns = await engine.run_cycle(session, TENANT)
        await session.commit()
        assert [p for p in patterns if p.pattern_type == "cross_site_batch"]
        stored = await FleetPatternRepo(session).list_patterns(
            pattern_type="cross_site_batch"
        )
        assert len(stored) == 1

    async def test_cycle_cursor_no_reingest(self, session):
        site_a, site_b = await _seed_sites(session)
        for _ in range(3):
            session.add(_history_row(site_a.id))
            session.add(_history_row(site_b.id))
        await session.commit()

        engine = IntelligenceEngine()
        await engine.run_cycle(session, TENANT)
        total_after_first = engine.aggregator.get_metrics()[0].total_count
        await engine.run_cycle(session, TENANT)
        assert engine.aggregator.get_metrics()[0].total_count == total_after_first

    async def test_second_cycle_no_duplicate_patterns(self, session):
        site_a, site_b = await _seed_sites(session)
        for _ in range(3):
            session.add(_history_row(site_a.id))
            session.add(_history_row(site_b.id))
        await session.commit()

        engine = IntelligenceEngine()
        first = await engine.run_cycle(session, TENANT)
        second = await engine.run_cycle(session, TENANT)
        assert first
        assert second == []


@pytest.fixture
async def client():
    """CC app with two sites and cross-site failure outcomes seeded."""
    config = CCConfig(tenant_id=TENANT, insecure=True)
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
    await seed_tenant_admin(sessionmaker, TENANT, "lab-user")

    async with sessionmaker() as session:
        site_a, site_b = await _seed_sites(session)
        for _ in range(3):
            session.add(_history_row(site_a.id))
            session.add(_history_row(site_b.id))
        for _ in range(4):
            session.add(_history_row(site_a.id, outcome="SUCCESS"))
        # Persist a pattern the way the intelligence loop would
        intel = IntelligenceEngine()
        await intel.run_cycle(session, TENANT)
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await engine.dispose()


class TestOutcomesAPI:
    async def test_metrics_endpoint(self, client):
        r = await client.get("/api/outcomes/metrics")
        assert r.status_code == 200
        data = r.json()
        assert data["total_outcomes"] == 10
        m = data["metrics"][0]
        assert m["action_type"] == "FAN_RESET"
        assert m["vendor"] == "dell"
        assert m["total_count"] == 10
        assert m["failure_count"] == 6
        assert m["site_count"] == 2
        assert m["failing_site_count"] == 2
        assert len(m["sites"]) == 2

    async def test_metrics_vendor_filter(self, client):
        r = await client.get("/api/outcomes/metrics", params={"vendor": "hpe"})
        assert r.status_code == 200
        assert r.json()["metrics"] == []

    async def test_patterns_endpoint(self, client):
        r = await client.get("/api/outcomes/patterns")
        assert r.status_code == 200
        patterns = r.json()["patterns"]
        assert patterns
        types = {p["pattern_type"] for p in patterns}
        assert "cross_site_batch" in types
        cross = next(p for p in patterns
                     if p["pattern_type"] == "cross_site_batch")
        assert cross["status"] == "active"
        assert cross["evidence"]["sites_affected"] == 2

    async def test_patterns_type_filter(self, client):
        r = await client.get(
            "/api/outcomes/patterns", params={"pattern_type": "nonexistent"}
        )
        assert r.status_code == 200
        assert r.json()["patterns"] == []
