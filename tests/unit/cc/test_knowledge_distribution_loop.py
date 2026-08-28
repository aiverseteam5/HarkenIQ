"""QA-033: knowledge distribution wired into the intelligence loop.

The KnowledgeDistributor classes were tested in isolation since R3b-3;
these tests cover the actual wiring: CC patterns -> scope-matched sites
-> PushPolicy learned_patterns_json.
"""

from __future__ import annotations

import json
import time

import pytest

from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.repos import FleetCacheRepo, FleetPatternRepo, SiteRepo
from harkeniq_cc.knowledge_distributor import (
    KnowledgeDistributor,
    distribute_patterns,
)
from harkeniq_cc.pattern_detector import FleetPattern

TENANT = "test-tenant"


class _FakeSMClient:
    def __init__(self, accept=True):
        self.accept = accept
        self.pushes: list[dict] = []

    async def push_policy(self, endpoint, token, tenant_id, site_id,
                          autonomy_budgets_json="", approval_policies_json="",
                          learned_patterns_json=""):
        self.pushes.append({
            "site_id": site_id,
            "patterns": json.loads(learned_patterns_json),
        })
        return {"accepted": self.accept, "reason": ""}


@pytest.fixture
async def env():
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sessionmaker = make_sessionmaker(engine)
    config = CCConfig(tenant_id=TENANT, insecure=True)

    async with sessionmaker() as session:
        site_repo = SiteRepo(session)
        dell_site = await site_repo.upsert(
            TENANT, "dell-site", "sm-a:50051", sm_token="t-a")
        hpe_site = await site_repo.upsert(
            TENANT, "hpe-site", "sm-b:50051", sm_token="t-b")
        cache = FleetCacheRepo(session)
        await cache.upsert_device(
            site_id=dell_site.id, agent_id="a1", vendor="Dell", model="R750")
        await cache.upsert_device(
            site_id=hpe_site.id, agent_id="a2", vendor="HPE", model="DL380")
        pattern = FleetPattern(
            pattern_id="pat-1", pattern_type="batch_failure",
            description="Dell R750 PSU batch failing",
            affected_scope={"vendor": "Dell", "model": "R750"},
            confidence=0.9, detected_at=time.time(),
            evidence={"failures": 7},
        )
        await FleetPatternRepo(session).save(pattern, tenant_id=TENANT)
        await session.commit()
        ids = {"dell": dell_site.id, "hpe": hpe_site.id}

    yield {"config": config, "sessionmaker": sessionmaker, "ids": ids}
    await engine.dispose()


class TestDistributePatterns:
    async def test_scope_matched_site_receives_pattern(self, env):
        client = _FakeSMClient()
        delivered = await distribute_patterns(
            env["config"], env["sessionmaker"], client=client,
        )
        assert delivered == 1
        assert len(client.pushes) == 1
        push = client.pushes[0]
        assert push["site_id"] == env["ids"]["dell"]  # HPE site skipped
        assert push["patterns"][0]["pattern_id"] == "pat-1"
        assert push["patterns"][0]["affected_scope"]["vendor"] == "Dell"

    async def test_dedup_across_cycles(self, env):
        client = _FakeSMClient()
        distributor = KnowledgeDistributor()
        first = await distribute_patterns(
            env["config"], env["sessionmaker"],
            client=client, distributor=distributor,
        )
        second = await distribute_patterns(
            env["config"], env["sessionmaker"],
            client=client, distributor=distributor,
        )
        assert first == 1
        assert second == 0  # already delivered, not re-pushed
        assert len(client.pushes) == 1

    async def test_failed_push_retries_next_cycle(self, env):
        distributor = KnowledgeDistributor()
        failing = _FakeSMClient(accept=False)
        delivered = await distribute_patterns(
            env["config"], env["sessionmaker"],
            client=failing, distributor=distributor,
        )
        assert delivered == 0
        working = _FakeSMClient()
        delivered = await distribute_patterns(
            env["config"], env["sessionmaker"],
            client=working, distributor=distributor,
        )
        assert delivered == 1

    async def test_no_patterns_no_pushes(self, env):
        engine2 = make_engine("sqlite+aiosqlite:///:memory:")
        await create_all(engine2)
        try:
            client = _FakeSMClient()
            delivered = await distribute_patterns(
                env["config"], make_sessionmaker(engine2), client=client,
            )
            assert delivered == 0
            assert client.pushes == []
        finally:
            await engine2.dispose()
