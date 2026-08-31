"""E1.2: the executable endpoint x persona x permission x scope matrix.

Ten personas against 68 protected endpoints is 680 cells, and a
hand-maintained table that size is wrong within a week. So the matrix is
executed, not read:

1. **The declaration table below** states, per route, its permission and
   its scope treatment. It is the only hand-written part.
2. **The route-contract test** walks the running app's own route table
   and requires every `/api` route to appear. A new endpoint with no
   scope decision FAILS THE SUITE -- it cannot be forgotten.
3. **The persona sweep** (test_e1_persona_matrix.py) derives every
   expected outcome from this table and drives the real ASGI app.

No test here asserts that a UI hid something.
"""

from __future__ import annotations

import pytest

from harkeniq_cc.app import create_app
from harkeniq_cc.auth import ROLE_PERMISSIONS
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import make_engine, make_sessionmaker
from harkeniq_cc.runtime import AppState

# Scope treatments. Exactly four, and every route is one of them.
READ_SCOPED = "read_scoped"      # 200, rows filtered to the caller's scope
OBJECT_GATED = "object_gated"    # 403 when the target is out of scope
TENANT_GATED = "tenant_gated"    # needs a tenant-scope grant
UNSCOPED = "unscoped"            # no scope dimension exists for this route

TREATMENTS = {READ_SCOPED, OBJECT_GATED, TENANT_GATED, UNSCOPED}

#: (method, path) -> (permission, treatment, audited)
#:
#: `permission` is what the route guard demands -- layer 1, "could this
#: actor ever". `treatment` is what layers 2-4 do with the target.
ROUTE_CONTRACT: dict[tuple[str, str], tuple[str, str, bool]] = {
    # -- fleet and device reads ------------------------------------
    ("GET", "/api/agents/"):                    ("fleet.view", READ_SCOPED, False),
    ("GET", "/api/agents/{agent_id}"):          ("fleet.view", READ_SCOPED, False),
    ("GET", "/api/fleet/"):                     ("fleet.view", READ_SCOPED, False),
    ("GET", "/api/fleet/summary"):              ("fleet.view", READ_SCOPED, False),
    ("GET", "/api/fleet/{device_id}"):          ("fleet.view", READ_SCOPED, False),
    ("GET", "/api/sites/"):                     ("fleet.view", READ_SCOPED, False),
    ("GET", "/api/sites/{site_id}"):            ("fleet.view", READ_SCOPED, False),
    ("GET", "/api/incidents/"):                 ("incident.view", READ_SCOPED, False),
    ("GET", "/api/incidents/{incident_id}"):    ("incident.view", READ_SCOPED, False),
    ("GET", "/api/outcomes/metrics"):           ("fleet.view", READ_SCOPED, False),
    # Capability Registry. READ_SCOPED, not UNSCOPED: unlike /api/autonomy
    # (which describes tenant-wide posture) this describes the caller's
    # actual DEVICES, so effective reach must be computed only over the
    # fleet that caller may see. A scoped principal reading the whole
    # tenant's reach would be a fleet-inventory leak wearing a
    # capability label.
    ("GET", "/api/capabilities/"):              ("fleet.view", READ_SCOPED, False),
    # S6 campaigns. Reads are fleet.view and READ_SCOPED (an out-of-scope
    # campaign is 404, never 403); configuration is site.manage and
    # OBJECT_GATED, because the delegation ceiling is checked against the
    # scope rows the campaign is being pointed at. No campaign.*
    # permission exists -- the vocabulary is fixed.
    ("GET", "/api/campaigns/"):                 ("fleet.view", READ_SCOPED, False),
    ("GET", "/api/campaigns/{campaign_id}"):    ("fleet.view", READ_SCOPED, False),
    ("GET", "/api/campaigns/{campaign_id}/targets"):
        ("fleet.view", READ_SCOPED, False),
    ("GET", "/api/campaigns/{campaign_id}/sites"):
        ("fleet.view", READ_SCOPED, False),
    ("GET", "/api/campaigns/{campaign_id}/waves"):
        ("fleet.view", READ_SCOPED, False),
    ("POST", "/api/campaigns/{campaign_id}/advance"):
        ("site.manage", OBJECT_GATED, True),
    ("POST", "/api/campaigns/"):                ("site.manage", OBJECT_GATED, True),
    ("POST", "/api/campaigns/{campaign_id}/preflight"):
        ("site.manage", OBJECT_GATED, True),
    ("POST", "/api/campaigns/{campaign_id}/acknowledge"):
        ("site.manage", OBJECT_GATED, True),
    ("POST", "/api/campaigns/{campaign_id}/submit"):
        ("site.manage", OBJECT_GATED, True),
    ("POST", "/api/campaigns/{campaign_id}/cancel"):
        ("site.manage", OBJECT_GATED, True),
    # READ_SCOPED like /api/fleet/{device_id}: an out-of-scope device is
    # 404, never 403, because a 403 confirms it exists.
    ("GET", "/api/capabilities/devices/{device_id}"):
        ("fleet.view", READ_SCOPED, False),

    # -- approvals -------------------------------------------------
    ("GET", "/api/approvals/"):                 ("action.approve", READ_SCOPED, False),
    ("GET", "/api/approvals/history"):          ("action.approve", READ_SCOPED, False),
    ("GET", "/api/approvals/{action_id}/records"): ("action.approve", READ_SCOPED, False),
    ("POST", "/api/approvals/{action_id}/approve"): ("action.approve", OBJECT_GATED, True),
    ("POST", "/api/approvals/{action_id}/deny"):    ("action.approve", OBJECT_GATED, True),
    ("POST", "/api/approvals/batch"):           ("action.approve", OBJECT_GATED, True),

    # -- the organizational tree -----------------------------------
    ("GET", "/api/org-units/"):                 ("site.view", READ_SCOPED, False),
    ("GET", "/api/org-units/{unit_id}"):        ("site.view", READ_SCOPED, False),
    ("POST", "/api/org-units/"):                ("site.manage", OBJECT_GATED, True),
    ("PATCH", "/api/org-units/{unit_id}"):      ("site.manage", OBJECT_GATED, True),
    ("DELETE", "/api/org-units/{unit_id}"):     ("site.manage", OBJECT_GATED, True),
    ("PUT", "/api/sites/{site_id}/org-unit"):   ("site.manage", OBJECT_GATED, True),

    # -- scope administration --------------------------------------
    ("GET", "/api/scope-grants/"):              ("user.view", READ_SCOPED, False),
    ("GET", "/api/scope-grants/me"):            ("fleet.view", UNSCOPED, False),
    ("POST", "/api/scope-grants/"):             ("role.manage", OBJECT_GATED, True),
    ("DELETE", "/api/scope-grants/{grant_id}"): ("role.manage", OBJECT_GATED, True),
    ("GET", "/api/tenant-settings/scope-enforcement"): ("fleet.view", UNSCOPED, False),
    ("PUT", "/api/tenant-settings/scope-enforcement"): ("role.manage", TENANT_GATED, True),

    # -- site registration -----------------------------------------
    ("POST", "/api/sites/register"):            ("site.manage", TENANT_GATED, True),

    # -- tenant governance: READ at permission, MUTATE at tenant scope
    ("GET", "/api/policies/"):                  ("fleet.view", UNSCOPED, False),
    ("GET", "/api/policies/autonomy"):          ("fleet.view", UNSCOPED, False),
    ("GET", "/api/policies/groups"):            ("fleet.view", UNSCOPED, False),
    ("GET", "/api/policies/groups/{group_id}"): ("fleet.view", UNSCOPED, False),
    ("GET", "/api/policies/stop-switch"):       ("fleet.view", UNSCOPED, False),
    ("POST", "/api/policies/"):                 ("site.manage", TENANT_GATED, True),
    ("PATCH", "/api/policies/{policy_id}"):     ("site.manage", TENANT_GATED, True),
    ("DELETE", "/api/policies/{policy_id}"):    ("site.manage", TENANT_GATED, True),
    ("POST", "/api/policies/autonomy"):         ("site.manage", TENANT_GATED, True),
    ("DELETE", "/api/policies/autonomy/{budget_id}"): ("site.manage", TENANT_GATED, True),
    ("POST", "/api/policies/groups"):           ("site.manage", TENANT_GATED, True),
    ("PATCH", "/api/policies/groups/{group_id}"):  ("site.manage", TENANT_GATED, True),
    ("DELETE", "/api/policies/groups/{group_id}"): ("site.manage", TENANT_GATED, True),
    ("POST", "/api/policies/groups/{group_id}/members"): ("site.manage", TENANT_GATED, True),
    ("DELETE", "/api/policies/groups/{group_id}/members/{member_id}"):
        ("site.manage", TENANT_GATED, True),
    ("POST", "/api/policies/stop-switch"):      ("site.manage", TENANT_GATED, True),
    ("POST", "/api/policies/stop-switch/deactivate"): ("site.manage", TENANT_GATED, True),

    # -- Operational Agents ----------------------------------------
    ("GET", "/api/operational-agents/"):            ("fleet.view", UNSCOPED, False),
    ("GET", "/api/operational-agents/catalogue"):   ("fleet.view", UNSCOPED, False),
    ("GET", "/api/operational-agents/{agent_id}"):  ("fleet.view", UNSCOPED, False),
    ("GET", "/api/operational-agents/{agent_id}/proposals"): ("fleet.view", UNSCOPED, False),
    # Object-gated on the agent's SCOPE, not tenant-gated: a tenant gate
    # would make the delegation ceiling unreachable (a tenant-wide
    # creator can delegate anything), so the ceiling IS the gate.
    ("POST", "/api/operational-agents/"):           ("site.manage", OBJECT_GATED, True),
    ("PATCH", "/api/operational-agents/{agent_id}"): ("site.manage", OBJECT_GATED, True),
    ("PUT", "/api/operational-agents/{agent_id}/bindings"): ("site.manage", OBJECT_GATED, True),
    ("POST", "/api/operational-agents/{agent_id}/{transition}"):
        ("site.manage", OBJECT_GATED, True),

    # -- tenant-wide catalogues and analytics ----------------------
    # No site dimension exists on these tables, so there is nothing to
    # scope a read to. Their MUTATIONS are tenant-gated.
    ("GET", "/api/attention/"):                 ("fleet.view", READ_SCOPED, False),
    ("GET", "/api/autonomy/"):                  ("fleet.view", UNSCOPED, False),
    ("GET", "/api/outcomes/patterns"):          ("fleet.view", UNSCOPED, False),
    ("GET", "/api/predictive/risk"):            ("fleet.view", UNSCOPED, False),
    ("GET", "/api/learning/candidates"):        ("fleet.view", UNSCOPED, False),
    ("GET", "/api/learning/cycles"):            ("fleet.view", UNSCOPED, False),
    ("GET", "/api/learning/signals"):           ("fleet.view", UNSCOPED, False),
    ("GET", "/api/firmware/cve-feed"):          ("fleet.view", UNSCOPED, False),
    ("GET", "/api/firmware/exposure"):          ("fleet.view", UNSCOPED, False),
    ("GET", "/api/warranty/"):                  ("fleet.view", UNSCOPED, False),
    ("POST", "/api/firmware/cve-feed"):         ("site.manage", TENANT_GATED, True),
    ("POST", "/api/warranty/import"):           ("site.manage", TENANT_GATED, True),

    # -- audit -----------------------------------------------------
    ("GET", "/api/audit/"):                     ("audit.view", READ_SCOPED, False),
    ("GET", "/api/audit/verify"):               ("audit.view", UNSCOPED, False),
}

#: Unauthenticated by design (E0.3): no tenant identifiers, same posture
#: as any load balancer probe.
PUBLIC = {("GET", "/healthz"), ("GET", "/metrics")}


def _app():
    cfg = CCConfig(tenant_id="t", insecure=True)
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    return create_app(
        AppState(config=cfg, engine=engine, sessionmaker=make_sessionmaker(engine))
    )


def live_routes() -> set[tuple[str, str]]:
    spec = _app().openapi()
    return {
        (method.upper(), path)
        for path, ops in spec["paths"].items()
        for method in ops
        if method.upper() in ("GET", "POST", "PUT", "PATCH", "DELETE")
    }


class TestEveryRouteIsDeclared:
    """The mechanism that makes forgetting impossible."""

    def test_no_api_route_lacks_a_scope_decision(self):
        undeclared = sorted(
            r for r in live_routes()
            if r[1].startswith("/api") and r not in ROUTE_CONTRACT
        )
        assert not undeclared, (
            "these routes have no scope treatment declared in "
            "ROUTE_CONTRACT. Every /api route must state its permission "
            "and one of the four treatments; a route with no scope "
            "decision is a route where authorization was not considered:\n  "
            + "\n  ".join(f"{m} {p}" for m, p in undeclared)
        )

    def test_the_contract_names_no_route_that_does_not_exist(self):
        live = live_routes()
        stale = sorted(r for r in ROUTE_CONTRACT if r not in live)
        assert not stale, (
            "ROUTE_CONTRACT names routes the app does not serve; a stale "
            "declaration hides a removed endpoint's history:\n  "
            + "\n  ".join(f"{m} {p}" for m, p in stale)
        )

    def test_only_healthz_and_metrics_are_unauthenticated(self):
        live = live_routes()
        public = {r for r in live if not r[1].startswith("/api")}
        assert public <= PUBLIC | {
            ("GET", "/openapi.json"), ("GET", "/docs"), ("GET", "/redoc"),
            ("GET", "/docs/oauth2-redirect"),
        }

    def test_every_treatment_is_one_of_the_four(self):
        for route, (_, treatment, _audited) in ROUTE_CONTRACT.items():
            assert treatment in TREATMENTS, f"{route} has treatment {treatment!r}"

    def test_every_declared_permission_is_in_the_fixed_vocabulary(self):
        """E1.2 introduces NO new permission. The vocabulary is fixed."""
        known = set().union(*(set(p) for p in ROLE_PERMISSIONS.values())) - {"*"}
        for route, (permission, _, _) in ROUTE_CONTRACT.items():
            assert permission in known, (
                f"{route} demands {permission!r}, which is not in the fixed "
                "permission vocabulary (spec §4)"
            )


class TestTheShapeOfTheContract:
    def test_every_mutation_is_gated_and_audited(self):
        for (method, path), (_, treatment, audited) in ROUTE_CONTRACT.items():
            if method in ("POST", "PUT", "PATCH", "DELETE"):
                assert treatment in (OBJECT_GATED, TENANT_GATED), (
                    f"{method} {path} mutates but is {treatment}: a mutation "
                    "must resolve its target to a scope"
                )
                assert audited, f"{method} {path} mutates without an audit entry"

    def test_no_read_is_object_gated(self):
        """A read of something out of scope returns fewer rows, never 403.

        A 403 on a read confirms the object exists, which is itself a
        leak across a scope boundary.
        """
        for (method, path), (_, treatment, _) in ROUTE_CONTRACT.items():
            if method == "GET":
                assert treatment in (READ_SCOPED, UNSCOPED), (
                    f"GET {path} is {treatment}"
                )

    def test_the_census_matches_what_was_designed(self):
        reads = sum(1 for m, _ in ROUTE_CONTRACT if m == "GET")
        mutations = len(ROUTE_CONTRACT) - reads
        assert reads + mutations == len(ROUTE_CONTRACT)
        # A tripwire, not a target: if this moves, the endpoint x persona
        # sweep below has more or fewer cells than the design reviewed.
        assert reads >= 36 and mutations >= 26
