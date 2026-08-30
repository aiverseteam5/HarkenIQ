"""A1: the CC->SM dispatch verb, and outcomes for directed work.

DispatchAction delivers a decision Central Command already governed. It
must resolve the device, refuse what the site's own live safety state
forbids, and otherwise queue on the EXISTING directive transport with
the attribution intact.

The second half covers the defect this slice found: a directed action
that ran produced no outcome record at all, so every firmware-campaign
execution since R5-1 was invisible to the error budget and to fleet
learning.
"""

from __future__ import annotations

import json

import grpc
import pytest

from harkeniq.proto import harkeniq_pb2, harkeniq_pb2_grpc
from harkeniq_sm.approvals import ApprovalService
from harkeniq_sm.autonomy import SMAutonomyEnforcer
from harkeniq_sm.db.models import ActionOutcomeRow, ErrorBudgetRow
from harkeniq_sm.db.repos import (
    AuditRepo,
    DeviceRepo,
    DirectiveRepo,
    ErrorBudgetRepo,
    SiteRepo,
)
from harkeniq_sm.config import SMConfig
from harkeniq_sm.directives import DirectiveService
from harkeniq_sm.grpc_server import (
    AgentServiceServicer,
    SiteManagerServiceServicer,
    build_server,
)
from harkeniq_sm.ingest import IngestService

TOKEN = "site-secret"
AGENT_ACTOR = "op-agent:ag1@v1"


def _config(**overrides):
    defaults = dict(insecure=True, site_name="site-1", grpc_port=0)
    defaults.update(overrides)
    return SMConfig(**defaults)


SITE_CC_ID = "cc-site-1"


async def _seed_device(db, agent_id="agent-1", site_name="site-1",
                       cc_site_id=SITE_CC_ID):
    async with db() as session:
        site = await SiteRepo(session).get_or_create(site_name)
        # E0.2: a site is addressed by Central Command's identity.
        site.cc_site_id = site.cc_site_id or cc_site_id
        device = await DeviceRepo(session).upsert_registration(
            site_id=site.id, agent_id=agent_id, vendor="dell",
        )
        await session.commit()
        return device.id


def _dispatch(**kw):
    body = dict(
        tenant_id="t1", site_id="s1", device_agent_id="agent-1",
        action_type="SEL_CLEAR", params_json="{}", actor=AGENT_ACTOR,
        authorization="human_approval", decided_by="op@example.com",
        proposal_id="prop-1",
    )
    body.update(kw)
    return harkeniq_pb2.ActionDispatch(**body)


class TestDispatchAction:
    @pytest.fixture
    async def served(self, db):
        config = _config(site_token=TOKEN)
        directives = DirectiveService(db, config)
        autonomy = SMAutonomyEnforcer()
        sm_servicer = SiteManagerServiceServicer(
            db, ApprovalService(db, config), config,
            directives=directives, autonomy=autonomy,
        )
        server, port = build_server(
            config, AgentServiceServicer(IngestService(db, config)),
            sm_servicer=sm_servicer,
        )
        await server.start()
        yield port, directives, autonomy, db
        await server.stop(None)

    async def _call(self, port, request):
        async with grpc.aio.insecure_channel(f"localhost:{port}") as channel:
            stub = harkeniq_pb2_grpc.SiteManagerServiceStub(channel)
            return await stub.DispatchAction(
                request, metadata=[("authorization", f"Bearer {TOKEN}")],
            )

    async def test_queues_a_directive_carrying_the_attribution(self, served):
        port, _, _, db = served
        device_id = await _seed_device(db)
        ack = await self._call(port, _dispatch(
            params_json=json.dumps({"reason": "log saturated"}),
        ))
        assert ack.accepted is True
        assert ack.directive_id
        async with db() as session:
            row = await DirectiveRepo(session).get(ack.directive_id)
        assert row.kind == "action"
        assert row.action_type == "SEL_CLEAR"
        assert row.device_id == device_id
        assert row.actor == AGENT_ACTOR
        assert row.authorization_basis == "human_approval"
        assert row.proposal_id == "prop-1"
        assert row.params == {"reason": "log saturated"}

    async def test_unknown_device_is_refused_with_a_reason(self, served):
        port, _, _, db = served
        await _seed_device(db)
        ack = await self._call(port, _dispatch(device_agent_id="ghost"))
        assert ack.accepted is False
        assert "not known at this site" in ack.reason

    async def test_unknown_action_class_is_refused(self, served):
        """A directive nothing can execute would never settle."""
        port, _, _, db = served
        await _seed_device(db)
        ack = await self._call(port, _dispatch(action_type="MAKE_COFFEE"))
        assert ack.accepted is False
        assert "unknown action type" in ack.reason

    async def test_unparseable_params_are_refused(self, served):
        port, _, _, db = served
        await _seed_device(db)
        ack = await self._call(port, _dispatch(params_json="not json"))
        assert ack.accepted is False
        assert "params_json" in ack.reason

    async def test_site_stop_switch_refuses_everything(self, served):
        port, _, autonomy, db = served
        await _seed_device(db)
        autonomy.activate_stop_switch("maintenance")
        ack = await self._call(port, _dispatch())
        assert ack.accepted is False
        assert "stop switch" in ack.reason

    async def test_error_budget_dropback_refuses_an_autonomous_dispatch(
        self, served,
    ):
        """The S5 demotion has to bite the actor it was created for."""
        port, _, _, db = served
        await _seed_device(db)
        async with db() as session:
            site = await SiteRepo(session).get_by_name("site-1")
            session.add(ErrorBudgetRow(
                site_id=site.id,
                action_type="SEL_CLEAR", success_count=2, failure_count=18,
                total_count=20, dropped_back=True,
            ))
            await session.commit()
        ack = await self._call(port, _dispatch(authorization="autonomous_grant"))
        assert ack.accepted is False
        assert "withdrawn by the error budget" in ack.reason

    async def test_a_human_approval_survives_a_dropback(self, served):
        """Demotion withdraws AUTONOMY, not the action itself (A2.2)."""
        port, _, _, db = served
        await _seed_device(db)
        async with db() as session:
            site = await SiteRepo(session).get_by_name("site-1")
            session.add(ErrorBudgetRow(
                site_id=site.id,
                action_type="SEL_CLEAR", success_count=2, failure_count=18,
                total_count=20, dropped_back=True,
            ))
            await session.commit()
        ack = await self._call(port, _dispatch(authorization="human_approval"))
        assert ack.accepted is True

    async def test_dispatch_is_audited_on_the_site_chain(self, served):
        port, _, _, db = served
        await _seed_device(db)
        await self._call(port, _dispatch())
        async with db() as session:
            audit = AuditRepo(session)
            rows = await audit.list_all()
            enqueued = [r for r in rows if r.action == "directive.enqueue"]
            assert enqueued and enqueued[0].actor == AGENT_ACTOR
            assert enqueued[0].detail["proposal_id"] == "prop-1"
            assert (await audit.verify_chain()).valid is True

    async def test_requires_the_site_token(self, served):
        port, _, _, db = served
        await _seed_device(db)
        async with grpc.aio.insecure_channel(f"localhost:{port}") as channel:
            stub = harkeniq_pb2_grpc.SiteManagerServiceStub(channel)
            with pytest.raises(grpc.aio.AioRpcError):
                await stub.DispatchAction(_dispatch())


class TestDirectedWorkBecomesEvidence:
    """The D-1 defect: directed executions produced no outcome at all."""

    async def test_settled_action_directive_writes_an_attributed_outcome(self, db):
        device_id = await _seed_device(db)
        svc = DirectiveService(db, _config())
        directive_id = await svc.enqueue_action(
            device_id, "SEL_CLEAR", {"reason": "log full"},
            issued_by="op@example.com", actor=AGENT_ACTOR,
            authorization_basis="human_approval", proposal_id="prop-1",
        )
        await svc.poll("agent-1")
        assert await svc.report_result("agent-1", directive_id, True, "ok")

        async with db() as session:
            from sqlalchemy import select

            rows = (await session.execute(select(ActionOutcomeRow))).scalars().all()
        assert len(rows) == 1
        assert rows[0].action_type == "SEL_CLEAR"
        assert rows[0].outcome == "SUCCESS"
        assert rows[0].actor == AGENT_ACTOR
        assert rows[0].reported_to_cc is False  # rides the next snapshot up

    async def test_a_failed_directive_folds_into_the_error_budget(self, db):
        """Repeated failure must withdraw autonomy without a human."""
        device_id = await _seed_device(db)
        svc = DirectiveService(db, _config())
        for i in range(12):
            directive_id = await svc.enqueue_action(
                device_id, "SEL_CLEAR", actor=AGENT_ACTOR,
                authorization_basis="autonomous_grant",
            )
            await svc.poll("agent-1")
            await svc.report_result("agent-1", directive_id, False, "boom")
        async with db() as session:
            site = await SiteRepo(session).get_by_name("site-1")
            dropped = await ErrorBudgetRepo(session).dropped_back_types(site.id)
        assert "SEL_CLEAR" in dropped

    async def test_skill_installs_do_not_fabricate_an_action_outcome(self, db):
        """Only executions are evidence; installing a skill is not one."""
        device_id = await _seed_device(db)
        svc = DirectiveService(db, _config())
        directive_id = await svc.enqueue_skill_install(
            device_id, "fan-health", "1", "name: fan-health\n",
        )
        await svc.poll("agent-1")
        await svc.report_result("agent-1", directive_id, True, "installed")
        async with db() as session:
            from sqlalchemy import select

            rows = (await session.execute(select(ActionOutcomeRow))).scalars().all()
        assert rows == []

    async def test_the_outcome_reaches_the_fleet_snapshot_with_its_actor(self, db):
        device_id = await _seed_device(db)
        svc = DirectiveService(db, _config())
        directive_id = await svc.enqueue_action(
            device_id, "SEL_CLEAR", actor=AGENT_ACTOR,
        )
        await svc.poll("agent-1")
        await svc.report_result("agent-1", directive_id, True, "ok")

        config = _config(site_token=TOKEN)
        sm_servicer = SiteManagerServiceServicer(
            db, ApprovalService(db, config), config,
        )
        snapshot = await sm_servicer.GetFleetSnapshot(
            harkeniq_pb2.FleetSnapshotRequest(
                tenant_id="t1", site_id=SITE_CC_ID,
            ),
            None,
        )
        assert len(snapshot.outcomes) == 1
        assert snapshot.outcomes[0].actor == AGENT_ACTOR
        assert snapshot.outcomes[0].action_type == "SEL_CLEAR"
