"""Phase 5 support repository tests: SupportTicket, TicketMessage,
TicketStateChange, SupportAccessLog.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from harkeniq_console.db.repos import (
    SupportAccessLogRepo,
    SupportTicketRepo,
    TenantRepo,
    TicketMessageRepo,
    TicketStateChangeRepo,
)


def utcnow():
    return datetime.now(timezone.utc)


@pytest.fixture
async def tenant(session):
    return await TenantRepo(session).create(
        slug="acme", name="Acme Corp", billing_country="US",
    )


@pytest.fixture
async def second_tenant(session):
    return await TenantRepo(session).create(
        slug="globex", name="Globex Corp", billing_country="IN",
    )


@pytest.fixture
async def ticket(session, tenant):
    return await SupportTicketRepo(session).create(
        tenant_id=tenant.id, ticket_number=1, subject="Server down",
        body="DC-East server not responding", severity="S1",
        component="Agent", created_by="user1",
        sla_due_at=utcnow() + timedelta(hours=4),
    )


# ── SupportTicketRepo ────────────────────────────────────────────────


class TestSupportTicketRepo:
    async def test_create(self, session, tenant):
        repo = SupportTicketRepo(session)
        t = await repo.create(
            tenant_id=tenant.id, ticket_number=1, subject="Test",
            severity="S3", component="Other", created_by="u1",
        )
        assert t.id
        assert t.status == "open"
        assert t.severity == "S3"

    async def test_get_by_id(self, session, ticket):
        found = await SupportTicketRepo(session).get_by_id(ticket.id)
        assert found is not None
        assert found.subject == "Server down"

    async def test_get_by_id_not_found(self, session):
        assert await SupportTicketRepo(session).get_by_id("nope") is None

    async def test_next_ticket_number(self, session, tenant):
        repo = SupportTicketRepo(session)
        assert await repo.next_ticket_number(tenant.id) == 1
        await repo.create(
            tenant_id=tenant.id, ticket_number=1, subject="T1",
            severity="S3", component="Other", created_by="u1",
        )
        assert await repo.next_ticket_number(tenant.id) == 2

    async def test_next_ticket_number_per_tenant(self, session, tenant, second_tenant):
        repo = SupportTicketRepo(session)
        await repo.create(
            tenant_id=tenant.id, ticket_number=1, subject="T1",
            severity="S3", component="Other", created_by="u1",
        )
        assert await repo.next_ticket_number(second_tenant.id) == 1

    async def test_list_by_tenant(self, session, tenant):
        repo = SupportTicketRepo(session)
        for i in range(3):
            await repo.create(
                tenant_id=tenant.id, ticket_number=i + 1,
                subject=f"Ticket {i}", severity="S3", component="Other",
                created_by="u1",
            )
        items, total = await repo.list_by_tenant(tenant.id)
        assert total == 3
        assert len(items) == 3

    async def test_list_by_tenant_status_filter(self, session, tenant):
        repo = SupportTicketRepo(session)
        await repo.create(
            tenant_id=tenant.id, ticket_number=1, subject="Open",
            severity="S3", component="Other", created_by="u1",
        )
        t2 = await repo.create(
            tenant_id=tenant.id, ticket_number=2, subject="Closed",
            severity="S3", component="Other", created_by="u1",
            status="closed",
        )
        items, total = await repo.list_by_tenant(tenant.id, status="closed")
        assert total == 1
        assert items[0].status == "closed"

    async def test_list_by_tenant_severity_filter(self, session, tenant):
        repo = SupportTicketRepo(session)
        await repo.create(tenant_id=tenant.id, ticket_number=1, subject="S1", severity="S1", component="Other", created_by="u1")
        await repo.create(tenant_id=tenant.id, ticket_number=2, subject="S3", severity="S3", component="Other", created_by="u1")
        items, total = await repo.list_by_tenant(tenant.id, severity="S1")
        assert total == 1

    async def test_list_by_tenant_search(self, session, tenant):
        repo = SupportTicketRepo(session)
        await repo.create(tenant_id=tenant.id, ticket_number=1, subject="CPU overheating", severity="S2", component="Agent", created_by="u1")
        await repo.create(tenant_id=tenant.id, ticket_number=2, subject="Billing question", severity="S4", component="Billing", created_by="u1")
        items, total = await repo.list_by_tenant(tenant.id, search="CPU")
        assert total == 1
        assert "CPU" in items[0].subject

    async def test_list_all(self, session, tenant, second_tenant):
        repo = SupportTicketRepo(session)
        await repo.create(tenant_id=tenant.id, ticket_number=1, subject="T1", severity="S3", component="Other", created_by="u1")
        await repo.create(tenant_id=second_tenant.id, ticket_number=1, subject="T2", severity="S2", component="SM", created_by="u2")
        items, total = await repo.list_all()
        assert total == 2

    async def test_list_all_assigned_filter(self, session, tenant):
        repo = SupportTicketRepo(session)
        await repo.create(tenant_id=tenant.id, ticket_number=1, subject="T1", severity="S3", component="Other", created_by="u1", assigned_to="support1")
        await repo.create(tenant_id=tenant.id, ticket_number=2, subject="T2", severity="S3", component="Other", created_by="u1")
        items, total = await repo.list_all(assigned_to="support1")
        assert total == 1

    async def test_update(self, session, ticket):
        repo = SupportTicketRepo(session)
        updated = await repo.update(ticket, status="acknowledged", assigned_to="support1")
        assert updated.status == "acknowledged"
        assert updated.assigned_to == "support1"

    async def test_count_open(self, session, tenant):
        repo = SupportTicketRepo(session)
        await repo.create(tenant_id=tenant.id, ticket_number=1, subject="Open", severity="S3", component="Other", created_by="u1", status="open")
        await repo.create(tenant_id=tenant.id, ticket_number=2, subject="InProg", severity="S3", component="Other", created_by="u1", status="in_progress")
        await repo.create(tenant_id=tenant.id, ticket_number=3, subject="Closed", severity="S3", component="Other", created_by="u1", status="closed")
        assert await repo.count_open(tenant.id) == 2

    async def test_count_open_all_tenants(self, session, tenant, second_tenant):
        repo = SupportTicketRepo(session)
        await repo.create(tenant_id=tenant.id, ticket_number=1, subject="T1", severity="S3", component="Other", created_by="u1", status="open")
        await repo.create(tenant_id=second_tenant.id, ticket_number=1, subject="T2", severity="S3", component="Other", created_by="u2", status="acknowledged")
        assert await repo.count_open() == 2

    async def test_tenant_isolation(self, session, tenant, second_tenant):
        repo = SupportTicketRepo(session)
        await repo.create(tenant_id=tenant.id, ticket_number=1, subject="T1", severity="S3", component="Other", created_by="u1")
        await repo.create(tenant_id=second_tenant.id, ticket_number=1, subject="T2", severity="S3", component="Other", created_by="u2")
        items, total = await repo.list_by_tenant(tenant.id)
        assert total == 1


# ── TicketMessageRepo ────────────────────────────────────────────────


class TestTicketMessageRepo:
    async def test_create(self, session, ticket):
        msg = await TicketMessageRepo(session).create(
            ticket_id=ticket.id, author_id="u1", author_email="u@acme.com",
            body="Hello",
        )
        assert msg.id
        assert msg.is_internal is False

    async def test_create_internal(self, session, ticket):
        msg = await TicketMessageRepo(session).create(
            ticket_id=ticket.id, author_id="support1",
            author_email="s@harkeniq.com", body="Internal note",
            is_internal=True,
        )
        assert msg.is_internal is True

    async def test_list_by_ticket_excludes_internal(self, session, ticket):
        repo = TicketMessageRepo(session)
        await repo.create(ticket_id=ticket.id, author_id="u1", author_email="u@a.com", body="Public")
        await repo.create(ticket_id=ticket.id, author_id="s1", author_email="s@h.com", body="Internal", is_internal=True)
        msgs = await repo.list_by_ticket(ticket.id, include_internal=False)
        assert len(msgs) == 1
        assert msgs[0].body == "Public"

    async def test_list_by_ticket_includes_internal(self, session, ticket):
        repo = TicketMessageRepo(session)
        await repo.create(ticket_id=ticket.id, author_id="u1", author_email="u@a.com", body="Public")
        await repo.create(ticket_id=ticket.id, author_id="s1", author_email="s@h.com", body="Internal", is_internal=True)
        msgs = await repo.list_by_ticket(ticket.id, include_internal=True)
        assert len(msgs) == 2

    async def test_list_ordered_by_created_at(self, session, ticket):
        repo = TicketMessageRepo(session)
        await repo.create(ticket_id=ticket.id, author_id="u1", author_email="u@a.com", body="First")
        await repo.create(ticket_id=ticket.id, author_id="u1", author_email="u@a.com", body="Second")
        msgs = await repo.list_by_ticket(ticket.id)
        assert msgs[0].body == "First"
        assert msgs[1].body == "Second"

    async def test_list_empty(self, session, ticket):
        msgs = await TicketMessageRepo(session).list_by_ticket(ticket.id)
        assert len(msgs) == 0


# ── TicketStateChangeRepo ────────────────────────────────────────────


class TestTicketStateChangeRepo:
    async def test_append(self, session, ticket):
        sc = await TicketStateChangeRepo(session).append(
            ticket_id=ticket.id, from_status="open",
            to_status="acknowledged", changed_by="support1",
        )
        assert sc.id
        assert sc.to_status == "acknowledged"

    async def test_list_by_ticket(self, session, ticket):
        repo = TicketStateChangeRepo(session)
        await repo.append(ticket_id=ticket.id, from_status="open", to_status="acknowledged", changed_by="s1")
        await repo.append(ticket_id=ticket.id, from_status="acknowledged", to_status="in_progress", changed_by="s1")
        changes = await repo.list_by_ticket(ticket.id)
        assert len(changes) == 2
        assert changes[0].to_status == "acknowledged"
        assert changes[1].to_status == "in_progress"

    async def test_list_empty(self, session, ticket):
        changes = await TicketStateChangeRepo(session).list_by_ticket(ticket.id)
        assert len(changes) == 0


# ── SupportAccessLogRepo ─────────────────────────────────────────────


class TestSupportAccessLogRepo:
    async def test_create(self, session, tenant):
        now = utcnow()
        entry = await SupportAccessLogRepo(session).create(
            tenant_id=tenant.id, enabled_by="support1", status="approved",
            enabled_at=now, expires_at=now + timedelta(hours=24),
        )
        assert entry.id
        assert entry.revoked_at is None

    async def test_get_active(self, session, tenant):
        now = utcnow()
        await SupportAccessLogRepo(session).create(
            tenant_id=tenant.id, enabled_by="s1", status="approved",
            enabled_at=now, expires_at=now + timedelta(hours=24),
        )
        active = await SupportAccessLogRepo(session).get_active(tenant.id)
        assert active is not None

    async def test_get_active_expired(self, session, tenant):
        now = utcnow()
        await SupportAccessLogRepo(session).create(
            tenant_id=tenant.id, enabled_by="s1", status="approved",
            enabled_at=now - timedelta(hours=25),
            expires_at=now - timedelta(hours=1),
        )
        active = await SupportAccessLogRepo(session).get_active(tenant.id)
        assert active is None

    async def test_get_active_revoked(self, session, tenant):
        now = utcnow()
        entry = await SupportAccessLogRepo(session).create(
            tenant_id=tenant.id, enabled_by="s1", status="approved",
            enabled_at=now, expires_at=now + timedelta(hours=24),
        )
        await SupportAccessLogRepo(session).revoke(entry, "s1")
        active = await SupportAccessLogRepo(session).get_active(tenant.id)
        assert active is None

    async def test_revoke(self, session, tenant):
        now = utcnow()
        repo = SupportAccessLogRepo(session)
        entry = await repo.create(
            tenant_id=tenant.id, enabled_by="s1", status="approved",
            enabled_at=now, expires_at=now + timedelta(hours=24),
        )
        revoked = await repo.revoke(entry, "s2")
        assert revoked.revoked_at is not None
        assert revoked.revoked_by == "s2"

    async def test_list_by_tenant(self, session, tenant):
        now = utcnow()
        repo = SupportAccessLogRepo(session)
        await repo.create(tenant_id=tenant.id, enabled_by="s1", status="approved", enabled_at=now, expires_at=now + timedelta(hours=24))
        await repo.create(tenant_id=tenant.id, enabled_by="s2", status="approved", enabled_at=now - timedelta(days=7), expires_at=now - timedelta(days=6))
        entries = await repo.list_by_tenant(tenant.id)
        assert len(entries) == 2
        # most recent first
        assert entries[0].enabled_by == "s1"

    async def test_tenant_isolation(self, session, tenant, second_tenant):
        now = utcnow()
        repo = SupportAccessLogRepo(session)
        await repo.create(tenant_id=tenant.id, enabled_by="s1", status="approved", enabled_at=now, expires_at=now + timedelta(hours=24))
        entries = await repo.list_by_tenant(second_tenant.id)
        assert len(entries) == 0

    async def test_get_active_not_found(self, session, tenant):
        active = await SupportAccessLogRepo(session).get_active(tenant.id)
        assert active is None
