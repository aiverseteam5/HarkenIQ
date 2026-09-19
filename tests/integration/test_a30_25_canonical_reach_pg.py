"""A6-4B0b (A30.25) on a REAL PostgreSQL: repository visibility == canonical reach.

The sqlite matrix lives in tests/unit/cc/test_a30_25_canonical_reach.py and
runs THIS SAME harness (`tests/unit/cc/b0b_matrix.py`). What sqlite cannot
answer honestly is the part that decides who reads what in production:

* `lower(device_class) IN (...)` against a column that stores `SWITCH`;
* the correlated `EXISTS` on `cc_fleet_cache`, joining a `VARCHAR(255)`
  agent id to `VARCHAR(64)` and `VARCHAR(255)` device columns;
* `permission_subset` as JSONB and a grant lifecycle as timestamptz;
* paginated COUNTs that must agree with the rows they count.

So: every persona x every device-bearing reader, on PostgreSQL, against the
canonical Python rule -- then the behaviours that matter most, over HTTP on
the same database. Each test owns a fresh tenant and a unique tag; rows are
left behind (the audit chain is append-only, like S1's and S2's).

Gated on ``HARKEN_TEST_CC_PG_DSN``.
"""

from __future__ import annotations

import json
import os
import uuid

import httpx
import pytest

from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.app import create_app
from harkeniq_cc.auth import ROLE_PERMISSIONS, UserContext, configure_auth
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import make_engine, make_sessionmaker
from harkeniq_cc.runtime import AppState

from tests.unit.cc import b0b_matrix as M
from tests.unit.cc.conftest import seed_tenant_admin

DSN = os.environ.get("HARKEN_TEST_CC_PG_DSN", "")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not DSN, reason="HARKEN_TEST_CC_PG_DSN not set"),
]


class Stack:
    def __init__(self, app, sessionmaker, estate):
        self.app, self.sessionmaker, self.estate = app, sessionmaker, estate

    def n(self, stem: str) -> str:
        return self.estate.name(stem)

    async def client(self, persona: str, role: str = "tenant_owner"):
        principal = await M.persist_persona(self.sessionmaker, self.estate, persona)

        async def _fake():
            return UserContext(
                user_id=principal, email=f"{principal}@example.com",
                tenant_id=self.estate.tenant, role=role,
                permissions=list(ROLE_PERMISSIONS[role]),
            )

        self.app.dependency_overrides[get_current_user] = _fake
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test",
        )


async def _stack() -> Stack:
    tenant = f"b0b-{uuid.uuid4().hex[:10]}"
    configure_auth("", "", "", insecure=True)
    engine = make_engine(DSN)
    sessionmaker = make_sessionmaker(engine)
    state = AppState(
        config=CCConfig(tenant_id=tenant, insecure=True),
        engine=engine, sessionmaker=sessionmaker,
    )
    app = create_app(state)
    # A tenant born with an administrator is STRICT (A23.11): nobody below
    # reads anything by synthesis.
    await seed_tenant_admin(sessionmaker, tenant, "kc-owner")
    return Stack(app, sessionmaker, await M.seed_estate(sessionmaker, tenant))


async def _get(client, path):
    resp = await client.get(path)
    assert resp.status_code == 200, (path, resp.status_code, resp.text[:300])
    return resp.json()


async def test_the_whole_matrix_on_postgres():
    stack = await _stack()
    result = await M.run_matrix(stack.sessionmaker, stack.estate)
    assert result.mismatches == [], "\n".join(result.mismatches)
    assert result.checked == len(M.PERSONAS) * len(M.READERS)

    tag = stack.estate.tag
    seen = lambda p, r: {k.removesuffix(f"-{tag}") for k in result.seen[(p, r)]}  # noqa: E731
    # F1 closed, on the production engine.
    assert seen("device-node-1", "fleet.list_all") == {"node-1"}
    assert seen("device-node-1", "incidents.list") == {"child-node-1", "inc-node-1"}
    # `lower()` on the COLUMN: the stored class is `SWITCH`.
    assert seen("class-switch", "fleet.list_all") == {"sw-1", "sw-2", "swup-2"}
    assert seen("class-SWITCH", "outcomes.list") == {"oc-sw-1", "oc-sw-2", "oc-swup-2"}
    # R1: the class-less device is in no class, and is read by id and site.
    assert "blank-1" not in seen("class-server", "fleet.list_all")
    assert seen("device-blank-1", "fleet.list_all") == {"blank-1"}
    # R9: a device that does not resolve at the row's site owns nothing.
    assert "inc-moved-2" not in seen("device-node-1", "incidents.list")
    assert seen("missing-device", "incidents.list") == set()
    assert "inc-ghost-1" in seen("site-s1", "incidents.list")
    # D10: the non-canonical identity is the site's and nobody else's.
    assert "oc-noncanon-1" in seen("site-s1", "outcomes.list")
    assert "oc-noncanon-1" not in seen("class-server", "outcomes.list")
    # JSONB subset and timestamptz lifecycle, through the real loader.
    assert seen("subset-incident-only", "fleet.list_all") == set()
    assert seen("subset-incident-only", "incidents.list") == {"child-node-1", "inc-node-1"}
    for control in ("expired", "revoked", "inert", "other-tenant", "blank-class",
                    "blank-device", "unknown-class", "ungranted"):
        for reader in M.READERS:
            assert result.seen[(control, reader)] == set(), (control, reader)


async def test_context_is_a_separate_read_on_postgres():
    stack = await _stack()
    cases = {
        "device-node-1": (set(), {"s1"}),
        "class-switch": (set(), {"s1", "s2"}),
        "site-s3+device-node-1": ({"s3"}, {"s1"}),
        "site-s2+class-switch": ({"s2"}, {"s1"}),
        "site-s1": ({"s1"}, set()),
        "tenant": ({"s1", "s2", "s3"}, set()),
        "missing-device": (set(), set()),
        "subset-incident-only": (set(), set()),   # fleet.view is not carried
    }
    for persona, expected in cases.items():
        principal = await M.persist_persona(stack.sessionmaker, stack.estate, persona)
        assert await M.context_sites(stack.sessionmaker, stack.estate, principal) == expected, persona


async def test_a_device_scoped_human_over_http_on_postgres():
    stack = await _stack()
    n, e = stack.n, stack.estate
    async with await stack.client("device-node-1") as c:
        fleet = await _get(c, "/api/fleet/?page_size=200")
        summary = await _get(c, "/api/fleet/summary")
        sites = await _get(c, "/api/sites/")
        audit = await _get(c, "/api/audit/?page_size=200")
        incidents = await _get(c, "/api/incidents/?status=all")
        child = await _get(c, f"/api/incidents/{n('child-node-1')}")
        hidden = [(await c.get(f"/api/incidents/{n(i)}")).status_code
                  for i in ("parent-1", "child-sw-1", "inc-moved-2", "inc-ghost-1")]
        site_mutation = await c.put(
            f"/api/sites/{e.sites['s1']}/org-unit", json={"org_unit_id": e.units["b1"]})
    assert [d["agent_id"] for d in fleet["devices"]] == [n("node-1")]
    assert fleet["total"] == 1, "the COUNT is scoped by the same condition"
    assert (summary["total_nodes"], summary["sites_count"]) == (1, 0)
    assert sites["sites"] == [{"id": e.sites["s1"], "site_name": n("s1"), "contextual": True}]
    assert (audit["entries"], audit["total"]) == ([], 0), "R3: no site audit through context"
    assert {i["incident_id"] for i in incidents["incidents"]} == {n("child-node-1"), n("inc-node-1")}
    assert child["parent_incident_id"] is None and child["correlation"] == {}
    assert "SITE-KNOWLEDGE" not in json.dumps(child["prior_learning"])
    assert hidden == [404, 404, 404, 404]
    assert site_mutation.status_code in (403, 404), site_mutation.text


async def test_the_approval_owner_rule_on_postgres():
    stack = await _stack()
    n = stack.n

    async def decide(persona, action):
        async with await stack.client(persona) as c:
            return (await c.post(f"/api/approvals/{n(action)}/approve", json={})).status_code

    # R6: the CURRENT class of the target, stored upper-case, matched by lower().
    assert await decide("class-switch", "act-swup-2") == 200
    assert await decide("class-switch", "act-node-2") == 403
    # R1: the class-less device belongs to no class.
    assert await decide("class-server", "act-blank-1") == 403
    # R9: an unresolved target is the site's to decide, not the device grant's.
    assert await decide("missing-device", "act-ghost-1") == 403
    assert await decide("site-s1", "act-ghost-1") == 200
    # D8: the decider reads its own record; a sibling device's holder does not.
    assert await decide("device-node-1", "act-node-1") == 200
    async with await stack.client("device-node-1") as c:
        assert (await _get(c, f"/api/approvals/{n('act-node-1')}/records"))["total"] == 1
    async with await stack.client("device-sw-1") as c:
        assert (await _get(c, f"/api/approvals/{n('act-node-1')}/records"))["total"] == 0


async def test_existing_personas_are_unchanged_on_postgres():
    """For tenant, org and site principals the device-aware predicates are
    the legacy site filter. Same rows, same counts, on PostgreSQL."""
    stack = await _stack()
    e = stack.estate
    by_site = {"s1": {"node-1", "sw-1", "blank-1"},
               "s2": {"node-2", "sw-2", "swup-2"}, "s3": {"node-3"}}
    cases = {"tenant": ("s1", "s2", "s3"), "org-region_a": ("s1", "s2"),
             "org-a1": ("s1", "s2"), "org-b1": ("s3",), "site-s1": ("s1",), "site-s3": ("s3",)}
    for persona, sites in cases.items():
        want = {e.name(k) for s in sites for k in by_site[s]}
        async with await stack.client(persona) as c:
            fleet = await _get(c, "/api/fleet/?page_size=200")
            listed = await _get(c, "/api/sites/")
        mine = {d["agent_id"] for d in fleet["devices"]} & {e.name(k) for k in sum(map(list, by_site.values()), [])}
        assert mine == want, persona
        held = [s for s in listed["sites"] if s["id"] in e.sites.values()]
        assert {s["site_name"] for s in held} == {e.name(s) for s in sites}, persona
        assert not any("contextual" in s for s in held), persona
