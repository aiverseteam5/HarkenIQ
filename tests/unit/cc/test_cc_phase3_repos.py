"""Phase 3 repository tests: ApprovalPolicyRepo, ApprovalGroupRepo, AutonomyBudgetRepo."""

from __future__ import annotations

import pytest

from harkeniq_cc.db.repos import (
    ApprovalGroupRepo,
    ApprovalPolicyRepo,
    AutonomyBudgetRepo,
)

TENANT = "tenant-test"


# ---------------------------------------------------------------------------
# ApprovalPolicyRepo
# ---------------------------------------------------------------------------

class TestApprovalPolicyRepo:
    async def test_create(self, session):
        repo = ApprovalPolicyRepo(session)
        policy = await repo.create(
            TENANT, "fan-policy", created_by="admin",
            device_type="Dell", action_type="fan_boost",
            risk_level="low", approval_mode="require_approval",
            required_approvers=1,
        )
        assert policy.tenant_id == TENANT
        assert policy.name == "fan-policy"
        assert policy.device_type == "Dell"
        assert policy.status == "active"

    async def test_list_by_tenant(self, session):
        repo = ApprovalPolicyRepo(session)
        await repo.create(TENANT, "policy-a", created_by="admin")
        await repo.create(TENANT, "policy-b", created_by="admin")
        await repo.create("other-tenant", "policy-c", created_by="admin")
        policies = await repo.list_all(TENANT)
        assert len(policies) == 2
        names = [p.name for p in policies]
        assert "policy-a" in names
        assert "policy-b" in names

    async def test_get_by_id(self, session):
        repo = ApprovalPolicyRepo(session)
        policy = await repo.create(TENANT, "get-test", created_by="admin")
        found = await repo.get_by_id(policy.id)
        assert found is not None
        assert found.name == "get-test"

    async def test_get_by_id_not_found(self, session):
        repo = ApprovalPolicyRepo(session)
        found = await repo.get_by_id("nonexistent")
        assert found is None

    async def test_update(self, session):
        repo = ApprovalPolicyRepo(session)
        policy = await repo.create(
            TENANT, "update-test", created_by="admin", risk_level="low",
        )
        await repo.update(policy, risk_level="high", required_approvers=3)
        assert policy.risk_level == "high"
        assert policy.required_approvers == 3

    async def test_delete(self, session):
        repo = ApprovalPolicyRepo(session)
        policy = await repo.create(TENANT, "delete-me", created_by="admin")
        policy_id = policy.id
        await repo.delete(policy)
        assert await repo.get_by_id(policy_id) is None


# ---------------------------------------------------------------------------
# ApprovalGroupRepo
# ---------------------------------------------------------------------------

class TestApprovalGroupRepo:
    async def test_create(self, session):
        repo = ApprovalGroupRepo(session)
        group = await repo.create(
            TENANT, "ops-team", created_by="admin",
            slack_channel="#ops", github_team="org/ops",
            required_count=2,
        )
        assert group.tenant_id == TENANT
        assert group.name == "ops-team"
        assert group.required_count == 2

    async def test_list_by_tenant(self, session):
        repo = ApprovalGroupRepo(session)
        await repo.create(TENANT, "group-a", created_by="admin")
        await repo.create(TENANT, "group-b", created_by="admin")
        await repo.create("other-tenant", "group-c", created_by="admin")
        groups = await repo.list_all(TENANT)
        assert len(groups) == 2

    async def test_get_by_id(self, session):
        repo = ApprovalGroupRepo(session)
        group = await repo.create(TENANT, "get-test-grp", created_by="admin")
        found = await repo.get_by_id(group.id)
        assert found is not None
        assert found.name == "get-test-grp"

    async def test_get_by_id_not_found(self, session):
        repo = ApprovalGroupRepo(session)
        found = await repo.get_by_id("nonexistent")
        assert found is None

    async def test_update(self, session):
        repo = ApprovalGroupRepo(session)
        group = await repo.create(
            TENANT, "upd-grp", created_by="admin", slack_channel="#old",
        )
        await repo.update(group, slack_channel="#new", required_count=5)
        assert group.slack_channel == "#new"
        assert group.required_count == 5

    async def test_delete(self, session):
        repo = ApprovalGroupRepo(session)
        group = await repo.create(TENANT, "del-grp", created_by="admin")
        group_id = group.id
        await repo.delete(group)
        assert await repo.get_by_id(group_id) is None

    async def test_delete_cascades_members(self, session):
        repo = ApprovalGroupRepo(session)
        group = await repo.create(TENANT, "cascade-grp", created_by="admin")
        await repo.add_member(group.id, "alice@lab")
        await repo.add_member(group.id, "bob@lab")
        members_before = await repo.list_members(group.id)
        assert len(members_before) == 2

        await repo.delete(group)
        # Members table entries should be gone
        members_after = await repo.list_members(group.id)
        assert len(members_after) == 0


# ---------------------------------------------------------------------------
# AutonomyBudgetRepo
# ---------------------------------------------------------------------------

class TestAutonomyBudgetRepo:
    async def test_create(self, session):
        repo = AutonomyBudgetRepo(session)
        budget = await repo.upsert(
            TENANT, device_type="Dell", level=1,
            budget_limit=20, budget_period="weekly",
        )
        assert budget.tenant_id == TENANT
        assert budget.device_type == "Dell"
        assert budget.level == 1
        assert budget.budget_limit == 20
        assert budget.actions_used == 0

    async def test_upsert_updates_existing(self, session):
        repo = AutonomyBudgetRepo(session)
        budget1 = await repo.upsert(TENANT, device_type="HPE", level=0, budget_limit=5)
        budget2 = await repo.upsert(TENANT, device_type="HPE", level=2, budget_limit=50)
        assert budget1.id == budget2.id
        assert budget2.level == 2
        assert budget2.budget_limit == 50

    async def test_list_by_tenant(self, session):
        repo = AutonomyBudgetRepo(session)
        await repo.upsert(TENANT, device_type="Dell", level=0, budget_limit=10)
        await repo.upsert(TENANT, device_type="HPE", level=1, budget_limit=20)
        await repo.upsert("other-tenant", device_type="*", level=0, budget_limit=5)
        budgets = await repo.list_all(TENANT)
        assert len(budgets) == 2

    async def test_get_by_id(self, session):
        repo = AutonomyBudgetRepo(session)
        budget = await repo.upsert(TENANT, device_type="*", level=0, budget_limit=10)
        found = await repo.get_by_id(budget.id)
        assert found is not None
        assert found.device_type == "*"

    async def test_delete(self, session):
        repo = AutonomyBudgetRepo(session)
        budget = await repo.upsert(TENANT, device_type="del-type", level=0)
        budget_id = budget.id
        await repo.delete(budget)
        assert await repo.get_by_id(budget_id) is None
