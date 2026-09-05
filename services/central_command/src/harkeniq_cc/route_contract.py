"""The route contract: every `/api` route's permission and scope treatment.

A23 (spec A23.2, 2026-09-02). This table used to live in a test file.
That was the whole problem: a declaration only a test could see was a
promise nothing at runtime kept, and the sweep that derived from it
checked 403-versus-not-403 -- the permission guard -- while calling
itself a scope matrix. Several handlers injected the caller's scope and
never read it, and the table said READ_SCOPED about them.

Three things must now hold TOGETHER for every route, and the test suite
asserts all three from this one module:

1. **The declaration** below -- the only hand-written part.
2. **Runtime consumption** -- a route whose treatment needs a scope must
   have a handler that reads it. :func:`scope_consumption` inspects the
   handler's source, so a handler that accepts ``scope=Depends(get_scope)``
   and never touches the name fails the suite by name.
3. **Behaviour** -- the persona matrix asserts narrowing on reads and
   refusal on mutations against real out-of-scope objects.

A route with no scope decision is a route where authorization was not
considered; a new endpoint that does not appear here fails the suite.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from dataclasses import dataclass
from typing import Any, Callable

# Scope treatments. Exactly four, and every route is one of them.
READ_SCOPED = "read_scoped"      # 200, rows filtered to the caller's scope
OBJECT_GATED = "object_gated"    # 403 when the target is out of scope
TENANT_GATED = "tenant_gated"    # needs a tenant-scope grant
UNSCOPED = "unscoped"            # no scope dimension exists for this route

TREATMENTS = frozenset({READ_SCOPED, OBJECT_GATED, TENANT_GATED, UNSCOPED})

#: Treatments whose handler MUST consume the resolved scope.
SCOPE_CONSUMING = frozenset({READ_SCOPED, OBJECT_GATED, TENANT_GATED})

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
    # fleet that caller may see.
    ("GET", "/api/capabilities/"):              ("fleet.view", READ_SCOPED, False),
    # S6 campaigns. Reads are fleet.view and READ_SCOPED (an out-of-scope
    # campaign is 404, never 403); configuration is site.manage and
    # OBJECT_GATED on every scope rule the campaign names -- A23.3
    # extends that gate from creation and preflight to the whole
    # lifecycle, because a campaign is executed by advancing it and an
    # unscoped `advance` was an unscoped execution control.
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
    # A4 (spec A21): the condition -> capability catalogue. Reads are
    # fleet.view like the rest of the Registry surface; the write is
    # TENANT authority (site.manage + tenant object gate).
    ("GET", "/api/capabilities/catalogue"):
        ("fleet.view", READ_SCOPED, False),
    ("PUT", "/api/capabilities/catalogue"):
        ("site.manage", OBJECT_GATED, True),
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
    # A23: the listing is narrowed to grants the caller could have made
    # -- a cluster administrator sees the cluster's delegations, not the
    # tenant's whole authorization map.
    ("GET", "/api/scope-grants/"):              ("user.view", READ_SCOPED, False),
    ("GET", "/api/scope-grants/me"):            ("fleet.view", UNSCOPED, False),
    ("POST", "/api/scope-grants/"):             ("role.manage", OBJECT_GATED, True),
    ("DELETE", "/api/scope-grants/{grant_id}"): ("role.manage", OBJECT_GATED, True),
    # A23-3: atomic revoke + grant on a new target, gated on BOTH targets.
    ("POST", "/api/scope-grants/{grant_id}/reassign"): ("role.manage", OBJECT_GATED, True),
    ("GET", "/api/tenant-settings/scope-enforcement"): ("fleet.view", UNSCOPED, False),
    ("PUT", "/api/tenant-settings/scope-enforcement"): ("role.manage", TENANT_GATED, True),
    # A22.10: the report half of report-before-enforce. UNSCOPED because
    # the whole point is a tenant-wide census an admin acts on; it names
    # principals and grant counts, never device or site data.
    ("GET", "/api/tenant-settings/scope-enforcement/impact"): ("fleet.view", UNSCOPED, False),

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
    # A23: an agent is a scoped object. It is visible to a caller who
    # reaches at least one of its scope rules with fleet.view, and an
    # out-of-scope agent is 404, never 403. Its device and proposal rows
    # are narrowed to the caller. The catalogue's site list is the
    # caller's, not the tenant's.
    ("GET", "/api/operational-agents/"):            ("fleet.view", READ_SCOPED, False),
    ("GET", "/api/operational-agents/catalogue"):   ("fleet.view", READ_SCOPED, False),
    ("GET", "/api/operational-agents/{agent_id}"):  ("fleet.view", READ_SCOPED, False),
    ("GET", "/api/operational-agents/{agent_id}/proposals"): ("fleet.view", READ_SCOPED, False),
    # Object-gated on the agent's SCOPE, not tenant-gated: a tenant gate
    # would make the delegation ceiling unreachable (a tenant-wide
    # creator can delegate anything), so the ceiling IS the gate.
    ("POST", "/api/operational-agents/"):           ("site.manage", OBJECT_GATED, True),
    ("PATCH", "/api/operational-agents/{agent_id}"): ("site.manage", OBJECT_GATED, True),
    ("PUT", "/api/operational-agents/{agent_id}/bindings"): ("site.manage", OBJECT_GATED, True),
    ("POST", "/api/operational-agents/{agent_id}/preflight"):
        ("site.manage", OBJECT_GATED, True),
    ("POST", "/api/operational-agents/{agent_id}/acknowledge"):
        ("site.manage", OBJECT_GATED, True),
    ("GET", "/api/operational-agents/{agent_id}/preflight"):
        ("fleet.view", READ_SCOPED, False),
    ("GET", "/api/operational-agents/{agent_id}/runtime"):
        ("fleet.view", READ_SCOPED, False),
    # A5 (A22.7/A22.8): it writes NOTHING, so it is a GET and governed as
    # a read at `fleet.view`. "Its own and no other" is an object-level
    # gate inside the handler, not a permission.
    ("GET", "/api/operational-agents/{agent_id}/dry-run"):
        ("fleet.view", READ_SCOPED, False),
    # A3 (spec A20): the machine-identity lifecycle. No new permission.
    ("POST", "/api/operational-agents/{agent_id}/identity"):
        ("site.manage", OBJECT_GATED, True),
    ("POST", "/api/operational-agents/{agent_id}/identity/rotate"):
        ("site.manage", OBJECT_GATED, True),
    ("POST", "/api/operational-agents/{agent_id}/identity/revoke"):
        ("site.manage", OBJECT_GATED, True),
    ("GET", "/api/operational-agents/{agent_id}/identity"):
        ("fleet.view", READ_SCOPED, False),
    # A6-1 (A24): external governed submission. OBJECT_GATED like every
    # other mutation, and additionally self-restricted -- A24.5 requires
    # the token-derived agent to BE the route's agent, which the handler
    # asks before it loads anything.
    ("POST", "/api/operational-agents/{agent_id}/proposals"):
        ("proposal.submit", OBJECT_GATED, True),
    ("POST", "/api/operational-agents/{agent_id}/{transition}"):
        ("site.manage", OBJECT_GATED, True),

    # -- attention, autonomy, analytics ----------------------------
    ("GET", "/api/attention/"):                 ("fleet.view", READ_SCOPED, False),
    # A23: the disposition is tenant posture, but the contract NAMES the
    # sites it was composed over and the sites not reporting safety --
    # narrowed to the caller, so a scoped reader learns nothing about
    # sites outside their reach.
    ("GET", "/api/autonomy/"):                  ("fleet.view", READ_SCOPED, False),
    # A23: these three were declared UNSCOPED and emitted one row per
    # DEVICE to any fleet.view holder -- a fleet-inventory leak wearing a
    # risk, a CVE and a warranty label respectively.
    ("GET", "/api/predictive/risk"):            ("fleet.view", READ_SCOPED, False),
    ("GET", "/api/firmware/exposure"):          ("fleet.view", READ_SCOPED, False),
    ("GET", "/api/warranty/"):                  ("fleet.view", READ_SCOPED, False),
    # A23: patterns name the sites they were detected across, candidate
    # skills carry the site and device they were generated from, and a
    # site-scoped learned signal names its site. All three narrow.
    ("GET", "/api/outcomes/patterns"):          ("fleet.view", READ_SCOPED, False),
    ("GET", "/api/learning/candidates"):        ("fleet.view", READ_SCOPED, False),
    ("GET", "/api/learning/signals"):           ("fleet.view", READ_SCOPED, False),
    # Counts only; no site or device identifier exists on the row.
    ("GET", "/api/learning/cycles"):            ("fleet.view", UNSCOPED, False),
    # The CVE feed is a vendor advisory list with no fleet dimension.
    ("GET", "/api/firmware/cve-feed"):          ("fleet.view", UNSCOPED, False),
    ("POST", "/api/firmware/cve-feed"):         ("site.manage", TENANT_GATED, True),
    ("POST", "/api/warranty/import"):           ("site.manage", TENANT_GATED, True),

    # -- audit -----------------------------------------------------
    ("GET", "/api/audit/"):                     ("audit.view", READ_SCOPED, False),
    ("GET", "/api/audit/verify"):               ("audit.view", UNSCOPED, False),
}

#: Unauthenticated by design (E0.3): no tenant identifiers, same posture
#: as any load balancer probe.
PUBLIC = frozenset({("GET", "/healthz"), ("GET", "/metrics")})


# ---------------------------------------------------------------------------
# Runtime consumption census
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScopeConsumption:
    """What one handler does with the resolved scope."""

    declares: bool   # accepts `scope=Depends(get_scope)`
    consumes: bool   # reads the name `scope` anywhere in its body


def scope_consumption(handler: Callable[..., Any]) -> ScopeConsumption:
    """Does this handler declare a scope dependency, and does it read it?

    Source-level, on purpose. A dynamic check would need a request to
    drive every route to a point where scope is read; this one answers
    for the whole app at import time and names the handler that lies.
    "Consumes" means the name ``scope`` is LOADED somewhere in the body
    -- passed to a repository, a gate, a helper, or read directly. A
    handler that only ever has it as a parameter never consumed it.
    """
    try:
        source = textwrap.dedent(inspect.getsource(handler))
    except (OSError, TypeError):
        return ScopeConsumption(declares=False, consumes=False)
    tree = ast.parse(source)
    fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))),
        None,
    )
    if fn is None:
        return ScopeConsumption(declares=False, consumes=False)
    params = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
    declares = "scope" in params
    consumes = any(
        isinstance(n, ast.Name) and n.id == "scope" and isinstance(n.ctx, ast.Load)
        for node in fn.body
        for n in ast.walk(node)
    )
    return ScopeConsumption(declares=declares, consumes=consumes)


def route_handlers(app) -> dict[tuple[str, str], Callable[..., Any]]:
    """(method, path) -> endpoint callable, from the running app."""
    from fastapi.routing import APIRoute

    out: dict[tuple[str, str], Callable[..., Any]] = {}

    def walk(routes) -> None:
        for route in routes:
            if isinstance(route, APIRoute):
                for method in route.methods or ():
                    if method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                        out[(method, route.path)] = route.endpoint
                continue
            # FastAPI >= 0.140 includes routers lazily as `_IncludedRouter`,
            # which keeps the APIRouter it wraps on `original_router`.
            inner = getattr(route, "original_router", None)
            if inner is not None:
                walk(inner.routes)
            elif getattr(route, "routes", None):
                walk(route.routes)

    walk(app.routes)
    return out


def census(app) -> list[str]:
    """Every route whose declared treatment its handler cannot keep.

    Returns human-readable violations; an empty list is the contract
    holding. Used by the suite, and usable at boot for a self-check.
    """
    handlers = route_handlers(app)
    problems: list[str] = []
    for (method, path), (_perm, treatment, _aud) in ROUTE_CONTRACT.items():
        handler = handlers.get((method, path))
        if handler is None:
            continue  # the stale-route test reports it
        if treatment not in SCOPE_CONSUMING:
            continue
        found = scope_consumption(handler)
        if not found.declares:
            problems.append(
                f"{method} {path} is {treatment} but its handler "
                f"{handler.__name__!r} takes no scope dependency"
            )
        elif not found.consumes:
            problems.append(
                f"{method} {path} is {treatment} but its handler "
                f"{handler.__name__!r} accepts `scope` and never reads it"
            )
    return problems
