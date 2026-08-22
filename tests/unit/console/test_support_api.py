"""Phase 5 support API, audit export, and support mode tests."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from harkeniq_console.db.repos import (
    AuditRepo,
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
async def ticket(session, tenant):
    return await SupportTicketRepo(session).create(
        tenant_id=tenant.id, ticket_number=1, subject="Server down",
        body="Not responding", severity="S1", component="Agent",
        created_by="user1", sla_due_at=utcnow() + timedelta(hours=4),
    )


# ── Ticket lifecycle tests ───────────────────────────────────────────


class TestTicketLifecycle:
    async def test_create_and_close(self, session, tenant):
        repo = SupportTicketRepo(session)
        t = await repo.create(
            tenant_id=tenant.id, ticket_number=1, subject="Test",
            severity="S3", component="Other", created_by="u1",
        )
        assert t.status == "open"
        await repo.update(t, status="closed", closed_at=utcnow())
        assert t.status == "closed"
        assert t.closed_at is not None

    async def test_status_transitions(self, session, ticket):
        repo = SupportTicketRepo(session)
        sc_repo = TicketStateChangeRepo(session)

        await repo.update(ticket, status="acknowledged")
        await sc_repo.append(ticket_id=ticket.id, from_status="open", to_status="acknowledged", changed_by="s1")

        await repo.update(ticket, status="in_progress")
        await sc_repo.append(ticket_id=ticket.id, from_status="acknowledged", to_status="in_progress", changed_by="s1")

        await repo.update(ticket, status="waiting_on_tenant")
        await sc_repo.append(ticket_id=ticket.id, from_status="in_progress", to_status="waiting_on_tenant", changed_by="s1")

        await repo.update(ticket, status="closed", closed_at=utcnow())
        await sc_repo.append(ticket_id=ticket.id, from_status="waiting_on_tenant", to_status="closed", changed_by="s1")

        changes = await sc_repo.list_by_ticket(ticket.id)
        assert len(changes) == 4
        assert changes[-1].to_status == "closed"

    async def test_message_thread(self, session, ticket):
        msg_repo = TicketMessageRepo(session)
        await msg_repo.create(ticket_id=ticket.id, author_id="u1", author_email="u@a.com", body="Help needed")
        await msg_repo.create(ticket_id=ticket.id, author_id="s1", author_email="s@h.com", body="Looking into it")
        await msg_repo.create(ticket_id=ticket.id, author_id="s1", author_email="s@h.com", body="Internal: checking logs", is_internal=True)
        await msg_repo.create(ticket_id=ticket.id, author_id="u1", author_email="u@a.com", body="Thanks")

        # tenant view (no internal)
        tenant_msgs = await msg_repo.list_by_ticket(ticket.id, include_internal=False)
        assert len(tenant_msgs) == 3

        # support view (with internal)
        support_msgs = await msg_repo.list_by_ticket(ticket.id, include_internal=True)
        assert len(support_msgs) == 4

    async def test_sla_assignment(self, session, tenant):
        repo = SupportTicketRepo(session)
        s1 = await repo.create(
            tenant_id=tenant.id, ticket_number=1, subject="S1",
            severity="S1", component="Other", created_by="u1",
            sla_due_at=utcnow() + timedelta(hours=4),
        )
        s4 = await repo.create(
            tenant_id=tenant.id, ticket_number=2, subject="S4",
            severity="S4", component="Other", created_by="u1",
            sla_due_at=utcnow() + timedelta(hours=72),
        )
        # S1 has shorter SLA than S4
        assert s1.sla_due_at < s4.sla_due_at

    async def test_assign_to_support(self, session, ticket):
        repo = SupportTicketRepo(session)
        await repo.update(ticket, assigned_to="support1")
        assert ticket.assigned_to == "support1"

        items, _ = await repo.list_all(assigned_to="support1")
        assert len(items) == 1

    async def test_severity_filter_in_queue(self, session, tenant):
        repo = SupportTicketRepo(session)
        await repo.create(tenant_id=tenant.id, ticket_number=1, subject="S1", severity="S1", component="Other", created_by="u1")
        await repo.create(tenant_id=tenant.id, ticket_number=2, subject="S3", severity="S3", component="Other", created_by="u1")
        await repo.create(tenant_id=tenant.id, ticket_number=3, subject="S1", severity="S1", component="Other", created_by="u1")

        items, total = await repo.list_all(severity="S1")
        assert total == 2


# ── Support mode (24h access) tests ──────────────────────────────────


class TestSupportMode:
    async def test_enable_24h_access(self, session, tenant):
        now = utcnow()
        repo = SupportAccessLogRepo(session)
        entry = await repo.create(
            tenant_id=tenant.id, enabled_by="support1",
            enabled_at=now, expires_at=now + timedelta(hours=24),
        )
        active = await repo.get_active(tenant.id)
        assert active is not None
        assert active.id == entry.id

    async def test_auto_expire(self, session, tenant):
        now = utcnow()
        repo = SupportAccessLogRepo(session)
        await repo.create(
            tenant_id=tenant.id, enabled_by="s1",
            enabled_at=now - timedelta(hours=25),
            expires_at=now - timedelta(hours=1),
        )
        active = await repo.get_active(tenant.id)
        assert active is None

    async def test_manual_revoke(self, session, tenant):
        now = utcnow()
        repo = SupportAccessLogRepo(session)
        entry = await repo.create(
            tenant_id=tenant.id, enabled_by="s1",
            enabled_at=now, expires_at=now + timedelta(hours=24),
        )
        await repo.revoke(entry, "s2")
        active = await repo.get_active(tenant.id)
        assert active is None

    async def test_audit_logged_on_enable(self, session, tenant):
        now = utcnow()
        await SupportAccessLogRepo(session).create(
            tenant_id=tenant.id, enabled_by="s1",
            enabled_at=now, expires_at=now + timedelta(hours=24),
        )
        await AuditRepo(session).append(
            actor_id="s1", actor_email="s@h.com",
            action="support_access.enabled",
            subject_type="tenant", subject_id=tenant.id,
            tenant_id=tenant.id,
        )
        logs, _ = await AuditRepo(session).list_filtered(
            tenant_id=tenant.id, action="support_access.enabled",
        )
        assert len(logs) == 1


# ── Audit log query tests ────────────────────────────────────────────


class TestAuditLogQueries:
    async def test_list_filtered_by_tenant(self, session, tenant):
        repo = AuditRepo(session)
        await repo.append(actor_id="u1", actor_email="u@a.com", action="test.action", tenant_id=tenant.id)
        await repo.append(actor_id="u2", actor_email="u2@a.com", action="other.action", tenant_id="other")
        items, total = await repo.list_filtered(tenant_id=tenant.id)
        assert total == 1

    async def test_list_filtered_by_action(self, session, tenant):
        repo = AuditRepo(session)
        await repo.append(actor_id="u1", actor_email="u@a.com", action="ticket.created", tenant_id=tenant.id)
        await repo.append(actor_id="u1", actor_email="u@a.com", action="ticket.closed", tenant_id=tenant.id)
        items, total = await repo.list_filtered(tenant_id=tenant.id, action="ticket.created")
        assert total == 1

    async def test_list_filtered_by_actor(self, session, tenant):
        repo = AuditRepo(session)
        await repo.append(actor_id="u1", actor_email="admin@acme.com", action="test", tenant_id=tenant.id)
        await repo.append(actor_id="u2", actor_email="user@acme.com", action="test", tenant_id=tenant.id)
        items, total = await repo.list_filtered(tenant_id=tenant.id, actor="admin")
        assert total == 1

    async def test_list_filtered_by_date_range(self, session, tenant):
        repo = AuditRepo(session)
        await repo.append(actor_id="u1", actor_email="u@a.com", action="test", tenant_id=tenant.id)
        now = utcnow()
        items, total = await repo.list_filtered(
            tenant_id=tenant.id,
            date_from=now - timedelta(hours=1),
            date_to=now + timedelta(hours=1),
        )
        assert total == 1

    async def test_list_filtered_search(self, session, tenant):
        repo = AuditRepo(session)
        await repo.append(actor_id="u1", actor_email="u@a.com", action="invoice.generated", subject_type="invoice", tenant_id=tenant.id)
        await repo.append(actor_id="u1", actor_email="u@a.com", action="ticket.created", subject_type="ticket", tenant_id=tenant.id)
        items, total = await repo.list_filtered(tenant_id=tenant.id, search="invoice")
        assert total == 1

    async def test_list_pagination(self, session, tenant):
        repo = AuditRepo(session)
        for i in range(15):
            await repo.append(actor_id="u1", actor_email="u@a.com", action=f"action.{i}", tenant_id=tenant.id)
        items, total = await repo.list_filtered(tenant_id=tenant.id, page=1, page_size=10)
        assert total == 15
        assert len(items) == 10
        items2, _ = await repo.list_filtered(tenant_id=tenant.id, page=2, page_size=10)
        assert len(items2) == 5

    async def test_platform_wide_listing(self, session, tenant):
        repo = AuditRepo(session)
        await repo.append(actor_id="u1", actor_email="u@a.com", action="test", tenant_id=tenant.id)
        await repo.append(actor_id="u2", actor_email="u2@b.com", action="test", tenant_id="other_tenant")
        items, total = await repo.list_filtered()  # no tenant filter
        assert total == 2

    async def test_append_with_detail(self, session, tenant):
        repo = AuditRepo(session)
        entry = await repo.append(
            actor_id="u1", actor_email="u@a.com", action="test",
            tenant_id=tenant.id, detail={"key": "value", "count": 42},
        )
        assert entry.detail["key"] == "value"
        assert entry.detail["count"] == 42

    async def test_immutable_view_only(self, session, tenant):
        """Audit logs are append-only: verify we only have create/list operations."""
        repo = AuditRepo(session)
        # AuditRepo has append and list_filtered, no update/delete
        assert hasattr(repo, "append")
        assert hasattr(repo, "list_filtered")
        assert not hasattr(repo, "delete")
        assert not hasattr(repo, "update")
