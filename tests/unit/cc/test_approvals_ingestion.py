"""C2 (final assessment, P0 2026-08-29): the approvals ingestion hop.

Before this fix the fleet poller decoded FleetSnapshot.pending_actions and
discarded them — cc_approval_routes was written only by tests, so
GET /api/approvals/ was empty against any real deployment and approve/deny
always 404ed. These tests pin the reconciliation semantics:

- pending actions become undecided routes, idempotently;
- undecided routes whose action no longer waits at the SM are closed as
  "superseded" (decided locally at the SM, or expired) — never left as
  approvable ghosts;
- routes already decided at CC are never touched by reconciliation.
"""

from __future__ import annotations

import pytest

from harkeniq_cc.db.models import CCSite
from harkeniq_cc.db.repos import ApprovalRouteRepo
from harkeniq_cc.fleet_poller import _ingest_pending_actions

TENANT = "t1"


async def _seed_site(session) -> str:
    site = CCSite(
        tenant_id=TENANT, site_name="site-1",
        sm_endpoint="sm:50051", sm_token="tok",
    )
    session.add(site)
    await session.flush()
    return site.id


def _pending(action_id: str, action_type: str = "SEL_CLEAR") -> dict:
    return {
        "action_id": action_id,
        "type": action_type,
        "device_agent_id": "agent-1",
        "severity": "low",
        "skill_name": "fan-health",
        "status": "pending",
        "proposed_at_unix": 1787000000,
    }


class TestPendingActionIngestion:
    async def test_pending_actions_become_routes(self, session):
        site_id = await _seed_site(session)
        await _ingest_pending_actions(
            session, site_id, [_pending("act-1"), _pending("act-2", "BMC_RESET")],
        )
        repo = ApprovalRouteRepo(session)
        routes = await repo.list_pending(TENANT)
        assert {r.action_id for r in routes} == {"act-1", "act-2"}
        by_id = {r.action_id: r for r in routes}
        assert by_id["act-2"].action_type == "BMC_RESET"
        assert by_id["act-1"].device_agent_id == "agent-1"
        assert all(r.decision is None for r in routes)

    async def test_ingestion_is_idempotent(self, session):
        site_id = await _seed_site(session)
        for _ in range(3):  # three poll cycles, same pending action
            await _ingest_pending_actions(session, site_id, [_pending("act-1")])
        routes = await ApprovalRouteRepo(session).list_pending(TENANT)
        assert len(routes) == 1

    async def test_vanished_actions_are_superseded(self, session):
        site_id = await _seed_site(session)
        await _ingest_pending_actions(
            session, site_id, [_pending("act-1"), _pending("act-2")],
        )
        # Next cycle: act-1 was decided at the SM (CLI/dashboard) and no
        # longer appears in the pending list.
        await _ingest_pending_actions(session, site_id, [_pending("act-2")])
        repo = ApprovalRouteRepo(session)
        pending = await repo.list_pending(TENANT)
        assert [r.action_id for r in pending] == ["act-2"]
        gone = await repo.get_by_action_id("act-1")
        assert gone is not None
        assert gone.decision == "superseded"
        assert gone.decided_by == "sm"

    async def test_cc_decided_routes_are_untouched(self, session):
        site_id = await _seed_site(session)
        await _ingest_pending_actions(session, site_id, [_pending("act-1")])
        repo = ApprovalRouteRepo(session)
        route = await repo.get_by_action_id("act-1")
        await repo.update_decision(route, "approved", "op@example.com")
        # The SM still lists it pending (decision not yet delivered) — the
        # reconciler must not duplicate it or overwrite the decision.
        await _ingest_pending_actions(session, site_id, [_pending("act-1")])
        route = await repo.get_by_action_id("act-1")
        assert route.decision == "approved"
        assert route.decided_by == "op@example.com"
        assert len(await repo.list_undecided_for_site(site_id)) == 0

    async def test_actions_without_id_are_skipped(self, session):
        site_id = await _seed_site(session)
        await _ingest_pending_actions(
            session, site_id, [{"type": "SEL_CLEAR"}, _pending("act-9")],
        )
        routes = await ApprovalRouteRepo(session).list_pending(TENANT)
        assert [r.action_id for r in routes] == ["act-9"]
