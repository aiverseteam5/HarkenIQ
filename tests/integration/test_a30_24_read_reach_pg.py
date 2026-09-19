"""A6-4B0b-S2 (A30.24) on a REAL PostgreSQL: no permission x scope cross-product.

The sqlite matrix lives in tests/unit/cc/test_a30_24_permission_aware_reach.py.
This file asks what sqlite cannot answer honestly. `permission_subset` is
JSONB here and a grant's lifecycle is timestamptz, and the read filter is
an `IN (...)` the engine production runs actually plans -- so: with

    grant A   site-1   the full role
    grant B   site-2   permission_subset = ["incident.view"]
    grant C   site-3   the full role, EXPIRED
    grant D   tenant   the full role, under ANOTHER tenant

does every read take its reach from the grants that carry the permission
it requires? site-2 must answer `incident.view` reads and nothing else;
site-3 and the foreign tenant grant must answer nothing; and the paginated
totals must agree with the rows, because a count that ignored the filter
would leak the size of what the rows withhold.

Every assertion is paired with a CONTROL principal holding the same grants
with nothing withheld, so an empty answer cannot pass as a correct one.

Audit rows are deliberately NOT cleaned up: the chain is append-only.

Gated on ``HARKEN_TEST_CC_PG_DSN``.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.app import create_app
from harkeniq_cc.auth import ROLE_PERMISSIONS, UserContext, configure_auth
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import make_engine, make_sessionmaker
from harkeniq_cc.db.models import (
    CCApprovalRoute, CCFleetCache, CCIncident, CCOutcomeHistory, CCScopeGrant,
    CCSite,
)
from harkeniq_cc.db.repos import AuditRepo, ScopeGrantRepo
from harkeniq_cc.governance import load_scope
from harkeniq_cc.runtime import AppState
from harkeniq_cc.scope import SCOPE_SITE, SCOPE_TENANT, read_reach

from tests.unit.cc.conftest import seed_tenant_admin

DSN = os.environ.get("HARKEN_TEST_CC_PG_DSN", "")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not DSN, reason="HARKEN_TEST_CC_PG_DSN not set"),
]

MIXED, CONTROL = "kc-mixed", "kc-control"
PAST = datetime.now(timezone.utc) - timedelta(hours=1)


class Stack:
    def __init__(self, app, state, tenant):
        self.app, self.state, self.tenant = app, state, tenant
        self.sessionmaker = state.sessionmaker
        self.sites: dict[str, str] = {}
        self.tag = uuid.uuid4().hex[:8]

    def client(self, sub):
        async def _fake():
            return UserContext(
                user_id=sub, email=f"{sub}@example.com", tenant_id=self.tenant,
                role="tenant_owner",
                permissions=list(ROLE_PERMISSIONS["tenant_owner"]),
            )

        self.app.dependency_overrides[get_current_user] = _fake
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test",
        )

    def marker(self, kind, site):
        return f"{kind}-{site}-{self.tag}"


async def _stack() -> Stack:
    tenant = f"s2-{uuid.uuid4().hex[:10]}"
    configure_auth("", "", "", insecure=True)
    engine = make_engine(DSN)
    sessionmaker = make_sessionmaker(engine)
    state = AppState(
        config=CCConfig(tenant_id=tenant, insecure=True),
        engine=engine, sessionmaker=sessionmaker,
    )
    app = create_app(state)
    # A tenant with a founding administrator is STRICT from birth (A23.11).
    await seed_tenant_admin(sessionmaker, tenant, "kc-owner")
    stack = Stack(app, state, tenant)

    async with sessionmaker() as session:
        for name in ("site-1", "site-2", "site-3"):
            site = CCSite(tenant_id=tenant, site_name=f"{name}-{stack.tag}",
                          sm_endpoint="sm:50051", sm_token="tok")
            session.add(site)
            await session.flush()
            stack.sites[name] = site.id
            node = stack.marker("node", name)
            session.add(CCFleetCache(
                site_id=site.id, agent_id=node, agent_name=node, vendor="Dell",
                model=stack.marker("model", name), device_class="server",
                health="ok",
            ))
            session.add(CCIncident(
                incident_id=stack.marker("inc", name), tenant_id=tenant,
                site_id=site.id, device_agent_id=node, status="open",
                kind="device", title=f"incident at {name}",
            ))
            session.add(CCApprovalRoute(
                site_id=site.id, action_id=stack.marker("pending", name),
                action_type="SEL_CLEAR", device_agent_id=node,
            ))
            session.add(CCApprovalRoute(
                site_id=site.id, action_id=stack.marker("decided", name),
                action_type="SEL_CLEAR", device_agent_id=node,
                decision="approved", decided_by="kc-owner",
                decided_at=datetime.now(timezone.utc),
            ))
            session.add(CCOutcomeHistory(
                site_id=site.id, action_id=f"directive:{node}",
                action_type="SEL_CLEAR", device_agent_id=node, vendor="Dell",
                model=stack.marker("model", name), outcome="SUCCESS",
            ))
            await AuditRepo(session).append(
                actor="seed", action="seed.site",
                subject=stack.marker("audit", name), tenant_id=tenant,
                site_id=site.id,
            )
        await session.commit()

    for principal, narrowed in ((MIXED, ["incident.view"]), (CONTROL, None)):
        async with sessionmaker() as session:
            repo = ScopeGrantRepo(session)
            await repo.grant(
                tenant_id=tenant, principal_type="user", principal_ref=principal,
                scope_type=SCOPE_SITE, scope_ref=stack.sites["site-1"],
                role="tenant_owner", granted_by="kc-owner",
            )
            await repo.grant(
                tenant_id=tenant, principal_type="user", principal_ref=principal,
                scope_type=SCOPE_SITE, scope_ref=stack.sites["site-2"],
                role="tenant_owner", granted_by="kc-owner",
                permission_subset=narrowed,
            )
            # C: the full role at site-3, and it has lapsed (timestamptz).
            session.add(CCScopeGrant(
                tenant_id=tenant, principal_type="user", principal_ref=principal,
                scope_type=SCOPE_SITE, scope_ref=stack.sites["site-3"],
                role="tenant_owner", granted_by="kc-owner", expires_at=PAST,
            ))
            # D: everything, everywhere -- in somebody else's tenant.
            session.add(CCScopeGrant(
                tenant_id=f"other-{stack.tag}", principal_type="user",
                principal_ref=principal, scope_type=SCOPE_TENANT, scope_ref="",
                role="tenant_owner", granted_by="kc-owner",
            ))
            await session.commit()
    return stack


#: path -> (marker kind, the permission its guard requires)
READS = {
    "/api/fleet/": ("node", "fleet.view"),
    "/api/agents/": ("node", "fleet.view"),
    "/api/predictive/risk": ("node", "fleet.view"),
    "/api/outcomes/metrics": ("model", "fleet.view"),
    "/api/incidents/": ("inc", "incident.view"),
    "/api/approvals/": ("pending", "action.approve"),
    "/api/approvals/history": ("decided", "action.approve"),
    "/api/audit/?page_size=200": ("audit", "audit.view"),
}


async def _visible_sites(stack, principal, path, kind) -> set[str]:
    async with stack.client(principal) as c:
        resp = await c.get(path)
    assert resp.status_code == 200, (path, resp.status_code, resp.text[:300])
    return {s for s in stack.sites if stack.marker(kind, s) in resp.text}


async def test_no_permission_scope_cross_product_on_postgres():
    stack = await _stack()
    for path, (kind, permission) in READS.items():
        control = await _visible_sites(stack, CONTROL, path, kind)
        assert control == {"site-1", "site-2"}, (
            f"control: {path} must show both live grants and neither the "
            f"expired nor the foreign-tenant one, saw {sorted(control)}"
        )
        expected = {"site-1", "site-2"} if permission == "incident.view" else {"site-1"}
        mixed = await _visible_sites(stack, MIXED, path, kind)
        assert mixed == expected, (path, permission, sorted(mixed))


async def test_the_jsonb_subset_round_trips_into_the_reach():
    """The resolver reads what PostgreSQL stored, not what was sent."""
    stack = await _stack()
    async with stack.sessionmaker() as session:
        scope = await load_scope(
            session, tenant_id=stack.tenant, principal_ref=MIXED,
            role_permissions=list(ROLE_PERMISSIONS["tenant_owner"]),
        )
    s1, s2, s3 = (stack.sites[n] for n in ("site-1", "site-2", "site-3"))
    assert scope.site_ids == {s1, s2}, "the neutral projection keeps its meaning"
    assert read_reach(scope, "incident.view").site_ids == {s1, s2}
    for permission in ("fleet.view", "action.approve", "audit.view", "site.manage"):
        assert read_reach(scope, permission).site_ids == {s1}, permission
    assert read_reach(scope, "action.approve", "audit.view").site_ids == {s1}
    assert s3 not in read_reach(scope, "incident.view").site_ids
    assert not any(
        read_reach(scope, p).tenant_wide
        for p in ROLE_PERMISSIONS["tenant_owner"] if p != "*"
    ), "a foreign tenant's grant must never read as tenant-wide here"


async def test_counts_agree_with_rows():
    """A total that ignored the filter would leak what the rows withhold."""
    stack = await _stack()
    async with stack.client(MIXED) as c:
        fleet = (await c.get("/api/fleet/")).json()
        summary = (await c.get("/api/fleet/summary")).json()
        pending = (await c.get("/api/approvals/")).json()
        history = (await c.get("/api/approvals/history")).json()
        audit = (await c.get("/api/audit/?page_size=200")).json()
    assert fleet["total"] == len(fleet["devices"]) == 1
    assert (summary["total_nodes"], summary["sites_count"]) == (1, 1)
    # incidents_open rides the fleet.view route: it follows fleet.view.
    assert summary["incidents_open"] == 1
    assert pending["total"] == 1 and history["total"] == 1
    assert audit["total"] == len(audit["entries"]) == 1

    async with stack.client(CONTROL) as c:
        fleet = (await c.get("/api/fleet/")).json()
        summary = (await c.get("/api/fleet/summary")).json()
    assert fleet["total"] == len(fleet["devices"]) == 2
    assert (summary["total_nodes"], summary["sites_count"]) == (2, 2)


async def test_detail_reads_follow_the_same_grant():
    stack = await _stack()
    s2 = stack.sites["site-2"]
    inc = stack.marker("inc", "site-2")
    node = stack.marker("node", "site-2")
    pending = stack.marker("pending", "site-2")
    expected = {
        MIXED: {"incident": 200, "site": 404, "agent": 404, "records": 0},
        CONTROL: {"incident": 200, "site": 200, "agent": 200, "records": None},
    }
    for principal, want in expected.items():
        async with stack.client(principal) as c:
            assert (await c.get(f"/api/incidents/{inc}")).status_code == want["incident"]
            assert (await c.get(f"/api/sites/{s2}")).status_code == want["site"]
            assert (await c.get(f"/api/agents/{node}")).status_code == want["agent"]
            records = await c.get(f"/api/approvals/{pending}/records")
            assert records.status_code == 200
