"""A6-4B0b (spec A30.25): canonical reach convergence -- F1 closed.

F1: a `device` or `device_class` grant is real authority (`covers_device`
and `permits` say yes) and contributes nothing to `site_ids`, which every
device-bearing list filtered on. Such a principal read ZERO rows about the
devices the platform itself said it reaches.

What is proven here:

* **The invariant** -- for every device-bearing repository reader and every
  scope type, repository visibility equals the canonical permission-aware
  rule, computed independently of the SQL (`b0b_matrix`), with a negative
  control for each way it could go wrong.
* **The behaviour** -- the production stack, the production `get_scope`,
  persisted grants, STRICT: what a device-scoped and a class-scoped human
  now read, and what they still do not.
* **Correlation never widens reach** (R2/D7): children, parents,
  `correlation`, prior learning.
* **Context is not authority** (R10/D2): the contextual site projection is
  reduced and marked, never enters a scope, and is refused by every
  mutation -- swept, not reviewed.
* **Nobody else changed** -- tenant, org-unit and site personas read
  byte-identical responses, asserted DIFFERENTIALLY against the legacy
  site-only filter for every read route.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import pathlib
import re

import httpx
import pytest

import harkeniq_cc.api as api_pkg
from harkeniq_cc import operational_agent
from harkeniq_cc.api.deps import get_current_user
from harkeniq_cc.app import create_app
from harkeniq_cc.auth import ROLE_PERMISSIONS, UserContext, configure_auth
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db import repos as repos_module
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.models import CCFleetCache, CCScopeGrant
from harkeniq_cc.db.repos import (
    FleetCacheRepo, IncidentRepo, SiteRepo, TenantSettingsRepo, scope_sites,
)
from harkeniq_cc.governance import load_attention, load_scope
from harkeniq_cc.route_contract import (
    MACHINE_JOBS, MACHINE_SURFACE, READ_SCOPED, ROUTE_CONTRACT, UNSCOPED,
)
from harkeniq_cc.runtime import AppState
from harkeniq_cc.scope import (
    ENFORCEMENT_STRICT, SCOPE_ONLY_MARKER, ReadReach, ResolvedScope,
    read_reach, where_reach,
)
from harkeniq_cc.target_authority import FleetIndex

from tests.unit.cc import b0b_matrix as M

TENANT = "tenant-b0b"
TAG = "t"


def n(stem: str) -> str:
    return f"{stem}-{TAG}"


# ---------------------------------------------------------------------------
# The production stack over the matrix estate
# ---------------------------------------------------------------------------


class Stack:
    def __init__(self, app, sessionmaker, estate):
        self.app, self.sessionmaker, self.estate = app, sessionmaker, estate

    def client(self, principal: str, role: str = "tenant_owner"):
        async def _fake():
            return UserContext(
                user_id=principal, email=f"{principal}@example.com",
                tenant_id=TENANT, role=role,
                permissions=list(ROLE_PERMISSIONS[role]),
            )

        self.app.dependency_overrides[get_current_user] = _fake
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test",
        )

    async def persona(self, name: str, role: str = "tenant_owner"):
        principal = await M.persist_persona(self.sessionmaker, self.estate, name)
        return self.client(principal, role), principal


async def _stack() -> Stack:
    configure_auth("", "", "", insecure=True)
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sessionmaker = make_sessionmaker(engine)
    state = AppState(
        config=CCConfig(tenant_id=TENANT, insecure=True),
        engine=engine, sessionmaker=sessionmaker,
    )
    app = create_app(state)
    estate = await M.seed_estate(sessionmaker, TENANT, tag=TAG)
    async with sessionmaker() as session:
        await TenantSettingsRepo(session).set_enforcement(TENANT, ENFORCEMENT_STRICT, "test")
        await session.commit()
    return Stack(app, sessionmaker, estate)


async def _get(client, path):
    resp = await client.get(path)
    assert resp.status_code == 200, (path, resp.status_code, resp.text[:200])
    return resp.json()


# ---------------------------------------------------------------------------
# 1. The invariant
# ---------------------------------------------------------------------------


class TestCrossReaderEquivalence:
    """repository visibility == canonical permission-aware coverage."""

    async def test_every_persona_every_reader(self):
        stack = await _stack()
        result = await M.run_matrix(stack.sessionmaker, stack.estate)
        assert result.mismatches == [], "\n".join(result.mismatches)
        assert result.checked == len(M.PERSONAS) * len(M.READERS)

        seen = lambda persona, reader: {  # noqa: E731
            k.removesuffix(f"-{TAG}") for k in result.seen[(persona, reader)]
        }
        # NOT VACUOUS -- F1 is closed: device and class principals read.
        assert seen("device-node-1", "fleet.list_all") == {"node-1"}
        assert seen("device-node-1", "incidents.list") == {"child-node-1", "inc-node-1"}
        assert seen("device-node-1", "approvals.pending") == {"act-node-1"}
        assert seen("device-node-1", "approvals.history") == {"done-node-1"}
        assert seen("device-node-1", "outcomes.list") == {"oc-node-1"}
        assert seen("class-switch", "fleet.list_all") == {"sw-1", "sw-2", "swup-2"}
        assert seen("class-SWITCH", "fleet.list_all") == {"sw-1", "sw-2", "swup-2"}, (
            "class is matched case-insensitively on BOTH sides, as covers_device does")
        assert seen("class-server", "fleet.list_all") == {"node-1", "node-2", "node-3"}
        # ...and the union of two grants is the union, never more.
        assert seen("site-s3+device-node-1", "fleet.list_all") == {"node-1", "node-3"}
        assert seen("site-s2+class-switch", "fleet.list_all") == {"node-2", "sw-1", "sw-2", "swup-2"}

    async def test_each_negative_control_reads_nothing(self):
        stack = await _stack()
        controls = ["missing-device", "blank-class", "blank-device", "unknown-class",
                    "expired", "revoked", "inert", "other-tenant", "ungranted"]
        result = await M.run_matrix(stack.sessionmaker, stack.estate, controls)
        assert result.mismatches == []
        for (persona, reader), visible in result.seen.items():
            assert visible == set(), (persona, reader, sorted(visible))

    async def test_sibling_device_class_and_site_stay_hidden(self):
        stack = await _stack()
        result = await M.run_matrix(
            stack.sessionmaker, stack.estate,
            ["device-node-1", "class-switch", "site-s1", "org-b1"],
        )
        flat = lambda persona: {  # noqa: E731
            k for (p, _r), keys in result.seen.items() if p == persona for k in keys
        }
        device, switches = flat("device-node-1"), flat("class-switch")
        # sibling DEVICE at the same site, and the class-less one
        assert not any("sw-1" in k or "blank-1" in k for k in device), device
        # sibling CLASS, and R1: a blank class is in no class
        assert not any("node-" in k or "blank-1" in k for k in switches), switches
        # sibling SITE and inaccessible ORG
        assert not any(k.removesuffix(f"-{TAG}").endswith(("-2", "-3")) for k in flat("site-s1"))
        assert not any(k.removesuffix(f"-{TAG}").endswith(("-1", "-2")) for k in flat("org-b1"))

    async def test_a_device_that_does_not_resolve_owns_nothing(self):
        """R9, natural zero. `inc-ghost-1` names a device Central Command
        cannot identify; `inc-moved-2` names node-1 at a site where node-1
        is NOT. Neither is the device's. Both are still their SITE's."""
        stack = await _stack()
        result = await M.run_matrix(
            stack.sessionmaker, stack.estate,
            ["device-node-1", "missing-device", "site-s1", "org-a1"],
        )
        incidents = lambda p: result.seen[(p, "incidents.list")]  # noqa: E731
        assert n("inc-moved-2") not in incidents("device-node-1")
        assert n("inc-ghost-1") not in incidents("missing-device")
        assert n("inc-ghost-1") in incidents("site-s1")
        assert {n("inc-ghost-1"), n("inc-moved-2")} <= incidents("org-a1")
        # D10: a non-canonical outcome identity is read by site, by nobody else.
        assert n("oc-noncanon-1") in result.seen[("site-s1", "outcomes.list")]
        assert n("oc-noncanon-1") not in result.seen[("device-node-1", "outcomes.list")]

    async def test_a_subset_still_decides_which_reads_a_device_grant_opens(self):
        """S2 and B0b compose: the device dimension is permission-aware."""
        stack = await _stack()
        result = await M.run_matrix(
            stack.sessionmaker, stack.estate,
            ["subset-incident-only", "subset-fleet-only"],
        )
        seen = result.seen
        assert seen[("subset-incident-only", "incidents.list")] == {n("child-node-1"), n("inc-node-1")}
        for reader in ("fleet.list_all", "approvals.pending", "outcomes.list"):
            assert seen[("subset-incident-only", reader)] == set(), reader
        assert seen[("subset-fleet-only", "fleet.list_all")] == {n("sw-1"), n("sw-2"), n("swup-2")}
        assert seen[("subset-fleet-only", "incidents.list")] == set()


class TestReachMaterial:
    def test_a_blank_ref_is_never_reach(self):
        """R1: a blank is never a wildcard, and ``IN ('')`` would match
        every SITE-owned row -- the ones whose device column is blank."""
        from types import SimpleNamespace as NS
        from harkeniq_cc.scope import resolve

        def row(scope_type, ref):
            return NS(scope_type=scope_type, scope_ref=ref, permission_subset=None,
                      revoked_at=None, expires_at=None, role=None, realm="")

        scope = resolve(
            tenant_id=TENANT, principal_type="user", principal_ref="p",
            role_permissions=["fleet.view"], enforcement="strict",
            grant_rows=[row("device", ""), row("device_class", ""),
                        row("device_class", "   "), row("device_class", "Switch")],
        )
        reach = read_reach(scope, "fleet.view")
        assert reach.device_ids == frozenset()
        assert reach.device_classes == {"switch"}
        assert not reach.covers_device("", "", "")
        assert not reach.covers_device("dev", "site", "")


# ---------------------------------------------------------------------------
# 2. Behaviour: a device-scoped human
# ---------------------------------------------------------------------------


class TestDeviceScopedHuman:
    async def test_reads_its_device_and_nothing_beside_it(self):
        stack = await _stack()
        e = stack.estate
        client, _ = await stack.persona("device-node-1")
        async with client as c:
            fleet = await _get(c, "/api/fleet/")
            assert [d["agent_id"] for d in fleet["devices"]] == [n("node-1")]
            assert fleet["total"] == 1
            summary = await _get(c, "/api/fleet/summary")
            # `sites_count` is AUTHORITATIVE: a contextual site is not held.
            assert (summary["total_nodes"], summary["sites_count"]) == (1, 0)
            assert summary["incidents_open"] == 2

            assert (await c.get(f"/api/fleet/{e.fleet_row_ids['node-1']}")).status_code == 200
            for sibling in ("sw-1", "blank-1", "node-2"):
                assert (await c.get(f"/api/fleet/{e.fleet_row_ids[sibling]}")).status_code == 404
            assert (await c.get(f"/api/agents/{n('node-1')}")).status_code == 200
            assert (await c.get(f"/api/agents/{n('sw-1')}")).status_code == 404

            incidents = await _get(c, "/api/incidents/?status=all")
            assert {i["incident_id"] for i in incidents["incidents"]} == {
                n("child-node-1"), n("inc-node-1")}
            assert (await c.get(f"/api/incidents/{n('inc-node-1')}")).status_code == 200
            for hidden in ("child-sw-1", "parent-1", "inc-ghost-1", "inc-moved-2", "inc-node-3"):
                assert (await c.get(f"/api/incidents/{n(hidden)}")).status_code == 404, hidden

            # ONE queue, two requesters: the node's action and the agent's
            # proposal about the same device -- and nobody else's.
            queue = await _get(c, "/api/approvals/")
            assert {(a["origin"], a["device_agent_id"]) for a in queue["actions"]} == {
                ("node", n("node-1")), ("agent", n("node-1"))}
            assert (queue["node_total"], queue["agent_total"]) == (1, 1)
            history = await _get(c, "/api/approvals/history")
            assert [a["action_id"] for a in history["actions"]] == [n("done-node-1")]

            risk = await _get(c, "/api/predictive/risk")
            assert n("node-1") in json.dumps(risk) and n("sw-1") not in json.dumps(risk)
            metrics = await _get(c, "/api/outcomes/metrics")
            assert metrics["total_outcomes"] == 1

    async def test_the_control_with_no_grant_reads_nothing(self):
        """The reads above are not there because everybody gets them."""
        stack = await _stack()
        client, _ = await stack.persona("ungranted")
        async with client as c:
            assert (await _get(c, "/api/fleet/"))["total"] == 0
            assert (await _get(c, "/api/incidents/?status=all"))["incidents"] == []
            assert (await _get(c, "/api/sites/"))["sites"] == []


class TestDeviceClassScopedHuman:
    async def test_reads_its_class_everywhere_and_no_other_class(self):
        stack = await _stack()
        client, _ = await stack.persona("class-switch")
        async with client as c:
            fleet = await _get(c, "/api/fleet/?page_size=200")
            assert sorted(d["agent_id"] for d in fleet["devices"]) == [
                n("sw-1"), n("sw-2"), n("swup-2")]
            assert {d["device_class"].lower() for d in fleet["devices"]} == {"switch"}
            incidents = await _get(c, "/api/incidents/?status=all")
            assert {i["incident_id"] for i in incidents["incidents"]} == {
                n("child-sw-1"), n("inc-sw-2")}
            sites = (await _get(c, "/api/sites/"))["sites"]
            assert {s["site_name"] for s in sites} == {n("s1"), n("s2")}
            assert all(s == {"id": s["id"], "site_name": s["site_name"], "contextual": True}
                       for s in sites), sites
            me = await _get(c, "/api/scope-grants/me")
            assert me["site_ids"] == [] and me["device_classes"] == ["switch"]

    async def test_a_blank_class_device_is_in_no_class_but_is_read_by_site_and_id(self):
        stack = await _stack()
        for persona, expected in (
            ("class-server", False), ("class-switch", False),
            ("device-blank-1", True), ("site-s1", True),
        ):
            client, _ = await stack.persona(persona)
            async with client as c:
                fleet = await _get(c, "/api/fleet/?page_size=200")
            assert (n("blank-1") in [d["agent_id"] for d in fleet["devices"]]) is expected, persona


# ---------------------------------------------------------------------------
# 3. Correlation never widens reach (R2 / D7)
# ---------------------------------------------------------------------------


class TestIncidentsNeverWidenReach:
    async def test_a_child_read_through_its_device_names_no_parent_and_no_peers(self):
        stack = await _stack()
        client, _ = await stack.persona("class-switch")
        async with client as c:
            detail = await _get(c, f"/api/incidents/{n('child-sw-1')}")
            listed = {i["incident_id"]: i for i in
                      (await _get(c, "/api/incidents/?status=all"))["incidents"]}
        for view in (detail, listed[n("child-sw-1")]):
            assert view["parent_incident_id"] is None
            assert view["is_parent"] is True, "a hidden parent is not hinted at"
            # `votes` names the PEER devices. Not this caller's to read.
            assert view["correlation"] == {}
            assert n("parent-1") not in json.dumps(view)
            assert n("node-1") not in json.dumps(view)
        assert detail["children"] == []
        learned = json.dumps(detail["prior_learning"])
        assert n("COHORT-KNOWLEDGE") in learned
        assert "SITE-KNOWLEDGE" not in learned

    async def test_the_site_holder_reads_exactly_what_it_always_read(self):
        stack = await _stack()
        client, _ = await stack.persona("site-s1")
        async with client as c:
            child = await _get(c, f"/api/incidents/{n('child-sw-1')}")
            parent = await _get(c, f"/api/incidents/{n('parent-1')}")
        assert child["parent_incident_id"] == n("parent-1") and child["is_parent"] is False
        assert child["correlation"]["votes"] == {n("node-1"): "ALIVE"}
        assert n("SITE-KNOWLEDGE-s1") in json.dumps(child["prior_learning"])
        assert {k["incident_id"] for k in parent["children"]} == {
            n("child-node-1"), n("child-sw-1")}
        assert parent["correlation"]["devices"]

    async def test_reading_a_parent_does_not_carry_a_caller_to_a_child_elsewhere(self):
        """R2 is not only about device principals. `child-far-3` hangs from
        parent-1 and lives at ANOTHER site. The site-s1 holder reads the
        parent and must not be handed that child; the site-s3 holder reads
        the child and must not be told its parent. On main the children
        were unscoped, so the first half was a latent cross-site read."""
        stack = await _stack()
        expected = {
            "tenant": {n("child-node-1"), n("child-sw-1"), n("child-far-3")},
            "site-s1": {n("child-node-1"), n("child-sw-1")},
            "org-a1": {n("child-node-1"), n("child-sw-1")},
        }
        for persona, children in expected.items():
            client, _ = await stack.persona(persona)
            async with client as c:
                parent = await _get(c, f"/api/incidents/{n('parent-1')}")
            assert {k["incident_id"] for k in parent["children"]} == children, persona
            assert parent["child_count"] == len(children), persona
        client, _ = await stack.persona("site-s3")
        async with client as c:
            far = await _get(c, f"/api/incidents/{n('child-far-3')}")
            assert (await c.get(f"/api/incidents/{n('parent-1')}")).status_code == 404
        assert far["parent_incident_id"] is None and far["is_parent"] is True

    async def test_children_are_filtered_by_the_same_predicate_as_the_list(self):
        stack = await _stack()
        e = stack.estate
        principal = await M.persist_persona(stack.sessionmaker, e, "device-node-1")
        reach = await M.reach_for(stack.sessionmaker, e, principal, M.INCIDENT_VIEW)
        async with stack.sessionmaker() as session:
            repo = IncidentRepo(session)
            everyone = {c.incident_id for c in await repo.children_of(TENANT, n("parent-1"))}
            mine = {c.incident_id for c in await repo.children_of(TENANT, n("parent-1"), scope=reach)}
        assert everyone == {n("child-node-1"), n("child-sw-1"), n("child-far-3")}
        assert mine == {n("child-node-1")}

    async def test_an_off_page_parent_the_caller_MAY_read_is_still_named(self):
        """D7 redacts a HIDDEN parent, not an off-page one. With the open
        filter the resolved parent is not on the page; the site holder may
        read it on its own, so the orphan still names it."""
        stack = await _stack()
        async with stack.sessionmaker() as session:
            parent = await IncidentRepo(session).get(TENANT, n("parent-1"))
            parent.status = "resolved"
            await session.commit()
        for persona, expected in (("site-s1", n("parent-1")), ("device-node-1", None)):
            client, _ = await stack.persona(persona)
            async with client as c:
                rows = (await _get(c, "/api/incidents/?status=open"))["incidents"]
            child = next(i for i in rows if i["incident_id"] == n("child-node-1"))
            assert child["parent_incident_id"] == expected, persona

    async def test_attention_attaches_site_learning_only_to_a_site_holder(self):
        stack = await _stack()
        for persona, holds_site in (("device-node-1", False), ("site-s1", True)):
            client, _ = await stack.persona(persona)
            async with client as c:
                items = (await _get(c, "/api/attention/"))["items"]
            mine = next(i for i in items if i["agent_id"] == n("node-1"))
            learned = json.dumps(mine["evidence"]["learned_signals"])
            assert n("COHORT-KNOWLEDGE") in learned, persona
            assert ("SITE-KNOWLEDGE" in learned) is holds_site, persona
            # Context lends the NAME of the site the device is at.
            assert mine["site_name"] == n("s1"), persona
            if not holds_site:
                assert {i["agent_id"] for i in items} == {n("node-1")}


# ---------------------------------------------------------------------------
# 4. The approval owner rule (R6 / R9 / D8)
# ---------------------------------------------------------------------------


class TestApprovalOwnerRule:
    async def _decide(self, stack, persona, action, role="tenant_owner"):
        client, principal = await stack.persona(persona, role)
        async with client as c:
            resp = await c.post(f"/api/approvals/{n(action)}/approve", json={})
        return resp, principal

    async def test_a_class_grant_satisfies_the_scope_dimension_for_its_class_only(self):
        stack = await _stack()
        allowed, _ = await self._decide(stack, "class-switch", "act-sw-1")
        assert allowed.status_code == 200, allowed.text
        refused, _ = await self._decide(stack, "class-switch", "act-node-1")
        assert refused.status_code == 403, refused.text
        # R1: the blank-class device belongs to NO class.
        blank, _ = await self._decide(stack, "class-server", "act-blank-1")
        assert blank.status_code == 403, blank.text

    async def test_a_device_grant_reaches_its_own_device_and_no_sibling(self):
        stack = await _stack()
        own, _ = await self._decide(stack, "device-node-1", "act-node-1")
        assert own.status_code == 200, own.text
        sibling, _ = await self._decide(stack, "device-sw-1", "act-blank-1")
        assert sibling.status_code == 403, sibling.text

    async def test_every_other_gate_is_still_mandatory(self):
        """R6 creates no standalone authority: a class grant whose subset
        withholds `action.approve` covers the device and decides nothing."""
        stack = await _stack()
        resp, _ = await self._decide(stack, "subset-fleet-only", "act-sw-1")
        assert resp.status_code == 403, resp.text
        # ...and a role without the permission is stopped at the guard.
        viewer, _ = await self._decide(stack, "class-switch", "act-sw-2", role="viewer")
        assert viewer.status_code == 403

    async def test_a_target_that_does_not_resolve_is_the_sites_to_decide(self):
        """R9. A grant naming a device Central Command cannot identify
        decides nothing about it -- and the site administrator still can."""
        stack = await _stack()
        device, _ = await self._decide(stack, "missing-device", "act-ghost-1")
        assert device.status_code == 403, device.text
        site, _ = await self._decide(stack, "site-s1", "act-ghost-1")
        assert site.status_code == 200, site.text

    async def test_records_follow_the_owner_rule_and_nothing_broader(self):
        stack = await _stack()
        decided, approver = await self._decide(stack, "device-node-1", "act-node-1")
        assert decided.status_code == 200
        readers = {"device-node-1": 1, "site-s1": 1, "tenant": 1,
                   "device-sw-1": 0, "class-switch": 0, "site-s3": 0}
        for persona, expected in readers.items():
            client, _ = await stack.persona(persona)
            async with client as c:
                body = await _get(c, f"/api/approvals/{n('act-node-1')}/records")
            assert body["total"] == expected, persona
            if expected:
                assert body["records"][0]["approver_ref"] == approver


# ---------------------------------------------------------------------------
# 5. Context is not authority (R10 / D2)
# ---------------------------------------------------------------------------


class TestContextIsNotAuthority:
    async def test_the_projection_is_reduced_marked_and_authoritative_rows_are_unchanged(self):
        stack = await _stack()
        e = stack.estate
        client, principal = await stack.persona("site-s3+device-node-1")
        async with client as c:
            sites = {s["site_name"]: s for s in (await _get(c, "/api/sites/"))["sites"]}
            contextual_detail = await _get(c, f"/api/sites/{e.sites['s1']}")
            held_detail = await _get(c, f"/api/sites/{e.sites['s3']}")
            assert (await c.get(f"/api/sites/{e.sites['s2']}")).status_code == 404
            summary = await _get(c, "/api/fleet/summary")
        assert set(sites) == {n("s1"), n("s3")}
        assert sites[n("s1")] == {"id": e.sites["s1"], "site_name": n("s1"), "contextual": True}
        assert contextual_detail == sites[n("s1")], "no device count, no endpoint, no state"
        held = sites[n("s3")]
        assert "contextual" not in held, "an authoritative row gains no field"
        assert {"sm_endpoint", "status", "license_fingerprint", "org_unit_id"} <= set(held)
        assert held_detail["device_count"] == 1
        assert summary["sites_count"] == 1, "a contextual site is not a held site"

        authoritative, contextual = await M.context_sites(stack.sessionmaker, e, principal)
        assert (authoritative, contextual) == ({"s3"}, {"s1"})

    async def test_context_is_permission_aware(self):
        """It starts from the reach: a device grant that does not carry
        `fleet.view` lends no site to the `fleet.view`-guarded route."""
        stack = await _stack()
        client, _ = await stack.persona("subset-incident-only")
        async with client as c:
            assert (await _get(c, "/api/sites/"))["sites"] == []

    async def test_a_contextual_site_is_in_no_scope_and_covers_nothing(self):
        stack = await _stack()
        e = stack.estate
        principal = await M.persist_persona(stack.sessionmaker, e, "device-node-1")
        async with stack.sessionmaker() as session:
            scope = await load_scope(
                session, tenant_id=TENANT, principal_ref=principal,
                role_permissions=list(ROLE_PERMISSIONS["tenant_owner"]),
            )
        s1 = e.sites["s1"]
        assert scope.site_ids == frozenset() and not scope.covers_site(s1)
        for permission in ROLE_PERMISSIONS["tenant_owner"]:
            reach = read_reach(scope, permission)
            assert reach.site_ids == frozenset() and not reach.covers_site(s1), permission
            assert not scope.permits(permission, site_id=s1), permission
        assert not scope.permits("role.manage", tenant_object=True)

    def test_neither_scope_type_can_hold_a_contextual_site(self):
        """There is no field a contextual id could be written into."""
        for cls in (ResolvedScope, ReadReach):
            names = {f.name for f in dataclasses.fields(cls)}
            assert not {x for x in names if "context" in x and "site" in x}, (cls, names)
        assert "contextual_unit_ids" not in {f.name for f in dataclasses.fields(ReadReach)}

    def test_the_context_read_has_exactly_the_callers_it_should(self):
        """`list_context` feeds two projections and a name map. Pinned at
        source level: a new caller is a decision, not a convenience."""
        root = pathlib.Path(repos_module.__file__).parents[1]
        found: dict[str, set[str]] = {"list_context": set(), "names_in_view": set()}
        for path in root.rglob("*.py"):
            if "migrations" in path.parts:
                continue
            tree = ast.parse(path.read_text())
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(fn):
                    if isinstance(node, ast.Attribute) and node.attr in found \
                            and isinstance(node.ctx, ast.Load):
                        found[node.attr].add(f"{path.name}:{fn.name}")
        assert found["list_context"] == {
            "sites.py:list_sites", "sites.py:get_site",
            "repos.py:names_in_view", "governance.py:load_attention",
        }, found["list_context"]
        assert found["names_in_view"] == {
            "incidents.py:list_incidents", "incidents.py:get_incident",
        }, found["names_in_view"]


class TestNoMutationAcceptsAContextualSite:
    """The A23-1 mutation probe, aimed at CONTEXT.

    The principal's role holds every permission, so the route guard never
    refuses it. Its only grant is a DEVICE at site-3 -- a device none of
    the probed objects is about -- so site-3 is contextual for it and
    every probed target (the site itself, its campaign, its agent, its
    grant, an approval on a SIBLING device) is outside its authority.
    """

    async def _principal(self):
        from tests.unit.cc.test_a23_enforcement import _a23_stack
        from tests.unit.cc.test_e1_persona_matrix import _client, _grant

        app, sessionmaker, estate = await _a23_stack()
        async with sessionmaker() as session:
            session.add(CCFleetCache(
                site_id=estate.sites["site-3"], agent_id="ctx-device-3",
                agent_name="ctx-device-3", vendor="Dell", model="R750",
                health="ok", device_class="server",
            ))
            await session.commit()
        await _grant(sessionmaker, "kc-ctx", "device", "ctx-device-3", role="tenant_owner")
        return app, estate, _client(app, "tenant_owner", "kc-ctx")

    async def test_site_3_really_is_contextual_for_this_principal(self):
        app, estate, client = await self._principal()
        async with client as c:
            sites = (await _get(c, "/api/sites/"))["sites"]
            fleet = (await _get(c, "/api/fleet/"))["devices"]
        assert sites == [{"id": estate.sites["site-3"], "site_name": "site-3", "contextual": True}]
        assert [d["agent_id"] for d in fleet] == ["ctx-device-3"]

    async def test_no_mutation_reaches_the_contextual_site(self):
        from tests.unit.cc.test_a23_enforcement import (
            MUTATION_ROUTES, _path_for, _probe_body,
        )

        app, estate, client = await self._principal()
        reached, unproven = [], []
        async with client as c:
            for method, path in MUTATION_ROUTES:
                target = _path_for(path, estate, "site-3")
                body = _probe_body(method, path, estate)
                resp = await c.request(method, target, **({"json": body} if body is not None else {}))
                if path == "/api/approvals/batch" and resp.status_code == 200:
                    results = resp.json()["results"]
                    if any(r.get("ok") for r in results):
                        reached.append(f"{method} {path} -> processed an item")
                    continue
                if 200 <= resp.status_code < 300:
                    reached.append(f"{method} {path} -> {resp.status_code}")
                elif resp.status_code not in (403, 404):
                    unproven.append(f"{method} {path} -> {resp.status_code} {resp.text[:100]}")
        assert not reached, "context was accepted as authority:\n  " + "\n  ".join(reached)
        assert not unproven, "these probes never reached the scope gate:\n  " + "\n  ".join(unproven)
        assert len(MUTATION_ROUTES) > 30

    async def test_the_same_principal_may_still_act_on_its_OWN_device(self):
        """The sweep is not green because everything is refused."""
        from harkeniq_cc.db.repos import ApprovalRouteRepo

        app, estate, client = await self._principal()
        async with app.state.cc.sessionmaker() as session:
            await ApprovalRouteRepo(session).create(
                site_id=estate.sites["site-3"], action_id="act-ctx-device-3",
                action_type="SEL_CLEAR", device_agent_id="ctx-device-3",
            )
            await session.commit()
        async with client as c:
            own = await c.post("/api/approvals/act-ctx-device-3/approve", json={})
            sibling = await c.post("/api/approvals/act-site-3/approve", json={})
        assert own.status_code == 200, own.text
        assert sibling.status_code == 403, sibling.text


# ---------------------------------------------------------------------------
# 6. Site-owned domains stay site-owned (R3 / R4 / D4)
# ---------------------------------------------------------------------------


class TestSiteOwnedDomainsStaySiteOwned:
    @pytest.mark.parametrize("persona", ["device-node-1", "class-server"])
    async def test_device_and_class_principals_gain_no_site_domain(self, persona):
        stack = await _stack()
        client, _ = await stack.persona(persona)
        async with client as c:
            audit = await _get(c, "/api/audit/?page_size=200")
            candidates = await _get(c, "/api/learning/candidates")
            signals = await _get(c, "/api/learning/signals")
            autonomy = await _get(c, "/api/autonomy/")
            campaigns = await _get(c, "/api/campaigns/")
        # R3: the audit schema cannot prove a device owns an entry.
        assert (audit["entries"], audit["total"]) == ([], 0)
        # D4: `source_device` is provenance, not ownership.
        assert candidates["candidates"] == []
        assert [s for s in signals["signals"] if s["scope_type"] == "site"] == []
        # R4: no raw site governance facts as context -- and the estate
        # HAS them (safety state at every site), so this is not an empty
        # list being compared with an empty list.
        assert autonomy["scope"]["sites"] == []
        safety = autonomy["safety_state"]
        for key in ("sites_reporting", "sites_not_reporting", "suppressions",
                    "site_stop_switches", "error_budgets"):
            assert safety[key] == [], key
        for row in autonomy["action_classes"]:
            assert row["safety"]["error_budget"] is None, row["action_type"]
            assert row["safety"]["suppressed_domains"] == [], row["action_type"]
            assert row["safety"]["site_budget_remaining"] == {}, row["action_type"]
            assert [b for b in row["blocking_conditions"] if b.get("site_id")] == []
        text = json.dumps(autonomy)
        for site_id in stack.estate.sites.values():
            assert site_id not in text, "a site id reached a principal who holds no site"
        assert "PDU-" not in text, "a suppressed fault domain reached them"
        # ...while the posture they operate under is still there.
        assert autonomy["posture"]["ladder"] and autonomy["action_classes"]
        assert all(row["disposition"] for row in autonomy["action_classes"])
        assert campaigns["campaigns"] == []

    async def test_the_site_holder_still_reads_them(self):
        stack = await _stack()
        client, _ = await stack.persona("site-s1")
        async with client as c:
            audit = await _get(c, "/api/audit/?page_size=200")
            candidates = await _get(c, "/api/learning/candidates")
            autonomy = await _get(c, "/api/autonomy/")
        assert audit["total"] >= 1
        assert [k["skill_id"] for k in candidates["candidates"]] == [n("skill-s1")]
        assert [x["id"] for x in autonomy["scope"]["sites"]] == [stack.estate.sites["s1"]]
        sel_clear = next(r for r in autonomy["action_classes"] if r["action_type"] == "SEL_CLEAR")
        assert sel_clear["safety"]["error_budget"]["total"] == 30
        assert stack.estate.sites["s1"] in sel_clear["safety"]["site_budget_remaining"]


class TestAutonomyNarrowing:
    """R4 in the one pure function that narrows the governance contract."""

    def _contract(self):
        from datetime import datetime, timezone
        from types import SimpleNamespace as NS
        from harkeniq_cc.autonomy import build_autonomy

        now = datetime.now(timezone.utc)

        def safety(site_id, dropped):
            return NS(site_id=site_id, as_of=now, sm_stop_switch=dropped,
                      error_budgets=[{"action_type": "SEL_CLEAR", "total_count": 10,
                                      "success_count": 3, "failure_count": 7,
                                      "dropped_back": dropped}],
                      site_budgets={"SEL_CLEAR": 5},
                      suppressions=[{"domain": f"PDU-at-{site_id}", "reason": "x"}])

        return build_autonomy(
            tenant_id="t", actor_id="kc-x", actor_species="human",
            permissions=["fleet.view"], budgets=[], stop_switch=None, outcomes=[],
            safety_rows=[safety("site-MINE", False), safety("site-OTHER", True)],
            sites=[NS(id="site-MINE", site_name="mine"), NS(id="site-OTHER", site_name="other")],
            learned_signals=[], approval_policies=[], site_id=None,
            action_type=None, now=now,
        )

    @staticmethod
    def _legacy_narrow(contract, visible_site_ids):
        """`narrow_to_sites` exactly as it stood before A30.25, frozen here
        as the regression ORACLE for readers who hold a site."""
        visible = set(visible_site_ids)

        def ok(item):
            sid = item.get("site_id", "") if isinstance(item, dict) else ""
            return not sid or sid in visible

        out = dict(contract)
        scope = dict(out.get("scope") or {})
        scope["sites"] = [x for x in scope.get("sites", []) if x.get("id") in visible]
        out["scope"] = scope
        safety = dict(out.get("safety_state") or {})
        safety["sites_reporting"] = [x for x in safety.get("sites_reporting", []) if x in visible]
        safety["sites_not_reporting"] = [x for x in safety.get("sites_not_reporting", []) if x in visible]
        for key in ("suppressions", "site_stop_switches", "error_budgets"):
            safety[key] = [x for x in safety.get(key, []) if ok(x)]
        out["safety_state"] = safety
        classes = []
        for row in out.get("action_classes", []):
            row = dict(row)
            row["blocking_conditions"] = [b for b in row.get("blocking_conditions", []) if ok(b)]
            row["learning"] = [
                sig for sig in row.get("learning", [])
                if not (isinstance(sig, dict) and sig.get("scope_type") == "site"
                        and sig.get("scope_ref") not in visible)
            ]
            classes.append(row)
        out["action_classes"] = classes
        return out

    def test_a_reader_who_holds_no_site_receives_no_site_derived_fact(self):
        from harkeniq_cc.autonomy import narrow_to_sites

        narrowed = narrow_to_sites(self._contract(), set())
        text = json.dumps(narrowed)
        assert "site-MINE" not in text and "site-OTHER" not in text
        assert "PDU-at-" not in text
        assert narrowed["safety_state"]["error_budgets"] == []
        for row in narrowed["action_classes"]:
            assert row["safety"]["error_budget"] is None
            assert row["safety"]["suppressed_domains"] == []
            assert row["safety"]["site_budget_remaining"] == {}
        # The posture they operate under is untouched.
        full = self._contract()
        assert narrowed["posture"] == full["posture"]
        assert [(r["action_type"], r["disposition"]) for r in narrowed["action_classes"]] == \
            [(r["action_type"], r["disposition"]) for r in full["action_classes"]]

    @pytest.mark.parametrize("visible", [{"site-MINE"}, {"site-OTHER"}, {"site-MINE", "site-OTHER"}])
    def test_a_reader_who_holds_a_site_reads_byte_identically(self, visible):
        from harkeniq_cc.autonomy import narrow_to_sites

        contract = self._contract()
        assert json.dumps(narrow_to_sites(contract, visible), sort_keys=True, default=str) == \
            json.dumps(self._legacy_narrow(contract, visible), sort_keys=True, default=str)

    def test_a_tenant_wide_reader_gets_the_contract_untouched(self):
        from harkeniq_cc.autonomy import narrow_to_sites

        contract = self._contract()
        assert narrow_to_sites(contract, None) is contract

    def test_P2_is_recorded_here_not_fixed_here(self):
        """A KNOWN, OPEN finding, asserted so it cannot be forgotten.

        For a reader who DOES hold a site, the fields this function never
        looked at still describe EVERY site: the folded error-budget
        aggregate, and each class's `sites_dropped_back`,
        `suppressed_domains` and `site_budget_remaining`. It predates
        A6-4B0b (A23-1's narrowing), it was found by this slice's live
        gate, and it is NOT corrected here: A30.25's regression promise is
        that tenant, org-unit and site principals read byte-identically.
        It needs its own ratified narrowing slice. When that lands, this
        test must be inverted -- exactly as the F1 pin was.
        """
        from harkeniq_cc.autonomy import narrow_to_sites

        narrowed = narrow_to_sites(self._contract(), {"site-MINE"})
        sel_clear = next(r for r in narrowed["action_classes"] if r["action_type"] == "SEL_CLEAR")
        leaked = json.dumps(sel_clear["safety"])
        assert "site-OTHER" in leaked and "PDU-at-site-OTHER" in leaked, (
            "P2 has been closed -- invert this test and remove the STATED, NOT "
            "HIDDEN paragraph from narrow_to_sites"
        )


# ---------------------------------------------------------------------------
# 7. Nobody else changed: a differential against the legacy site filter
# ---------------------------------------------------------------------------

READ_ROUTES = sorted(
    path for (method, path), (_perm, treatment, _a) in ROUTE_CONTRACT.items()
    if method == "GET" and treatment in (READ_SCOPED, UNSCOPED)
)
_VOLATILE = {"generated_at", "as_of", "evaluated_at", "computed_at", "now"}


def _paths(estate: M.Estate) -> list[str]:
    def fill(path: str) -> str:
        agent = "unknown-agent" if path.startswith("/api/operational-agents") else estate.dev("node-1")
        return (path.replace("{agent_id}", agent)
                .replace("{device_id}", estate.fleet_row_ids["node-1"])
                .replace("{site_id}", estate.sites["s1"])
                .replace("{incident_id}", n("child-node-1"))
                .replace("{action_id}", n("act-node-1"))
                .replace("{unit_id}", estate.units["a1"])
                .replace("{campaign_id}", "unknown").replace("{policy_id}", "unknown")
                .replace("{group_id}", "unknown").replace("{budget_id}", "unknown")
                .replace("{member_id}", "unknown").replace("{grant_id}", "unknown")
                .replace("{submission_id}", "unknown").replace("{proposal_id}", "unknown")
                .replace("{transition}", "activate"))

    extra = [f"/api/incidents/{n(i)}" for i in ("parent-1", "child-sw-1", "inc-node-3", "inc-ghost-1")]
    extra += ["/api/incidents/?status=all", "/api/fleet/?page_size=200",
              f"/api/sites/{estate.sites['s3']}", f"/api/fleet/{estate.fleet_row_ids['node-3']}",
              f"/api/approvals/{n('act-sw-1')}/records"]
    return sorted({fill(p) for p in READ_ROUTES} | set(extra))


def _scrub(value):
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items() if k not in _VOLATILE}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


class TestExistingPersonasReadIdentically:
    """For a principal with no device and no class reach, every predicate
    IS the legacy `site_col IN (...)`. Proven two ways: the SQL is the
    same string, and every read route answers the same bytes with the new
    predicates replaced by the legacy one."""

    PERSONAS = ["tenant", "org-region_a", "org-a1", "org-b1", "site-s1", "site-s3"]

    @pytest.mark.parametrize("persona", PERSONAS)
    async def test_the_predicates_compile_to_the_legacy_filter(self, persona):
        from sqlalchemy.dialects import postgresql
        from harkeniq_cc.db.models import CCIncident

        stack = await _stack()
        principal = await M.persist_persona(stack.sessionmaker, stack.estate, persona)
        for permissions in (M.FLEET_VIEW, M.INCIDENT_VIEW, M.APPROVAL_READ):
            reach = await M.reach_for(stack.sessionmaker, stack.estate, principal, permissions)
            assert not reach.device_ids and not reach.device_classes

            def sql(clause):
                return None if clause is None else str(clause.compile(
                    dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))

            assert sql(repos_module.scope_fleet_devices(reach)) == \
                sql(scope_sites(CCFleetCache.site_id, reach))
            assert sql(repos_module.scope_device_owned(
                CCIncident.site_id, CCIncident.device_agent_id, reach)) == \
                sql(scope_sites(CCIncident.site_id, reach))

    @pytest.mark.parametrize("persona", PERSONAS)
    async def test_every_read_route_answers_the_same_bytes(self, persona, monkeypatch):
        stack = await _stack()
        _, principal = await stack.persona(persona)
        paths = _paths(stack.estate)

        async def sweep():
            out = {}
            async with stack.client(principal) as c:
                for path in paths:
                    resp = await c.get(path)
                    body = _scrub(resp.json()) if "json" in resp.headers.get("content-type", "") else resp.text
                    out[path] = (resp.status_code, json.dumps(body, sort_keys=True))
            return out

        now = await sweep()

        # The legacy model: a device-bearing row is its SITE's, full stop,
        # and there is no contextual site.
        monkeypatch.setattr(repos_module, "scope_fleet_devices",
                            lambda scope: scope_sites(CCFleetCache.site_id, scope))
        monkeypatch.setattr(repos_module, "scope_device_owned",
                            lambda site_col, _dev_col, scope: scope_sites(site_col, scope))

        async def _no_context(self, tenant_id, scope=None):
            return []

        monkeypatch.setattr(SiteRepo, "list_context", _no_context)
        legacy = await sweep()

        assert now.keys() == legacy.keys() and len(now) > 60
        different = [p for p in paths if now[p] != legacy[p]]
        assert different == [], different
        assert sum(1 for code, _ in now.values() if code == 200) > 30, "the sweep read nothing"

    async def test_the_differential_is_not_vacuous(self, monkeypatch):
        """The same swap DOES change a device principal's answers -- so
        equality above means something."""
        stack = await _stack()
        _, principal = await stack.persona("device-node-1")
        async with stack.client(principal) as c:
            now = (await _get(c, "/api/fleet/"))["total"]
        monkeypatch.setattr(repos_module, "scope_fleet_devices",
                            lambda scope: scope_sites(CCFleetCache.site_id, scope))
        async with stack.client(principal) as c:
            legacy = (await _get(c, "/api/fleet/"))["total"]
        assert (now, legacy) == (1, 0), "F1: the legacy filter read nothing"


# ---------------------------------------------------------------------------
# 8. The machine plane, and the agent's own operational reach
# ---------------------------------------------------------------------------


class TestMachineReach:
    async def _agent(self, stack, *rules):
        agent_id = "agent-b0b"
        async with stack.sessionmaker() as session:
            for scope_type, ref in rules:
                session.add(CCScopeGrant(
                    tenant_id=TENANT, principal_type="agent", principal_ref=agent_id,
                    scope_type=scope_type, scope_ref=ref, granted_by="seed",
                ))
            await session.commit()
        return agent_id

    def _machine_client(self, stack, agent_id):
        from harkeniq_cc.machine_identity import machine_permissions

        async def _fake():
            return UserContext(
                user_id=agent_id, email=f"op-agent:{agent_id}@v1", tenant_id=TENANT,
                role="", species="agent", identity_id="id-1", machine_jobs=MACHINE_JOBS,
                permissions=machine_permissions(
                    ["attention", "fleet", "incidents", "autonomy", "learning"], ["proposals"]),
            )

        stack.app.dependency_overrides[get_current_user] = _fake
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=stack.app), base_url="http://test")

    async def test_a_device_scoped_agent_reads_what_it_canonically_reaches(self):
        stack = await _stack()
        agent_id = await self._agent(stack, ("device", n("node-1")))
        async with self._machine_client(stack, agent_id) as c:
            attention = await _get(c, "/api/attention/")
            incidents = await _get(c, "/api/incidents/?status=all")
            # Off the machine plane since A6-4A, and still off it.
            for off_plane in ("/api/fleet/", "/api/sites/", "/api/autonomy/",
                              "/api/capabilities/", "/api/audit/"):
                assert (await c.get(off_plane)).status_code == 403, off_plane
        assert {i["agent_id"] for i in attention["items"]} == {n("node-1")}
        ids = {i["incident_id"] for i in incidents["incidents"]}
        assert ids == {n("child-node-1"), n("inc-node-1")}
        assert all(i["correlation"] == {} and i["parent_incident_id"] is None
                   for i in incidents["incidents"])

    def test_the_machine_surface_is_unchanged(self):
        assert len(MACHINE_SURFACE) == 13
        assert ("GET", "/api/fleet/") not in MACHINE_SURFACE
        assert ("GET", "/api/sites/") not in MACHINE_SURFACE

    async def test_an_agent_reads_its_own_proposal_about_its_own_device(self):
        """`_narrow_proposals` asked the site alone, so a device-scoped
        agent could not see its own proposals."""
        from harkeniq_cc.api.operational_agents import (
            _narrow_proposals, _proposal_reach,
        )
        from harkeniq_cc.db.models import CCAgentProposal
        from sqlalchemy import select

        stack = await _stack()
        agent_id = await self._agent(stack, ("device", n("node-1")))
        async with stack.sessionmaker() as session:
            scope = await load_scope(
                session, tenant_id=TENANT, principal_ref=agent_id,
                principal_type="agent", role_permissions=["fleet.view"], realm="",
            )
            proposals = (await session.execute(select(CCAgentProposal))).scalars().all()
            reach, index = await _proposal_reach(session, TENANT, scope)
            mine = _narrow_proposals(reach, index, proposals)
        assert [p.id for p in mine] == [stack.estate.proposal_ids["node-1"]]

    async def test_the_evaluators_attention_read_agrees_with_resolve_scope(self):
        """The CC-resident evaluator narrows attention by the agent's
        WHERE-only scope. That read and `resolve_scope` are two statements
        of one reach; R1 is what used to make them differ."""
        stack = await _stack()
        for rules in ([("device", n("node-1"))], [("device_class", "switch")],
                      [("device_class", "server")],
                      [("site", stack.estate.sites["s2"]), ("device", n("node-3"))]):
            agent_id = f"agent-{abs(hash(str(rules))) % 10**8}"
            async with stack.sessionmaker() as session:
                for scope_type, ref in rules:
                    session.add(CCScopeGrant(
                        tenant_id=TENANT, principal_type="agent", principal_ref=agent_id,
                        scope_type=scope_type, scope_ref=ref, granted_by="seed"))
                await session.commit()
            async with stack.sessionmaker() as session:
                scope = await load_scope(
                    session, tenant_id=TENANT, principal_ref=agent_id,
                    principal_type="agent", role_permissions=[SCOPE_ONLY_MARKER], realm="")
                devices = await FleetCacheRepo(session).list_all(TENANT)
                in_python = {d.agent_id for d in operational_agent.resolve_scope(
                    scope.effective_grants, devices, scope.site_ids)}
                attention = await load_attention(
                    session, tenant_id=TENANT, scope=where_reach(scope))
            assert {i["agent_id"] for i in attention["items"]} == in_python, rules
            assert n("blank-1") not in in_python or ("site", stack.estate.sites["s1"]) in rules


# ---------------------------------------------------------------------------
# 9. The durable structural guard
# ---------------------------------------------------------------------------

DEVICE_BEARING_REPOS = {
    "FleetCacheRepo", "IncidentRepo", "ApprovalRouteRepo",
    "AgentProposalRepo", "OutcomeHistoryRepo",
}
#: Site- and tenant-owned by decision (A30.25): a site filter is RIGHT here.
SITE_OWNED_REPOS = {"SiteRepo", "CandidateSkillRepo"}

#: Functions that judge a DEVICE-bearing object. None may fall back to the
#: site as the complete authority model.
DEVICE_BEARING_API = {
    ("incidents.py", "list_incidents"), ("incidents.py", "get_incident"),
    ("incidents.py", "_incident_dict"),
    ("approvals.py", "approval_records"), ("approvals.py", "_single_target_in_scope"),
    ("operational_agents.py", "_proposal_reach"),
    ("operational_agents.py", "_proposal_in_reach"),
    ("operational_agents.py", "_narrow_proposals"),
    ("operational_agents.py", "_authority_for_proposal"),
    ("fleet.py", "list_devices"), ("fleet.py", "get_device"),
    ("agents.py", "list_agents"), ("agents.py", "get_agent"),
}


class TestDeviceBearingReadersAskTheOwnerRule:
    def _repo_classes(self):
        tree = ast.parse(pathlib.Path(repos_module.__file__).read_text())
        return {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}

    def test_no_device_bearing_repository_filters_on_the_site_alone(self):
        classes = self._repo_classes()
        assert DEVICE_BEARING_REPOS <= set(classes)
        for name in sorted(DEVICE_BEARING_REPOS):
            calls = {getattr(n.func, "id", getattr(n.func, "attr", ""))
                     for n in ast.walk(classes[name]) if isinstance(n, ast.Call)}
            assert not calls & {"apply_scope", "scope_sites"}, (
                f"{name} filters a device-bearing table on site_ids alone (F1)")
            assert calls & {"scope_fleet_devices", "scope_device_owned", "_owned"}, name

    def test_every_scoped_method_of_those_repositories_uses_a_device_predicate(self):
        for name, cls in self._repo_classes().items():
            if name not in DEVICE_BEARING_REPOS:
                continue
            for fn in cls.body:
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if "scope" not in {a.arg for a in fn.args.args + fn.args.kwonlyargs}:
                    continue
                calls = {getattr(n.func, "id", getattr(n.func, "attr", ""))
                         for n in ast.walk(fn) if isinstance(n, ast.Call)}
                assert calls & {"scope_fleet_devices", "scope_device_owned", "_owned"}, (
                    f"{name}.{fn.name} takes a scope and applies no device-aware predicate")

    def test_the_guard_is_targeted_not_a_ban(self):
        """Site-owned domains keep their site filter, on purpose."""
        classes = self._repo_classes()
        for name in SITE_OWNED_REPOS:
            calls = {getattr(n.func, "id", "") for n in ast.walk(classes[name])
                     if isinstance(n, ast.Call)}
            assert "apply_scope" in calls, name

    def test_no_device_bearing_api_function_reads_site_ids(self):
        api_dir = pathlib.Path(api_pkg.__file__).parent
        present, offenders = set(), []
        for path in sorted(api_dir.glob("*.py")):
            for fn in ast.walk(ast.parse(path.read_text())):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if (path.name, fn.name) not in DEVICE_BEARING_API:
                    continue
                present.add((path.name, fn.name))
                for node in ast.walk(fn):
                    if isinstance(node, ast.Attribute) and node.attr == "site_ids":
                        offenders.append(f"{path.name}:{fn.name}:{node.lineno}")
        assert present == DEVICE_BEARING_API, DEVICE_BEARING_API - present
        assert offenders == [], offenders

    def test_no_authorization_path_infers_a_class(self):
        """R1. `or "server"` survives only where a class is DISPLAYED or
        ingested (D6), never where it is asked of a grant."""
        root = pathlib.Path(repos_module.__file__).parents[1]
        pattern = re.compile(r"covers_device\([^)]*or \"server\"|or \"server\"\)\.lower\(\) in")
        hits = [f"{p.name}: {m.group(0)}" for p in root.rglob("*.py")
                for m in pattern.finditer(p.read_text())]
        assert hits == [], hits
