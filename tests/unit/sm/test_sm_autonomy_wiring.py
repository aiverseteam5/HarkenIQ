"""QA-021 wiring tests: the R3a autonomy chain, finally constructed.

Covers: SM keypair persistence (sm_settings), make_state building the
identity/enforcer/suppression trio, Heartbeat leases carrying real
budgets + stop switch + suppression domains, correlation-driven
suppression, and the /api/autonomy HTTP surface.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from harkeniq.proto import harkeniq_pb2
from harkeniq_sm.agent_identity import (
    AgentIdentityService,
    load_or_generate_sm_keypair,
)
from harkeniq_sm.autonomy import SMAutonomyEnforcer
from harkeniq_sm.config import SMConfig
from harkeniq_sm.grpc_server import AgentServiceServicer
from harkeniq_sm.ingest import IngestService
from harkeniq_sm.suppression import SuppressionEngine


def _config(**overrides):
    defaults = dict(insecure=True, site_token="")
    defaults.update(overrides)
    return SMConfig(**defaults)


class TestKeypairPersistence:
    async def test_generate_then_load_same_key(self, db):
        key1 = await load_or_generate_sm_keypair(db)
        key2 = await load_or_generate_sm_keypair(db)
        from cryptography.hazmat.primitives import serialization

        def pub(k):
            return k.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
        assert pub(key1) == pub(key2)


class TestMakeStateWiring:
    async def test_state_has_autonomy_chain(self):
        from harkeniq_sm.runtime import make_state
        config = _config(dsn="sqlite+aiosqlite:///:memory:")
        state = await make_state(config)
        try:
            assert isinstance(state.identity, AgentIdentityService)
            assert isinstance(state.autonomy, SMAutonomyEnforcer)
            assert isinstance(state.suppression, SuppressionEngine)
            # The suppression engine is threaded into correlation
            assert state.correlation.suppression is state.suppression
        finally:
            await state.engine.dispose()
            if state.tmp_db_path:
                import os
                os.unlink(state.tmp_db_path)


async def _site_id(db) -> str:
    """The site the registered agent's device belongs to.

    E0.2: error budgets are per site, so a test that seeds failures must
    say which site's evidence it is seeding.
    """
    from harkeniq_sm.db.repos import SiteRepo

    async with db() as session:
        site = await SiteRepo(session).get_by_name("site-1")
        return site.id


@pytest.fixture
async def lease_env(db):
    """Agent servicer with identity + enforcer + suppression, plus a
    registered agent whose keypair we hold (to parse leases)."""
    from harkeniq.autonomy.identity import AgentIdentity

    config = _config()
    key = await load_or_generate_sm_keypair(db)
    identity_service = AgentIdentityService(db, key)
    autonomy = SMAutonomyEnforcer()
    suppression = SuppressionEngine()
    servicer = AgentServiceServicer(
        IngestService(db, config),
        identity_service=identity_service,
        autonomy=autonomy,
        suppression=suppression,
    )
    agent = AgentIdentity.generate()
    await servicer.RegisterAgent(
        harkeniq_pb2.AgentRegistration(
            agent_id=agent.agent_id, agent_name="srv-01",
            vendor="Dell", model="R750",
            public_key_pem=agent.public_key_pem,
        ),
        None,
    )
    # sm_public_key must be pinned for lease verification
    agent.set_sm_public_key(identity_service.sm_public_key_pem)
    return {
        "servicer": servicer, "agent": agent,
        "autonomy": autonomy, "suppression": suppression,
    }


async def _heartbeat_lease(env):
    from harkeniq.autonomy.lease import AuthorizationLease

    ack = await env["servicer"].Heartbeat(
        harkeniq_pb2.AgentHeartbeat(
            agent_id=env["agent"].agent_id, agent_name="srv-01",
            state="OBSERVING",
        ),
        None,
    )
    assert ack.accepted
    assert ack.authorization_lease, "Heartbeat must carry a lease (QA-021)"
    return AuthorizationLease.parse(
        bytes(ack.authorization_lease), env["agent"]
    )


class TestHeartbeatLease:
    async def test_lease_issued_and_parses(self, lease_env):
        lease = await _heartbeat_lease(lease_env)
        assert lease.stop_switch is False
        assert "IDENTIFY_LED" in lease.action_classes
        assert lease.is_valid()

    async def test_lease_carries_stop_switch(self, lease_env):
        lease_env["autonomy"].activate_stop_switch("test-operator")
        lease = await _heartbeat_lease(lease_env)
        assert lease.stop_switch is True
        assert lease.allows_action("IDENTIFY_LED", "low", True) == "deny"

    async def test_lease_carries_policy_budgets(self, lease_env):
        lease_env["autonomy"].update_policy([
            {"action_type": "POWER_CYCLE", "max_per_window": 1,
             "window_seconds": 3600, "risk_level": "high"},
        ])
        lease = await _heartbeat_lease(lease_env)
        # Policy grants the class and bounds it
        assert "POWER_CYCLE" in lease.action_classes
        assert lease.budget_remaining["POWER_CYCLE"] == 1
        assert lease.risk_ceiling == "high"
        # Draw the budget down: remaining hits 0 in the next lease
        lease_env["autonomy"].record_execution("POWER_CYCLE")
        lease = await _heartbeat_lease(lease_env)
        assert lease.budget_remaining["POWER_CYCLE"] == 0
        assert lease.allows_action("POWER_CYCLE", "high", True) == "propose"

    async def test_lease_carries_suppression_domains(self, lease_env):
        from harkeniq_sm.suppression import CorrelationEvent
        import time

        for device in ("dev-a", "dev-b"):
            lease_env["suppression"].evaluate(CorrelationEvent(
                device_id=device, domain_id="dom-1", domain_kind="power",
                event_family="power", severity="CRITICAL",
                timestamp=time.time(),
            ))
        assert lease_env["suppression"].is_suppressed("dom-1")
        lease = await _heartbeat_lease(lease_env)
        assert lease.suppression_domains == ["dom-1"]


class TestErrorBudgetDropBackHasTeeth:
    """S5: automatic demotion must change what the agent may do.

    R3a ratified the drop-back model but nothing consulted it at runtime,
    so a class could fail repeatedly and keep its autonomy. The lease is
    where that has to bite — and it must bite as PROPOSE, not DENY: the
    action is still the right one, it just no longer runs unattended.
    """

    async def test_dropped_back_class_drops_to_propose(self, lease_env, db):
        from harkeniq_sm.db.repos import ErrorBudgetRepo
        from harkeniq_sm.knowledge import MIN_OUTCOMES_TO_JUDGE

        lease_env["autonomy"].update_policy([
            {"action_type": "SEL_CLEAR", "max_per_window": 5,
             "window_seconds": 3600, "risk_level": "low"},
        ])
        lease = await _heartbeat_lease(lease_env)
        assert lease.allows_action("SEL_CLEAR", "low", True) == "execute"

        site_id = await _site_id(db)
        async with db() as session:
            repo = ErrorBudgetRepo(session)
            for _ in range(MIN_OUTCOMES_TO_JUDGE):
                await repo.record(site_id, "SEL_CLEAR", "FAILURE")
            await session.commit()

        lease = await _heartbeat_lease(lease_env)
        assert lease.budget_remaining["SEL_CLEAR"] == 0
        assert lease.allows_action("SEL_CLEAR", "low", True) == "propose"
        # The class is still granted — demotion is not revocation.
        assert "SEL_CLEAR" in lease.action_classes

    async def test_a_healthy_class_is_untouched(self, lease_env, db):
        from harkeniq_sm.db.repos import ErrorBudgetRepo
        from harkeniq_sm.knowledge import MIN_OUTCOMES_TO_JUDGE

        lease_env["autonomy"].update_policy([
            {"action_type": "SEL_CLEAR", "max_per_window": 5,
             "window_seconds": 3600, "risk_level": "low"},
            {"action_type": "BMC_RESET", "max_per_window": 5,
             "window_seconds": 3600, "risk_level": "low"},
        ])
        site_id = await _site_id(db)
        async with db() as session:
            repo = ErrorBudgetRepo(session)
            for _ in range(MIN_OUTCOMES_TO_JUDGE):
                await repo.record(site_id, "SEL_CLEAR", "FAILURE")
            await session.commit()

        lease = await _heartbeat_lease(lease_env)
        assert lease.allows_action("SEL_CLEAR", "low", True) == "propose"
        assert lease.allows_action("BMC_RESET", "low", True) == "execute"

    async def test_recovery_restores_the_lease(self, lease_env, db):
        from harkeniq_sm.db.repos import ErrorBudgetRepo
        from harkeniq_sm.knowledge import MIN_OUTCOMES_TO_JUDGE

        lease_env["autonomy"].update_policy([
            {"action_type": "SEL_CLEAR", "max_per_window": 5,
             "window_seconds": 3600, "risk_level": "low"},
        ])
        site_id = await _site_id(db)
        async with db() as session:
            repo = ErrorBudgetRepo(session)
            for _ in range(MIN_OUTCOMES_TO_JUDGE):
                await repo.record(site_id, "SEL_CLEAR", "FAILURE")
            await session.commit()
        assert (await _heartbeat_lease(lease_env)).allows_action(
            "SEL_CLEAR", "low", True
        ) == "propose"

        async with db() as session:
            assert await ErrorBudgetRepo(session).recover(
                await _site_id(db), "SEL_CLEAR",
            ) is True
            await session.commit()
        assert (await _heartbeat_lease(lease_env)).allows_action(
            "SEL_CLEAR", "low", True
        ) == "execute"


class TestOutcomeReportFoldsTheErrorBudget:
    """The one place SM learns a terminal result must feed the budget."""

    async def test_repeated_failures_demote_the_class(self, db):
        from harkeniq_sm.approvals import ApprovalService
        from harkeniq_sm.db.repos import ErrorBudgetRepo
        from harkeniq_sm.knowledge import MIN_OUTCOMES_TO_JUDGE

        config = _config()
        approvals = ApprovalService(db, config)
        servicer = AgentServiceServicer(
            IngestService(db, config), approvals=approvals,
        )
        await servicer.RegisterAgent(
            harkeniq_pb2.AgentRegistration(
                agent_id="agent-eb", agent_name="srv-eb",
                vendor="Dell", model="R750",
            ),
            None,
        )
        for i in range(MIN_OUTCOMES_TO_JUDGE):
            await servicer.ReportAction(
                harkeniq_pb2.ActionReport(
                    agent_id="agent-eb", action_id=f"act-{i}",
                    type="SEL_CLEAR", status="FAILED",
                    outcome_json='{"success": false}',
                ),
                None,
            )
        async with db() as session:
            rows = await ErrorBudgetRepo(session).list_all()
            assert [r.action_type for r in rows] == ["SEL_CLEAR"]
            assert rows[0].total_count == MIN_OUTCOMES_TO_JUDGE
            assert rows[0].dropped_back is True

    async def test_successes_leave_autonomy_intact(self, db):
        from harkeniq_sm.approvals import ApprovalService
        from harkeniq_sm.db.repos import ErrorBudgetRepo
        from harkeniq_sm.knowledge import MIN_OUTCOMES_TO_JUDGE

        config = _config()
        servicer = AgentServiceServicer(
            IngestService(db, config), approvals=ApprovalService(db, config),
        )
        await servicer.RegisterAgent(
            harkeniq_pb2.AgentRegistration(
                agent_id="agent-ok", agent_name="srv-ok",
                vendor="Dell", model="R750",
            ),
            None,
        )
        for i in range(MIN_OUTCOMES_TO_JUDGE * 2):
            await servicer.ReportAction(
                harkeniq_pb2.ActionReport(
                    agent_id="agent-ok", action_id=f"ok-{i}",
                    type="BMC_RESET", status="COMPLETED",
                    outcome_json='{"success": true}',
                ),
                None,
            )
        async with db() as session:
            rows = await ErrorBudgetRepo(session).list_all()
            assert rows[0].dropped_back is False
            assert rows[0].success_count == MIN_OUTCOMES_TO_JUDGE * 2


class TestCompletedActionsDrawBudget:
    async def test_report_action_records_execution(self, db):
        from harkeniq_sm.approvals import ApprovalService

        config = _config()
        autonomy = SMAutonomyEnforcer()
        autonomy.update_policy([
            {"action_type": "FAN_RESET", "max_per_window": 5,
             "window_seconds": 3600, "risk_level": "low"},
        ])
        servicer = AgentServiceServicer(
            IngestService(db, config),
            approvals=ApprovalService(db, config),
            autonomy=autonomy,
        )
        report = harkeniq_pb2.ActionReport(
            agent_id="agent-x", action_id="act-1", type="FAN_RESET",
            sensor_id="fan1", skill_name="fan-health",
            verdict_severity="WARNING", status="COMPLETED",
            proposed_at="2026-08-25T00:00:00Z",
        )
        ack = await servicer.ReportAction(report, None)
        assert ack.accepted
        assert autonomy.get_budget_for_agent("agent-x")["FAN_RESET"] == 4


class TestCorrelationSuppressionHook:
    async def test_onset_in_shared_domain_triggers_suppression(self, db):
        """Two PSU criticals in one power domain -> domain suppressed."""
        from harkeniq_sm.correlation.engine import CorrelationEngine
        from harkeniq_sm.db.models import Device, FaultDomain, Site
        from harkeniq_sm.db.repos import DomainRepo
        from datetime import datetime, timezone

        config = _config(site_name="site-1")
        suppression = SuppressionEngine()
        engine = CorrelationEngine(db, config, suppression=suppression)

        async with db() as session:
            site = Site(name="site-1")
            session.add(site)
            await session.flush()
            devs = [
                Device(site_id=site.id, agent_id=f"agent-{i}",
                       agent_name=f"srv-0{i}", vendor="Dell", model="R750")
                for i in (1, 2)
            ]
            session.add_all(devs)
            await session.flush()
            domain = FaultDomain(site_id=site.id, name="pdu-a", kind="power")
            session.add(domain)
            await session.flush()
            await DomainRepo(session).set_members(
                domain.id, [d.id for d in devs], added_by="test",
            )
            domain_id = domain.id
            dev_ids = [d.id for d in devs]
            await session.commit()

        now = datetime.now(timezone.utc)
        for dev_id in dev_ids:
            await engine.on_onset(dev_id, "psu", "CRITICAL", now)

        assert suppression.is_suppressed(domain_id)
        state = suppression.get_state()
        reason = state["active_suppressions"][domain_id]["trigger_reason"]
        assert reason == "direct_dependency"


class TestAutonomyApi:
    @pytest.fixture
    async def client(self, db):
        from harkeniq_sm.runtime import AppState
        from harkeniq_sm.app import create_app

        config = _config(site_name="site-1")
        state = AppState(config=config)
        state.sessionmaker = db
        state.autonomy = SMAutonomyEnforcer()
        state.suppression = SuppressionEngine()
        app = create_app(state)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://sm") as c:
            yield c, state

    async def test_state_endpoint(self, client):
        c, _ = client
        resp = await c.get("/api/autonomy")
        assert resp.status_code == 200
        body = resp.json()
        assert body["stop_switch"] is False
        assert "suppression" in body

    async def test_stop_switch_requires_actor(self, client):
        c, _ = client
        resp = await c.post("/api/autonomy/stop-switch", json={})
        assert resp.status_code == 422

    async def test_stop_switch_flip_audited(self, client):
        c, state = client
        resp = await c.post(
            "/api/autonomy/stop-switch", json={"actor": "vinod"}
        )
        assert resp.status_code == 200
        assert resp.json()["stop_switch"] is True
        assert state.autonomy.stop_switch_active is True

        resp = await c.post(
            "/api/autonomy/stop-switch/deactivate", json={"actor": "vinod"}
        )
        assert resp.status_code == 200
        assert state.autonomy.stop_switch_active is False

        from sqlalchemy import select
        from harkeniq_sm.db.models import AuditLogRow
        async with state.sessionmaker() as session:
            actions = [
                r.action for r in (
                    await session.execute(select(AuditLogRow))
                ).scalars().all()
            ]
        assert "stop_switch.activate" in actions
        assert "stop_switch.deactivate" in actions

    async def test_suppression_re_enable(self, client):
        c, state = client
        from harkeniq_sm.suppression import CorrelationEvent
        import time

        for device in ("dev-a", "dev-b"):
            state.suppression.evaluate(CorrelationEvent(
                device_id=device, domain_id="dom-9", domain_kind="power",
                event_family="power", severity="CRITICAL",
                timestamp=time.time(),
            ))
        assert state.suppression.is_suppressed("dom-9")
        resp = await c.post(
            "/api/autonomy/suppression/dom-9/re-enable",
            json={"actor": "vinod"},
        )
        assert resp.status_code == 200
        assert not state.suppression.is_suppressed("dom-9")

        resp = await c.post(
            "/api/autonomy/suppression/dom-9/re-enable",
            json={"actor": "vinod"},
        )
        assert resp.status_code == 404
