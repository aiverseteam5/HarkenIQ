"""Predictive maintenance tests (R4-3 P20)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from harkeniq_cc.app import create_app
from harkeniq_cc.auth import configure_auth
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.models import CCOutcomeHistory
from harkeniq_cc.db.repos import FleetCacheRepo, SiteRepo, WarrantyRepo
from harkeniq_cc.predictive import (
    MIN_DEVICE_SAMPLES,
    band_for,
    cohort_failure_rates,
    score_device,
    weighted_failure_rate,
)
from harkeniq_cc.runtime import AppState
from harkeniq_cc.warranty.base import WarrantyRecord

TENANT = "test-tenant"
NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)


def _oc(outcome: str, days_ago: float = 0.0) -> dict:
    return {"outcome": outcome, "recorded_at": NOW - timedelta(days=days_ago)}


class TestWeightedFailureRate:
    def test_all_success_is_zero(self):
        rate, weight = weighted_failure_rate(
            [_oc("SUCCESS"), _oc("SUCCESS")], now=NOW
        )
        assert rate == 0.0 and weight > 0

    def test_all_failures_is_one(self):
        rate, _ = weighted_failure_rate(
            [_oc("FAILURE"), _oc("ROLLBACK")], now=NOW
        )
        assert rate == 1.0

    def test_recent_failure_outweighs_old_failure(self):
        recent, _ = weighted_failure_rate(
            [_oc("FAILURE", days_ago=1), _oc("SUCCESS", days_ago=1),
             _oc("SUCCESS", days_ago=1)], now=NOW,
        )
        old, _ = weighted_failure_rate(
            [_oc("FAILURE", days_ago=120), _oc("SUCCESS", days_ago=1),
             _oc("SUCCESS", days_ago=1)], now=NOW,
        )
        assert recent > old

    def test_empty_is_zero_weight(self):
        assert weighted_failure_rate([], now=NOW) == (0.0, 0.0)


class TestScoreDevice:
    def test_healthy_history_low_band(self):
        risk = score_device(
            "a1", [_oc("SUCCESS", i) for i in range(10)], now=NOW,
        )
        assert risk.band == "low"
        assert risk.risk_score == 0.0
        assert risk.factors["basis"] == "device_history"

    def test_failing_device_high_band(self):
        risk = score_device(
            "a1", [_oc("FAILURE", i) for i in range(8)], now=NOW,
        )
        assert risk.band == "high"
        assert risk.risk_score >= 0.6

    def test_sparse_history_uses_cohort(self):
        risk = score_device(
            "a1", [_oc("SUCCESS")], cohort_failure_rate=0.4, now=NOW,
        )
        assert risk.factors["basis"] == "cohort_prior"
        assert risk.band == "medium"

    def test_no_data_is_insufficient(self):
        risk = score_device("a1", [], cohort_failure_rate=None, now=NOW)
        assert risk.band == "insufficient_data"
        assert risk.risk_score == 0.0
        assert risk.factors["min_samples"] == MIN_DEVICE_SAMPLES

    def test_health_and_warranty_bumps(self):
        baseline = score_device(
            "a1", [_oc("SUCCESS", i) for i in range(6)], now=NOW,
        )
        bumped = score_device(
            "a1", [_oc("SUCCESS", i) for i in range(6)],
            health="critical", warranty_status="expired", now=NOW,
        )
        assert bumped.risk_score == pytest.approx(
            baseline.risk_score + 0.20 + 0.10
        )
        assert bumped.factors["health_bump"] == 0.20
        assert bumped.factors["warranty_expired_bump"] == 0.10

    def test_score_clamped_to_one(self):
        risk = score_device(
            "a1", [_oc("FAILURE", 0) for _ in range(10)],
            health="critical", warranty_status="expired", now=NOW,
        )
        assert risk.risk_score == 1.0

    def test_bands(self):
        assert band_for(0.7) == "high"
        assert band_for(0.4) == "medium"
        assert band_for(0.1) == "low"


class TestCohorts:
    def test_cohort_rates(self):
        rates = cohort_failure_rates([
            {"vendor": "dell", "model": "R750", "outcome": "FAILURE"},
            {"vendor": "dell", "model": "R750", "outcome": "SUCCESS"},
            {"vendor": "hpe", "model": "DL380", "outcome": "SUCCESS"},
        ])
        assert rates[("dell", "R750")] == 0.5
        assert rates[("hpe", "DL380")] == 0.0


@pytest.fixture
async def client():
    config = CCConfig(tenant_id=TENANT, insecure=True)
    configure_auth("", "", "", insecure=True)
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sessionmaker = make_sessionmaker(engine)
    state = AppState(config=config, engine=engine, sessionmaker=sessionmaker)
    app = create_app(state)

    async with sessionmaker() as session:
        site = await SiteRepo(session).upsert(TENANT, "dc-blr-1", "sm:50051")
        cache = FleetCacheRepo(session)
        await cache.upsert_device(
            site_id=site.id, agent_id="bad-device", vendor="dell",
            model="R750", health="critical", service_tag="BAD1",
        )
        await cache.upsert_device(
            site_id=site.id, agent_id="good-device", vendor="dell",
            model="R750", health="ok", service_tag="GOOD1",
        )
        await cache.upsert_device(
            site_id=site.id, agent_id="new-device", vendor="lenovo",
            model="SR650", health="ok", service_tag="NEW1",
        )
        for i in range(8):
            session.add(CCOutcomeHistory(
                site_id=site.id, action_id=f"a{i}", action_type="FAN_RESET",
                device_agent_id="bad-device", vendor="dell", model="R750",
                outcome="FAILURE" if i < 6 else "SUCCESS",
            ))
            session.add(CCOutcomeHistory(
                site_id=site.id, action_id=f"b{i}", action_type="FAN_RESET",
                device_agent_id="good-device", vendor="dell", model="R750",
                outcome="SUCCESS",
            ))
        await WarrantyRepo(session).upsert_records([
            WarrantyRecord("BAD1", "dell", end_date="2024-01-01"),  # expired
        ])
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await engine.dispose()


class TestPredictiveAPI:
    async def test_riskiest_device_first(self, client):
        r = await client.get("/api/predictive/risk")
        assert r.status_code == 200
        data = r.json()
        assert data["devices_scored"] == 3
        risks = data["risks"]
        assert risks[0]["agent_id"] == "bad-device"
        assert risks[0]["band"] == "high"
        assert risks[0]["factors"]["health_bump"] == 0.20
        assert risks[0]["factors"]["warranty_expired_bump"] == 0.10
        good = next(x for x in risks if x["agent_id"] == "good-device")
        assert good["band"] == "low"

    async def test_new_device_insufficient_data(self, client):
        r = await client.get("/api/predictive/risk")
        new = next(x for x in r.json()["risks"]
                   if x["agent_id"] == "new-device")
        # No history and no (lenovo, SR650) cohort -> insufficient
        assert new["band"] == "insufficient_data"

    async def test_band_filter(self, client):
        r = await client.get("/api/predictive/risk", params={"band": "high"})
        risks = r.json()["risks"]
        assert len(risks) == 1
        assert risks[0]["agent_id"] == "bad-device"
