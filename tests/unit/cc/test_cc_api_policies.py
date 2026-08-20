"""Policies API endpoint tests: approval policies, groups, autonomy budgets."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from harkeniq_cc.app import create_app
from harkeniq_cc.auth import configure_auth
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.repos import ApprovalGroupRepo, ApprovalPolicyRepo, AutonomyBudgetRepo
from harkeniq_cc.runtime import AppState

TENANT = "test-tenant"


@pytest.fixture
async def client():
    """FastAPI test client with seeded policies, groups, and budgets."""
    config = CCConfig(tenant_id=TENANT, insecure=True)
    configure_auth("", "", "", insecure=True)
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sessionmaker = make_sessionmaker(engine)
    state = AppState(config=config, engine=engine, sessionmaker=sessionmaker)
    app = create_app(state)

    async with sessionmaker() as session:
        # Seed a group
        group = await ApprovalGroupRepo(session).create(
            TENANT, "ops-team", created_by="admin",
            slack_channel="#ops", github_team="org/ops",
        )
        # Seed a policy
        await ApprovalPolicyRepo(session).create(
            TENANT, "default-policy", created_by="admin",
            device_type="*", action_type="*", risk_level="medium",
            approval_mode="require_approval", required_approvers=1,
            group_id=group.id,
        )
        # Seed a budget
        await AutonomyBudgetRepo(session).upsert(
            TENANT, device_type="*", level=0,
            budget_limit=10, budget_period="monthly",
        )
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await engine.dispose()


# ---------------------------------------------------------------------------
# Approval Policies
# ---------------------------------------------------------------------------

class TestPolicies:
    async def test_create_policy(self, client):
        r = await client.post(
            "/api/policies/",
            json={
                "name": "high-risk-policy",
                "device_type": "Dell",
                "action_type": "bmc_reset",
                "risk_level": "high",
                "approval_mode": "require_approval",
                "required_approvers": 2,
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["policy"]["name"] == "high-risk-policy"
        assert data["policy"]["risk_level"] == "high"
        assert data["policy"]["required_approvers"] == 2

    async def test_list_policies(self, client):
        r = await client.get("/api/policies/")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] >= 1
        assert any(p["name"] == "default-policy" for p in data["policies"])

    async def test_update_policy(self, client):
        # Get existing policy
        list_r = await client.get("/api/policies/")
        policy_id = list_r.json()["policies"][0]["id"]

        r = await client.patch(
            f"/api/policies/{policy_id}",
            json={"risk_level": "high", "required_approvers": 3},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["policy"]["risk_level"] == "high"
        assert data["policy"]["required_approvers"] == 3

    async def test_delete_policy(self, client):
        # Create then delete
        create_r = await client.post(
            "/api/policies/",
            json={"name": "to-delete"},
        )
        policy_id = create_r.json()["policy"]["id"]

        r = await client.delete(f"/api/policies/{policy_id}")
        assert r.status_code == 200
        assert r.json()["deleted"] is True

        # Verify gone
        list_r = await client.get("/api/policies/")
        ids = [p["id"] for p in list_r.json()["policies"]]
        assert policy_id not in ids

    async def test_update_nonexistent_policy(self, client):
        r = await client.patch(
            "/api/policies/nonexistent",
            json={"risk_level": "low"},
        )
        assert r.status_code == 404

    async def test_delete_nonexistent_policy(self, client):
        r = await client.delete("/api/policies/nonexistent")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Approval Groups
# ---------------------------------------------------------------------------

class TestGroups:
    async def test_create_group(self, client):
        r = await client.post(
            "/api/policies/groups",
            json={
                "name": "sre-team",
                "slack_channel": "#sre",
                "github_team": "org/sre",
                "required_count": 2,
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["group"]["name"] == "sre-team"
        assert data["group"]["required_count"] == 2

    async def test_list_groups(self, client):
        r = await client.get("/api/policies/groups")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] >= 1
        assert any(g["name"] == "ops-team" for g in data["groups"])

    async def test_update_group(self, client):
        list_r = await client.get("/api/policies/groups")
        group_id = list_r.json()["groups"][0]["id"]

        r = await client.patch(
            f"/api/policies/groups/{group_id}",
            json={"slack_channel": "#ops-updated", "required_count": 3},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["group"]["slack_channel"] == "#ops-updated"
        assert data["group"]["required_count"] == 3

    async def test_delete_group(self, client):
        create_r = await client.post(
            "/api/policies/groups",
            json={"name": "temp-group"},
        )
        group_id = create_r.json()["group"]["id"]

        r = await client.delete(f"/api/policies/groups/{group_id}")
        assert r.status_code == 200
        assert r.json()["deleted"] is True

    async def test_update_nonexistent_group(self, client):
        r = await client.patch(
            "/api/policies/groups/nonexistent",
            json={"name": "nope"},
        )
        assert r.status_code == 404

    async def test_delete_nonexistent_group(self, client):
        r = await client.delete("/api/policies/groups/nonexistent")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Autonomy Budgets
# ---------------------------------------------------------------------------

class TestAutonomyBudgets:
    async def test_create_autonomy_budget(self, client):
        r = await client.post(
            "/api/policies/autonomy",
            json={
                "device_type": "Dell",
                "level": 1,
                "budget_limit": 20,
                "budget_period": "weekly",
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["budget"]["device_type"] == "Dell"
        assert data["budget"]["level"] == 1
        assert data["budget"]["budget_limit"] == 20

    async def test_list_autonomy_budgets(self, client):
        r = await client.get("/api/policies/autonomy")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] >= 1
        assert any(b["device_type"] == "*" for b in data["budgets"])

    async def test_upsert_autonomy_budget(self, client):
        """POST same device_type updates the existing budget."""
        r = await client.post(
            "/api/policies/autonomy",
            json={
                "device_type": "*",
                "level": 2,
                "budget_limit": 50,
                "budget_period": "monthly",
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["budget"]["level"] == 2
        assert data["budget"]["budget_limit"] == 50

    async def test_delete_autonomy_budget(self, client):
        # Create then delete
        create_r = await client.post(
            "/api/policies/autonomy",
            json={"device_type": "HPE", "level": 0, "budget_limit": 5},
        )
        budget_id = create_r.json()["budget"]["id"]

        r = await client.delete(f"/api/policies/autonomy/{budget_id}")
        assert r.status_code == 200
        assert r.json()["deleted"] is True

    async def test_delete_nonexistent_budget(self, client):
        r = await client.delete("/api/policies/autonomy/nonexistent")
        assert r.status_code == 404
