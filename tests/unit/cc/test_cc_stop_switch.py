"""QA-022 tests: persisted stop switch + CC->SM policy push.

The stop switch was an in-process dict with zero readers; it now lives
in cc_stop_switch, survives restarts, and is pushed to Site Managers as
PushPolicy autonomy_budgets_json (which the SM applies to its enforcer
and threads into every lease).
"""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from harkeniq_cc.app import create_app
from harkeniq_cc.auth import configure_auth
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.repos import AutonomyBudgetRepo, SiteRepo, StopSwitchRepo
from harkeniq_cc.policy_push import (
    budget_row_to_policies,
    build_autonomy_payload,
    push_policy_to_all_sites,
)
from harkeniq_cc.runtime import AppState

TENANT = "test-tenant"


@pytest.fixture
async def env():
    config = CCConfig(tenant_id=TENANT, insecure=True)
    configure_auth("", "", "", insecure=True)
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sessionmaker = make_sessionmaker(engine)
    state = AppState(config=config, engine=engine, sessionmaker=sessionmaker)
    app = create_app(state)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield {"client": c, "state": state, "sessionmaker": sessionmaker,
               "config": config}
    await engine.dispose()


class TestPersistence:
    async def test_flip_persists_in_db(self, env):
        r = await env["client"].post("/api/policies/stop-switch")
        assert r.status_code == 200
        assert r.json()["stop_switch"] is True

        # State survives a fresh repo read (i.e. it is in the DB, not a dict)
        async with env["sessionmaker"]() as session:
            assert await StopSwitchRepo(session).is_active(TENANT) is True

        r = await env["client"].get("/api/policies/stop-switch")
        assert r.json()["stop_switch"] is True

        r = await env["client"].post("/api/policies/stop-switch/deactivate")
        assert r.status_code == 200
        async with env["sessionmaker"]() as session:
            assert await StopSwitchRepo(session).is_active(TENANT) is False

    async def test_default_inactive(self, env):
        r = await env["client"].get("/api/policies/stop-switch")
        assert r.status_code == 200
        assert r.json()["stop_switch"] is False


class TestBudgetMapping:
    def _budget(self, level, limit=5, period="daily"):
        class Row:
            pass
        row = Row()
        row.level = level
        row.budget_limit = limit
        row.budget_period = period
        return row

    def test_observe_and_suggest_grant_nothing(self):
        assert budget_row_to_policies(self._budget(0)) == []
        assert budget_row_to_policies(self._budget(1)) == []

    def test_level_2_grants_low_risk_only(self):
        policies = budget_row_to_policies(self._budget(2))
        actions = {p["action_type"] for p in policies}
        assert actions == {"SEL_CLEAR", "BMC_RESET"}
        assert all(p["risk_level"] == "low" for p in policies)
        assert all(p["max_per_window"] == 5 for p in policies)
        assert all(p["window_seconds"] == 86400 for p in policies)

    def test_level_3_adds_medium_never_high(self):
        policies = budget_row_to_policies(self._budget(3, period="hourly"))
        actions = {p["action_type"] for p in policies}
        assert "POWER_CYCLE" in actions
        assert "POWER_CAP_ADJUST" in actions
        assert "CONFIG_RESTORE" in actions
        # High-risk actions keep their dedicated approval paths
        assert "FIRMWARE_UPDATE" not in actions
        assert "INTERFACE_DISABLE" not in actions


class TestPayload:
    async def test_payload_carries_stop_switch_and_policies(self, env):
        async with env["sessionmaker"]() as session:
            await StopSwitchRepo(session).set(TENANT, True, "admin@x")
            await AutonomyBudgetRepo(session).upsert(
                TENANT, device_type="*", level=2,
                budget_limit=3, budget_period="daily",
            )
            # Device-scoped budgets are skipped (SM has no device dimension)
            await AutonomyBudgetRepo(session).upsert(
                TENANT, device_type="Dell R750", level=3,
                budget_limit=99, budget_period="daily",
            )
            await session.commit()

        async with env["sessionmaker"]() as session:
            payload = json.loads(await build_autonomy_payload(session, TENANT))
        assert payload["stop_switch"] is True
        assert payload["stop_switch_by"] == "admin@x"
        actions = {p["action_type"] for p in payload["policies"]}
        assert actions == {"SEL_CLEAR", "BMC_RESET"}
        assert all(p["max_per_window"] == 3 for p in payload["policies"])


class _FakeSMClient:
    def __init__(self):
        self.pushes = []

    async def push_policy(self, endpoint, token, tenant_id, site_id,
                          autonomy_budgets_json="", approval_policies_json=""):
        self.pushes.append({
            "endpoint": endpoint, "site_id": site_id,
            "payload": json.loads(autonomy_budgets_json),
        })
        return {"accepted": True, "reason": ""}


class TestPushToSites:
    async def test_pushes_to_every_registered_site(self, env):
        async with env["sessionmaker"]() as session:
            repo = SiteRepo(session)
            await repo.upsert(TENANT, "site-a", "sm-a:50051", sm_token="t-a")
            await repo.upsert(TENANT, "site-b", "sm-b:50051", sm_token="t-b")
            await StopSwitchRepo(session).set(TENANT, True, "admin@x")
            await session.commit()

        fake = _FakeSMClient()
        pushed = await push_policy_to_all_sites(
            env["config"], env["sessionmaker"], client=fake
        )
        assert pushed == 2
        assert {p["endpoint"] for p in fake.pushes} == {"sm-a:50051", "sm-b:50051"}
        assert all(p["payload"]["stop_switch"] is True for p in fake.pushes)
