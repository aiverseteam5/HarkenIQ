"""Approval brokering (R-S6, D16): lifecycle, races, wave guard."""

import json
from types import SimpleNamespace

import pytest

from harkeniq_sm.approvals import ApprovalError, ApprovalService
from harkeniq_sm.config import SMConfig
from harkeniq_sm.db.repos import (
    ActionRepo,
    AuditRepo,
    DeviceRepo,
    DomainRepo,
    IncidentRepo,
    SiteRepo,
)


@pytest.fixture
def config():
    return SMConfig(insecure=True, site_name="site-test")


@pytest.fixture
def service(db, config):
    return ApprovalService(db, config)


def report(agent_id="a1", action_id="act-1", status="PENDING", **kw):
    return SimpleNamespace(
        agent_id=agent_id,
        action_id=action_id,
        type=kw.get("type", "FAN_RESET"),
        sensor_id=kw.get("sensor_id", "fan:Fan1"),
        skill_name=kw.get("skill_name", "fan_health"),
        verdict_severity=kw.get("verdict_severity", "CRITICAL"),
        params_json=kw.get("params_json", '{"target": "Fan1"}'),
        status=status,
        proposed_at=kw.get("proposed_at", "2026-08-19T10:00:00Z"),
        outcome_json=kw.get("outcome_json", ""),
    )


async def _row(db, agent_id="a1", action_id="act-1"):
    async with db() as session:
        device = await DeviceRepo(session).get_by_agent_id(agent_id)
        return await ActionRepo(session).get_by_agent_action(device.id, action_id)


class TestReportAction:
    async def test_pending_creates_row(self, db, service):
        assert await service.report_action(report())
        row = await _row(db)
        assert row.status == "pending"
        assert row.params == {"target": "Fan1"}
        assert row.verdict_severity == "CRITICAL"

    async def test_idempotent_retry(self, db, service):
        await service.report_action(report())
        await service.report_action(report())
        async with db() as session:
            assert len(await ActionRepo(session).list_all()) == 1

    async def test_links_open_incident(self, db, config, service):
        async with db() as session:
            site = await SiteRepo(session).get_or_create(config.site_name)
            device = await DeviceRepo(session).upsert_registration(
                site_id=site.id, agent_id="a1"
            )
            incident = await IncidentRepo(session).create(
                site_id=site.id, kind="device", device_id=device.id, subsystem="fan"
            )
            await session.commit()
            incident_id = incident.id
        await service.report_action(report())
        assert (await _row(db)).incident_id == incident_id

    async def test_completed_records_outcome(self, db, service):
        await service.report_action(report())
        await service.approve((await _row(db)).id, actor="op")
        await service.poll_decisions("a1")
        await service.report_action(
            report(status="COMPLETED", outcome_json=json.dumps({"success": True}))
        )
        row = await _row(db)
        assert row.status == "completed"
        assert row.outcome == {"success": True}


class TestDecisionFlow:
    async def test_approve_then_poll_delivers_once(self, db, service):
        await service.report_action(report())
        row = await _row(db)
        approved = await service.approve(row.id, actor="op@site")
        assert approved.status == "approved"

        decisions = await service.poll_decisions("a1")
        assert len(decisions) == 1
        assert decisions[0].action_id == "act-1"
        assert decisions[0].decision == "approved"
        assert decisions[0].decided_by == "op@site"
        assert decisions[0].decided_at_unix > 0
        assert (await _row(db)).status == "delivered"
        # Delivered once only.
        assert await service.poll_decisions("a1") == []

    async def test_deny_is_final(self, db, service):
        await service.report_action(report())
        row = await _row(db)
        await service.deny(row.id, actor="op")
        decisions = await service.poll_decisions("a1")
        assert decisions[0].decision == "denied"
        assert (await _row(db)).status == "denied"
        with pytest.raises(ApprovalError):
            await service.approve(row.id, actor="op")

    async def test_unknown_agent_poll_empty(self, service):
        assert await service.poll_decisions("ghost") == []

    async def test_approve_unknown_action(self, service):
        with pytest.raises(ApprovalError) as exc:
            await service.approve("nope", actor="op")
        assert exc.value.code == "not_found"

    async def test_audit_written(self, db, service):
        await service.report_action(report())
        await service.approve((await _row(db)).id, actor="op")
        await service.poll_decisions("a1")
        async with db() as session:
            actions = [a.action for a in await AuditRepo(session).list_all()]
        assert "action.reported" in actions
        assert "action.approve" in actions
        assert "action.delivered" in actions


class TestFirstDeciderWins:
    async def test_cli_decided_before_sm(self, db, service):
        """Agent reports APPROVED while SM row is pending → CLI won."""
        await service.report_action(report())
        await service.report_action(report(status="APPROVED"))
        row = await _row(db)
        assert row.status == "delivered"
        assert row.decided_by == "agent-local"
        assert row.decision == "approved"

    async def test_conflicting_local_decision_supersedes_sm(self, db, service):
        """SM denied, but a CLI approval reached the agent first."""
        await service.report_action(report())
        await service.deny((await _row(db)).id, actor="op")
        # Decision not yet delivered; agent reports the CLI's approval.
        await service.report_action(report(status="APPROVED"))
        assert (await _row(db)).status == "superseded"

    async def test_matching_decisions_converge(self, db, service):
        await service.report_action(report())
        await service.approve((await _row(db)).id, actor="op")
        await service.report_action(report(status="APPROVED"))
        assert (await _row(db)).status == "delivered"

    async def test_locally_decided_first_report(self, db, service):
        """First-ever report already APPROVED (agent knew SM late)."""
        await service.report_action(report(status="APPROVED"))
        row = await _row(db)
        assert row.status == "delivered"
        assert row.decided_by == "agent-local"


class TestWaveGuard:
    async def _setup_domain(self, db, config, service):
        await service.report_action(report(agent_id="a1", action_id="act-1"))
        await service.report_action(report(agent_id="a2", action_id="act-2"))
        async with db() as session:
            site = await SiteRepo(session).get_or_create(config.site_name)
            device_repo = DeviceRepo(session)
            d1 = await device_repo.get_by_agent_id("a1")
            d2 = await device_repo.get_by_agent_id("a2")
            domain_repo = DomainRepo(session)
            domain = await domain_repo.create(site.id, "pdu-a", "power")
            await domain_repo.set_members(domain.id, [d1.id, d2.id])
            await session.commit()

    async def test_refuses_second_action_in_domain(self, db, config, service):
        await self._setup_domain(db, config, service)
        row1 = await _row(db, "a1", "act-1")
        row2 = await _row(db, "a2", "act-2")
        await service.approve(row1.id, actor="op")
        with pytest.raises(ApprovalError, match="in flight"):
            await service.approve(row2.id, actor="op")
        # Guard holds while delivered-unfinished too.
        await service.poll_decisions("a1")
        with pytest.raises(ApprovalError, match="in flight"):
            await service.approve(row2.id, actor="op")

    async def test_releases_after_completion(self, db, config, service):
        await self._setup_domain(db, config, service)
        row1 = await _row(db, "a1", "act-1")
        row2 = await _row(db, "a2", "act-2")
        await service.approve(row1.id, actor="op")
        await service.poll_decisions("a1")
        await service.report_action(
            report(agent_id="a1", action_id="act-1", status="COMPLETED")
        )
        approved = await service.approve(row2.id, actor="op")
        assert approved.status == "approved"

    async def test_no_domain_no_guard(self, db, config, service):
        await service.report_action(report(agent_id="a1", action_id="act-1"))
        await service.report_action(report(agent_id="a1", action_id="act-9",
                                           sensor_id="disk:sda"))
        row1 = await _row(db, "a1", "act-1")
        row2 = await _row(db, "a1", "act-9")
        await service.approve(row1.id, actor="op")
        # a1 belongs to no fault domain: guard does not apply.
        assert (await service.approve(row2.id, actor="op")).status == "approved"
