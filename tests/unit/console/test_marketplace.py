"""Skill marketplace tests (R4-3 P17, OQ-22)."""

from __future__ import annotations

import httpx
import pytest

from harkeniq_console.app import create_app
from harkeniq_console.config import ConsoleConfig
from harkeniq_console.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_console.db.repos import AuditRepo, MarketplaceRepo
from harkeniq_console.marketplace import (
    PROMOTION_MIN_DEVICES,
    PROMOTION_MIN_EXECUTIONS,
    check_promotion,
    validate_submission,
)
from harkeniq_console.runtime import AppState

VALID_SKILL = """\
name: community-fan-watch
version: 1
target: fan
description: Community-contributed fan degradation detector
rules:
  - condition: "health == 'Warning'"
    verdict: WARNING
    message: "Fan {name} degraded (community skill)"
default_verdict: HEALTHY
"""

DANGEROUS_SKILL = """\
name: community-bmc-kicker
version: 1
target: thermal
description: Proposes a BMC reset
rules:
  - condition: "health == 'Critical'"
    verdict: CRITICAL
    message: "Thermal critical"
    action:
      type: BMC_RESET
default_verdict: HEALTHY
"""


def _named(yaml_text: str, name: str) -> str:
    return yaml_text.replace("community-fan-watch", name)


class TestValidation:
    def test_valid_skill_passes(self):
        v = validate_submission(VALID_SKILL)
        assert v.passed is True
        assert v.skill_name == "community-fan-watch"
        assert v.target == "fan"
        assert v.warnings == []

    def test_invalid_yaml_fails(self):
        v = validate_submission("::: not yaml {{{")
        assert v.passed is False

    def test_unknown_field_fails(self):
        v = validate_submission(VALID_SKILL + "\nbackdoor: true\n")
        assert v.passed is False

    def test_bad_condition_field_fails(self):
        bad = VALID_SKILL.replace("health == 'Warning'", "shell_exec == 'x'")
        v = validate_submission(bad)
        assert v.passed is False

    def test_dangerous_action_warns_but_passes(self):
        v = validate_submission(DANGEROUS_SKILL)
        assert v.passed is True
        assert any("BMC_RESET" in w for w in v.warnings)


class TestPromotionGate:
    def test_zero_executions_never_eligible(self):
        # SkillOutcomeStats.success_rate defaults to 1.0 on no data --
        # the gate must not fall for that.
        gate = check_promotion(0, 0, 0)
        assert gate.eligible is False

    def test_meets_gate(self):
        gate = check_promotion(100, 96, 60)
        assert gate.eligible is True
        assert gate.success_rate == 0.96

    def test_low_success_rate_fails(self):
        gate = check_promotion(100, 90, 60)
        assert gate.eligible is False
        assert any("success rate" in r for r in gate.reasons)

    def test_too_few_devices_fails(self):
        gate = check_promotion(100, 100, PROMOTION_MIN_DEVICES - 1)
        assert gate.eligible is False

    def test_too_few_executions_fails(self):
        gate = check_promotion(PROMOTION_MIN_EXECUTIONS - 1, 40, 60)
        assert gate.eligible is False


class TestMarketplaceRepo:
    async def test_submit_and_review_flow(self, session):
        repo = MarketplaceRepo(session)
        entry = await repo.submit(
            "s1", 1, VALID_SKILL, author_email="dev@x.com",
            target="fan",
        )
        assert entry.tier == "community"
        assert entry.review_status == "submitted"
        assert entry.published is False

        await repo.review(entry, approve=True, reviewer_email="rev@x.com")
        assert entry.review_status == "approved"
        assert entry.published is True

    async def test_stats_accumulate_and_devices_high_water(self, session):
        repo = MarketplaceRepo(session)
        entry = await repo.submit("s2", 1, VALID_SKILL)
        await repo.record_stats(entry, successes=30, failures=2, devices=40)
        await repo.record_stats(entry, successes=40, failures=1, devices=55)
        assert entry.total_executions == 73
        assert entry.success_count == 70
        assert entry.device_count == 55  # high-water, not sum


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
        c.sessionmaker = sm
        yield c
    await engine.dispose()


async def _submit(client, name: str) -> dict:
    r = await client.post("/api/marketplace/skills",
                          json={"yaml_content": _named(VALID_SKILL, name)})
    assert r.status_code == 200, r.text
    return r.json()


class TestMarketplaceAPI:
    async def test_submit_review_install_flow(self, client):
        entry = await _submit(client, "flow-skill")
        assert entry["tier"] == "community"

        # Not yet published: browse is empty
        r = await client.get("/api/marketplace/skills")
        assert r.json()["total"] == 0

        # Approve
        r = await client.post(
            f"/api/admin/marketplace/skills/{entry['id']}/review",
            json={"approve": True},
        )
        assert r.status_code == 200
        assert r.json()["published"] is True

        # Now browsable + installable
        r = await client.get("/api/marketplace/skills")
        assert r.json()["total"] == 1
        r = await client.post(
            f"/api/marketplace/skills/{entry['id']}/install"
        )
        assert r.status_code == 200
        assert "yaml_content" in r.json()
        assert r.json()["install_count"] == 1

    async def test_invalid_submission_rejected(self, client):
        r = await client.post("/api/marketplace/skills",
                              json={"yaml_content": "not: [valid"})
        assert r.status_code == 422

    async def test_duplicate_submission_conflict(self, client):
        await _submit(client, "dup-skill")
        r = await client.post(
            "/api/marketplace/skills",
            json={"yaml_content": _named(VALID_SKILL, "dup-skill")},
        )
        assert r.status_code == 409

    async def test_reject_requires_reason(self, client):
        entry = await _submit(client, "rej-skill")
        r = await client.post(
            f"/api/admin/marketplace/skills/{entry['id']}/review",
            json={"approve": False},
        )
        assert r.status_code == 400
        r = await client.post(
            f"/api/admin/marketplace/skills/{entry['id']}/review",
            json={"approve": False, "reason": "too broad"},
        )
        assert r.status_code == 200
        assert r.json()["review_status"] == "rejected"
        assert r.json()["published"] is False

    async def test_unpublished_not_installable(self, client):
        entry = await _submit(client, "unpub-skill")
        r = await client.post(
            f"/api/marketplace/skills/{entry['id']}/install"
        )
        assert r.status_code == 404

    async def test_promotion_gate_enforced(self, client):
        entry = await _submit(client, "promo-skill")
        await client.post(
            f"/api/admin/marketplace/skills/{entry['id']}/review",
            json={"approve": True},
        )
        # Below gate
        r = await client.post(
            f"/api/admin/marketplace/skills/{entry['id']}/promote"
        )
        assert r.status_code == 422

        # Feed stats past the gate
        r = await client.post(
            f"/api/admin/marketplace/skills/{entry['id']}/stats",
            json={"successes": 96, "failures": 4, "devices": 60},
        )
        assert r.json()["promotion"]["eligible"] is True

        r = await client.post(
            f"/api/admin/marketplace/skills/{entry['id']}/promote"
        )
        assert r.status_code == 200
        assert r.json()["tier"] == "verified"

        # Idempotence: already verified -> 409
        r = await client.post(
            f"/api/admin/marketplace/skills/{entry['id']}/promote"
        )
        assert r.status_code == 409

    async def test_review_queue_lists_submissions(self, client):
        await _submit(client, "queue-a")
        await _submit(client, "queue-b")
        r = await client.get("/api/admin/marketplace/skills")
        assert r.json()["total"] == 2
        assert all(e["review_status"] == "submitted"
                   for e in r.json()["items"])

    async def test_marketplace_actions_audited_on_chain(self, client):
        entry = await _submit(client, "audit-skill")
        await client.post(
            f"/api/admin/marketplace/skills/{entry['id']}/review",
            json={"approve": True},
        )
        async with client.sessionmaker() as session:
            repo = AuditRepo(session)
            result = await repo.verify_chain()
            assert result.valid is True
            assert result.length >= 2  # submit + approve
