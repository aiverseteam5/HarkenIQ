"""SiteManagerServiceServicer tests: direct method calls, no gRPC transport.

Exercises RegisterSite, GetFleetSnapshot, RouteApproval, GetUsageSnapshot,
and PushPolicy RPCs against an in-memory DB with seeded test data.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from harkeniq.proto import harkeniq_pb2
from harkeniq_sm.approvals import ApprovalService
from harkeniq_sm.config import SMConfig
from harkeniq_sm.db.models import ActionRow, Device, Incident, Site
from harkeniq_sm.db.repos import StatusRepo, SubsystemStateRepo
from harkeniq_sm.grpc_server import SiteManagerServiceServicer


def _config(**overrides):
    defaults = dict(insecure=True, site_token="test-token")
    defaults.update(overrides)
    return SMConfig(**defaults)


@pytest.fixture
async def sm_env(db):
    """Set up a servicer with seeded devices, incidents, and actions."""
    config = _config()
    approvals = ApprovalService(db, config)
    servicer = SiteManagerServiceServicer(db, approvals, config)

    async with db() as session:
        site = Site(name="site-1")
        session.add(site)
        await session.flush()
        site_id = site.id

        dev1 = Device(
            site_id=site.id, agent_id="agent-1", agent_name="srv-01",
            vendor="Dell", model="R750",
        )
        dev2 = Device(
            site_id=site.id, agent_id="agent-2", agent_name="srv-02",
            vendor="HPE", model="DL380",
        )
        session.add_all([dev1, dev2])
        await session.flush()
        dev1_id = dev1.id
        dev2_id = dev2.id

        # Add agent statuses so devices are "observed"
        now = datetime.now(timezone.utc)
        await StatusRepo(session).upsert(
            dev1.id, now, "OBSERVING", {"psu": "OK"}, {},
        )
        await StatusRepo(session).upsert(
            dev2.id, now, "OBSERVING", {"fan": "WARNING"}, {},
        )

        # Add subsystem states for dev1 (psu=OK, fan=CRITICAL -> worst=critical)
        await SubsystemStateRepo(session).set(dev1.id, "psu", "OK", now)
        await SubsystemStateRepo(session).set(dev1.id, "fan", "CRITICAL", now)

        # Add an open incident
        incident = Incident(
            site_id=site.id, kind="device", status="open",
            device_id=dev1.id, subsystem="fan",
            title="Fan failure on srv-01",
        )
        session.add(incident)
        await session.flush()
        incident_id = incident.id

        # Add a pending action
        action = ActionRow(
            device_id=dev1.id, agent_action_id="act-cli-1",
            type="fan_boost", sensor_id="fan:FAN1", skill_name="fan_health",
            verdict_severity="CRITICAL", status="pending",
            proposed_at=now.isoformat(),
        )
        session.add(action)
        await session.flush()
        action_id = action.id

        await session.commit()

    return {
        "servicer": servicer,
        "config": config,
        "approvals": approvals,
        "site_id": site_id,
        "dev1_id": dev1_id,
        "dev2_id": dev2_id,
        "incident_id": incident_id,
        "action_id": action_id,
    }


class TestRegisterSite:
    async def test_register_site_accepted(self, sm_env):
        servicer = sm_env["servicer"]
        request = harkeniq_pb2.SiteRegistration(
            tenant_id="t1", site_id="s1", site_name="DC-BLR-1",
            cc_endpoint="https://cc.lab:8090",
            license_key_fingerprint="abc123",
        )
        ack = await servicer.RegisterSite(request, None)
        assert ack.accepted is True
        assert ack.site_token == "test-token"

    async def test_register_site_empty_fingerprint(self, sm_env):
        servicer = sm_env["servicer"]
        request = harkeniq_pb2.SiteRegistration(
            tenant_id="t1", site_id="s1", site_name="DC-BLR-1",
            cc_endpoint="https://cc.lab:8090",
            license_key_fingerprint="",
        )
        ack = await servicer.RegisterSite(request, None)
        assert ack.accepted is False
        assert "required" in ack.reason


class TestGetFleetSnapshot:
    async def test_devices_returned(self, sm_env):
        servicer = sm_env["servicer"]
        request = harkeniq_pb2.FleetSnapshotRequest(
            tenant_id="t1", site_id=sm_env["site_id"],
        )
        snap = await servicer.GetFleetSnapshot(request, None)
        assert len(snap.devices) == 2
        agent_ids = {d.agent_id for d in snap.devices}
        assert "agent-1" in agent_ids
        assert "agent-2" in agent_ids

    async def test_device_fields(self, sm_env):
        servicer = sm_env["servicer"]
        request = harkeniq_pb2.FleetSnapshotRequest(
            tenant_id="t1", site_id=sm_env["site_id"],
        )
        snap = await servicer.GetFleetSnapshot(request, None)
        dev1 = next(d for d in snap.devices if d.agent_id == "agent-1")
        assert dev1.agent_name == "srv-01"
        assert dev1.vendor == "Dell"
        assert dev1.model == "R750"

    async def test_health_rollup(self, sm_env):
        servicer = sm_env["servicer"]
        request = harkeniq_pb2.FleetSnapshotRequest(
            tenant_id="t1", site_id=sm_env["site_id"],
        )
        snap = await servicer.GetFleetSnapshot(request, None)
        dev1 = next(d for d in snap.devices if d.agent_id == "agent-1")
        # dev1 has psu=OK and fan=CRITICAL subsystem states; worst should be critical
        assert dev1.health == "critical"

    async def test_incidents_returned(self, sm_env):
        servicer = sm_env["servicer"]
        request = harkeniq_pb2.FleetSnapshotRequest(
            tenant_id="t1", site_id=sm_env["site_id"],
        )
        snap = await servicer.GetFleetSnapshot(request, None)
        assert len(snap.incidents) == 1
        inc = snap.incidents[0]
        assert inc.kind == "device"
        assert inc.status == "open"

    async def test_pending_actions_returned(self, sm_env):
        servicer = sm_env["servicer"]
        request = harkeniq_pb2.FleetSnapshotRequest(
            tenant_id="t1", site_id=sm_env["site_id"],
        )
        snap = await servicer.GetFleetSnapshot(request, None)
        assert len(snap.pending_actions) == 1
        act = snap.pending_actions[0]
        assert act.type == "fan_boost"
        assert act.status == "pending"

    async def test_snapshot_timestamp(self, sm_env):
        servicer = sm_env["servicer"]
        request = harkeniq_pb2.FleetSnapshotRequest(
            tenant_id="t1", site_id=sm_env["site_id"],
        )
        snap = await servicer.GetFleetSnapshot(request, None)
        assert snap.snapshot_at_unix > 0

    async def test_fallback_all_devices(self, sm_env):
        """When site_id doesn't match, falls back to listing all devices."""
        servicer = sm_env["servicer"]
        request = harkeniq_pb2.FleetSnapshotRequest(
            tenant_id="t1", site_id="nonexistent-site-id",
        )
        snap = await servicer.GetFleetSnapshot(request, None)
        # Should fall back and return all devices
        assert len(snap.devices) == 2


class TestRouteApproval:
    async def test_approve(self, sm_env):
        servicer = sm_env["servicer"]
        action_id = sm_env["action_id"]
        request = harkeniq_pb2.ApprovalRouteRequest(
            action_id=action_id,
            decision="approved",
            decided_by="admin@lab",
            tenant_id="t1",
        )
        ack = await servicer.RouteApproval(request, None)
        assert ack.accepted is True
        assert ack.delivered is True

    async def test_deny(self, sm_env):
        servicer = sm_env["servicer"]
        action_id = sm_env["action_id"]
        request = harkeniq_pb2.ApprovalRouteRequest(
            action_id=action_id,
            decision="denied",
            decided_by="admin@lab",
            tenant_id="t1",
        )
        ack = await servicer.RouteApproval(request, None)
        assert ack.accepted is True
        assert ack.delivered is True

    async def test_not_found(self, sm_env):
        servicer = sm_env["servicer"]
        request = harkeniq_pb2.ApprovalRouteRequest(
            action_id="nonexistent-id",
            decision="approved",
            decided_by="admin@lab",
            tenant_id="t1",
        )
        ack = await servicer.RouteApproval(request, None)
        assert ack.accepted is False

    async def test_unknown_decision(self, sm_env):
        servicer = sm_env["servicer"]
        request = harkeniq_pb2.ApprovalRouteRequest(
            action_id=sm_env["action_id"],
            decision="maybe",
            decided_by="admin@lab",
            tenant_id="t1",
        )
        ack = await servicer.RouteApproval(request, None)
        assert ack.accepted is False
        assert "unknown decision" in ack.reason


class TestGetUsageSnapshot:
    async def test_returns_node_count(self, sm_env):
        servicer = sm_env["servicer"]
        request = harkeniq_pb2.UsageSnapshotRequest(
            tenant_id="t1", site_id="s1", date="2026-08-20",
        )
        snap = await servicer.GetUsageSnapshot(request, None)
        # We seeded 2 devices
        assert snap.node_count == 2


class TestPushPolicy:
    async def test_accepted(self, sm_env):
        servicer = sm_env["servicer"]
        request = harkeniq_pb2.PolicyUpdate(
            tenant_id="t1", site_id="s1",
            approval_policies_json='[{"name":"default"}]',
            autonomy_budgets_json='[{"device_type":"*","level":0}]',
        )
        ack = await servicer.PushPolicy(request, None)
        assert ack.accepted is True
