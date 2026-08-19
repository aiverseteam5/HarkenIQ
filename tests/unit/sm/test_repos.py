"""Repository behavior against in-memory aiosqlite."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from harkeniq_sm.db.models import ActionRow
from harkeniq_sm.db.repos import (
    ActionRepo,
    AuditRepo,
    DeviceRepo,
    DomainRepo,
    IncidentRepo,
    SiteRepo,
    StatusRepo,
    SubsystemStateRepo,
    TelemetryRepo,
)


def now():
    return datetime.now(timezone.utc)


@pytest.fixture
async def site(session):
    return await SiteRepo(session).get_or_create("site-1")


@pytest.fixture
async def device(session, site):
    return await DeviceRepo(session).upsert_registration(
        site_id=site.id, agent_id="agent-1", agent_name="rack-12-srv-04",
        vendor="Dell", model="R750", service_tag="ABC123",
    )


class TestSiteAndDevice:
    async def test_site_get_or_create_idempotent(self, session):
        repo = SiteRepo(session)
        a = await repo.get_or_create("s")
        b = await repo.get_or_create("s")
        assert a.id == b.id

    async def test_upsert_registration_idempotent(self, session, site, device):
        repo = DeviceRepo(session)
        again = await repo.upsert_registration(
            site_id=site.id, agent_id="agent-1", peers=["10.0.0.2:5150"]
        )
        assert again.id == device.id
        # empty strings don't clobber prior values
        assert again.vendor == "Dell"
        assert again.peers == ["10.0.0.2:5150"]
        assert len(await repo.list_for_site(site.id)) == 1

    async def test_lookup_by_agent_id(self, session, device):
        repo = DeviceRepo(session)
        assert (await repo.get_by_agent_id("agent-1")).id == device.id
        assert await repo.get_by_agent_id("nope") is None


class TestStatusAndTelemetry:
    async def test_status_upsert_updates_row(self, session, device):
        repo = StatusRepo(session)
        t1 = now()
        await repo.upsert(device.id, t1, "RUNNING", {"psu": "OK"}, None)
        t2 = t1 + timedelta(seconds=60)
        await repo.upsert(device.id, t2, "DEGRADED", {"psu": "CRITICAL"}, {"p1": "ALIVE"})
        rows = await repo.list_all()
        assert len(rows) == 1
        assert rows[0].last_state == "DEGRADED"
        assert rows[0].last_peer_status == {"p1": "ALIVE"}

    async def test_verdicts_recent_ordering(self, session, device):
        repo = TelemetryRepo(session)
        t = now()
        for i in range(3):
            await repo.add_verdict(
                device.id, t + timedelta(seconds=i), "psu.1", "psu_health",
                "CRITICAL", f"m{i}", [{"field": "input_voltage", "value": 0}],
            )
        recent = await repo.recent_verdicts(device.id, limit=2)
        assert [r.message for r in recent] == ["m2", "m1"]
        assert recent[0].evidence[0]["field"] == "input_voltage"

    async def test_heartbeat_row(self, session, device):
        row = await TelemetryRepo(session).add_heartbeat(
            device.id, now(), "RUNNING", {"thermal": "OK"}, {"peer-a": "UNRESPONSIVE"}
        )
        assert row.peer_status == {"peer-a": "UNRESPONSIVE"}


class TestSubsystemState:
    async def test_set_and_non_ok(self, session, device):
        repo = SubsystemStateRepo(session)
        onset = now()
        await repo.set(device.id, "psu", "CRITICAL", onset)
        await repo.set(device.id, "thermal", "OK", None)
        bad = await repo.non_ok()
        assert [(s.subsystem, s.severity) for s in bad] == [("psu", "CRITICAL")]
        assert await repo.non_ok(subsystem="thermal") == []
        # recovery clears onset
        await repo.set(device.id, "psu", "OK", None)
        assert await repo.non_ok() == []
        state = await repo.get(device.id, "psu")
        assert state.onset_at is None


class TestDomains:
    async def test_create_members_confirm(self, session, site, device):
        repo = DomainRepo(session)
        d = await repo.create(site.id, "pdu-a", "power")
        assert (d.status, d.confidence, d.source) == ("inferred", 0.6, "inference")
        await repo.set_members(d.id, [device.id], added_by="inference")
        assert await repo.members(d.id) == [device.id]
        found = await repo.domains_for_device(device.id, kind="power")
        assert [x.id for x in found] == [d.id]
        assert await repo.domains_for_device(device.id, kind="cooling") == []

        await repo.confirm(d, by="operator@site")
        assert d.status == "confirmed"
        assert d.confidence == 1.0
        assert d.confirmed_by == "operator@site"
        assert d.confirmed_at is not None

    async def test_set_members_diff(self, session, site, device):
        dev2 = await DeviceRepo(session).upsert_registration(
            site_id=site.id, agent_id="agent-2"
        )
        repo = DomainRepo(session)
        d = await repo.create(site.id, "pdu-b", "power")
        await repo.set_members(d.id, [device.id, dev2.id])
        await repo.set_members(d.id, [dev2.id])
        assert await repo.members(d.id) == [dev2.id]


class TestIncidents:
    async def test_device_incident_dedup_and_parent(self, session, site, device):
        repo = IncidentRepo(session)
        child = await repo.create(
            site_id=site.id, kind="device", device_id=device.id, subsystem="psu"
        )
        assert (await repo.open_device_incident(device.id, "psu")).id == child.id
        assert await repo.open_device_incident(device.id, "thermal") is None

        parent = await repo.create(
            site_id=site.id, kind="shared_power", domain_id="dom1",
            subsystem="psu", confidence=1.0, inferred=False,
        )
        child.parent_id = parent.id
        assert (await repo.open_parent("shared_power", domain_id="dom1")).id == parent.id
        assert await repo.open_parent("shared_power", domain_id="other") is None
        assert [c.id for c in await repo.children(parent.id)] == [child.id]

        await repo.resolve(child)
        assert child.status == "resolved"
        assert child.resolved_at is not None
        assert await repo.open_device_incident(device.id, "psu") is None
        assert [i.id for i in await repo.list_open()] == [parent.id]
        assert len(await repo.list_all()) == 2


class TestActions:
    async def test_unique_agent_action(self, session, device):
        session.add(ActionRow(device_id=device.id, agent_action_id="a1", type="fan_boost"))
        await session.flush()
        session.add(ActionRow(device_id=device.id, agent_action_id="a1", type="fan_boost"))
        with pytest.raises(IntegrityError):
            await session.flush()

    async def test_lifecycle_queries(self, session, device):
        repo = ActionRepo(session)
        row = ActionRow(device_id=device.id, agent_action_id="a2", type="fan_boost")
        session.add(row)
        await session.flush()

        assert (await repo.get_by_agent_action(device.id, "a2")).id == row.id
        assert [a.id for a in await repo.list_by_status("pending")] == [row.id]

        row.status = "approved"
        row.decision = "approved"
        row.decided_by = "op"
        row.decided_at = now()
        await session.flush()
        pending_delivery = await repo.undelivered_decisions(device.id)
        assert [a.id for a in pending_delivery] == [row.id]

        row.delivered_at = now()
        await session.flush()
        assert await repo.undelivered_decisions(device.id) == []


class TestAudit:
    async def test_append_ordered(self, session):
        repo = AuditRepo(session)
        await repo.append("op", "action.approve", "act-1", {"note": "ok"})
        await repo.append("op", "domain.confirm", "dom-1")
        rows = await repo.list_all()
        assert [r.action for r in rows] == ["action.approve", "domain.confirm"]
        assert rows[0].detail == {"note": "ok"}
