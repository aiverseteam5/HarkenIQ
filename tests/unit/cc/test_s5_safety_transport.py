"""S5: safety state travels SM -> CC over the real wire.

Before S5 the suppression engine and the error budgets lived only inside
the Site Manager, behind its site-token break-glass API — so the tenant
operator could not see that a class had already been demoted, and
neither could any future agent.

Tested over a REAL SM servicer on a real port, deliberately: the QA-042
lesson is that a proto field decoded nowhere is a feed that silently
carries nothing, and direct-servicer tests never touch the client's
proto->dict layer where that bug lives.
"""

from __future__ import annotations

import grpc
import pytest

from harkeniq.proto import harkeniq_pb2_grpc
from harkeniq_cc.db.base import (
    create_all as cc_create_all,
    make_engine as cc_make_engine,
    make_sessionmaker as cc_make_sessionmaker,
)
from harkeniq_cc.db.repos import SafetyStateRepo
from harkeniq_cc.sm_client import SMClient
from harkeniq_sm.approvals import ApprovalService
from harkeniq_sm.autonomy import SMAutonomyEnforcer
from harkeniq_sm.config import SMConfig
from harkeniq_sm.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_sm.db.models import Site
from harkeniq_sm.db.repos import ErrorBudgetRepo
from harkeniq_sm.grpc_server import SiteManagerServiceServicer
from harkeniq_sm.knowledge import MIN_OUTCOMES_TO_JUDGE
from harkeniq_sm.suppression import SuppressionEngine, SuppressionState


async def _sm_stack(*, with_safety: bool = True):
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    db = make_sessionmaker(engine)
    async with db() as session:
        session.add(Site(name="site-1"))
        if with_safety:
            repo = ErrorBudgetRepo(session)
            for _ in range(MIN_OUTCOMES_TO_JUDGE):
                await repo.record("SEL_CLEAR", "FAILURE")
            for _ in range(MIN_OUTCOMES_TO_JUDGE):
                await repo.record("BMC_RESET", "SUCCESS")
        await session.commit()

    config = SMConfig(insecure=True, site_name="site-1")
    enforcer = suppression = None
    if with_safety:
        enforcer = SMAutonomyEnforcer()
        enforcer.update_policy([{
            "action_type": "SEL_CLEAR", "max_per_window": 5,
            "window_seconds": 3600, "risk_level": "low",
        }])
        suppression = SuppressionEngine()
        # Reach into the engine's active map directly: this test is about
        # TRANSPORT, and driving a real correlation storm here would test
        # the suppression engine instead (it has its own tests).
        suppression._active["rack-3"] = SuppressionState(
            domain_id="rack-3", domain_kind="rack", event_family="power",
            trigger_reason="direct_dependency", device_count=4,
            triggered_at=1_700_000_000.0,
        )
    servicer = SiteManagerServiceServicer(
        db, ApprovalService(db, config), config,
        autonomy=enforcer, suppression=suppression,
    )
    server = grpc.aio.server()
    harkeniq_pb2_grpc.add_SiteManagerServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    return f"127.0.0.1:{port}", server, engine


@pytest.mark.asyncio
async def test_safety_state_reaches_the_client_dict():
    endpoint, server, engine = await _sm_stack()
    try:
        snapshot = await SMClient().get_fleet_snapshot(
            endpoint, "any-token", "t1", "",
        )
        safety = snapshot["safety"]
        assert safety["reported"] is True
        assert safety["as_of_unix"] > 0

        budgets = {b["action_type"]: b for b in safety["error_budgets"]}
        assert budgets["SEL_CLEAR"]["dropped_back"] is True
        assert budgets["SEL_CLEAR"]["total_count"] == MIN_OUTCOMES_TO_JUDGE
        assert budgets["BMC_RESET"]["dropped_back"] is False

        assert len(safety["suppressions"]) == 1
        assert safety["suppressions"][0]["domain_id"] == "rack-3"
        assert safety["suppressions"][0]["trigger_reason"] == "direct_dependency"

        assert safety["site_budgets"]["SEL_CLEAR"] == 5
    finally:
        await server.stop(grace=None)
        await engine.dispose()


@pytest.mark.asyncio
async def test_an_sm_with_no_safety_machinery_reports_unknown_not_safe():
    """The one direction a governance input may never err."""
    endpoint, server, engine = await _sm_stack(with_safety=False)
    try:
        snapshot = await SMClient().get_fleet_snapshot(
            endpoint, "any-token", "t1", "",
        )
        safety = snapshot["safety"]
        # `reported` is True (the SM answered) but it vouches for nothing.
        assert safety["error_budgets"] == []
        assert safety["suppressions"] == []
        assert safety["sm_stop_switch"] is False
    finally:
        await server.stop(grace=None)
        await engine.dispose()


@pytest.mark.asyncio
async def test_cc_persists_what_it_received():
    endpoint, server, engine = await _sm_stack()
    cc_engine = cc_make_engine("sqlite+aiosqlite:///:memory:")
    await cc_create_all(cc_engine)
    cc_db = cc_make_sessionmaker(cc_engine)
    try:
        snapshot = await SMClient().get_fleet_snapshot(
            endpoint, "any-token", "t1", "",
        )
        async with cc_db() as session:
            await SafetyStateRepo(session).upsert("t1", "site-a", snapshot["safety"])
            await session.commit()
        async with cc_db() as session:
            rows = await SafetyStateRepo(session).list_for_tenant("t1")
            assert len(rows) == 1
            assert rows[0].reported is True
            assert rows[0].error_budgets[0]["action_type"] in ("SEL_CLEAR", "BMC_RESET")
            assert rows[0].suppressions[0]["domain_id"] == "rack-3"
    finally:
        await server.stop(grace=None)
        await engine.dispose()
        await cc_engine.dispose()


@pytest.mark.asyncio
async def test_a_poll_with_no_safety_replaces_the_stale_row():
    """A stale reading must never pass for a current one."""
    cc_engine = cc_make_engine("sqlite+aiosqlite:///:memory:")
    await cc_create_all(cc_engine)
    cc_db = cc_make_sessionmaker(cc_engine)
    try:
        async with cc_db() as session:
            repo = SafetyStateRepo(session)
            await repo.upsert("t1", "site-a", {
                "reported": True, "as_of_unix": 1_700_000_000,
                "error_budgets": [{"action_type": "SEL_CLEAR", "dropped_back": True}],
                "suppressions": [], "site_budgets": {},
            })
            await session.commit()
        async with cc_db() as session:
            await SafetyStateRepo(session).upsert(
                "t1", "site-a", {"reported": False},
            )
            await session.commit()
        async with cc_db() as session:
            row = (await SafetyStateRepo(session).list_for_tenant("t1"))[0]
            assert row.reported is False
            assert row.error_budgets == []
    finally:
        await cc_engine.dispose()
