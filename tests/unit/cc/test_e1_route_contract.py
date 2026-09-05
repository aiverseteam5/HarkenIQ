"""E1.2 / A23: the executable endpoint x persona x permission x scope matrix.

Ten personas against 68 protected endpoints is 680 cells, and a
hand-maintained table that size is wrong within a week. So the matrix is
executed, not read:

1. **The declaration table** (`harkeniq_cc.route_contract.ROUTE_CONTRACT`)
   states, per route, its permission and its scope treatment. It is the
   only hand-written part, and since A23 it is RUNTIME code: a
   declaration only a test could see was a promise nothing kept.
2. **The route-contract test** walks the running app's own route table
   and requires every `/api` route to appear. A new endpoint with no
   scope decision FAILS THE SUITE -- it cannot be forgotten.
3. **The consumption census** (A23.2) inspects every scope-consuming
   route's handler and fails the suite, by name, on one that accepts
   `scope` and never reads it. Strict mode cannot help a handler that
   never consumes the scope.
4. **The persona sweep** (test_e1_persona_matrix.py) derives every
   expected outcome from this table and drives the real ASGI app,
   asserting narrowing on reads and refusal on mutations.

No test here asserts that a UI hid something.
"""

from __future__ import annotations

from harkeniq_cc.app import create_app
from harkeniq_cc.auth import ROLE_PERMISSIONS
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import make_engine, make_sessionmaker
from harkeniq_cc.route_contract import (  # noqa: F401 -- re-exported for the matrix
    OBJECT_GATED,
    PUBLIC,
    READ_SCOPED,
    ROUTE_CONTRACT,
    SCOPE_CONSUMING,
    TENANT_GATED,
    TREATMENTS,
    UNSCOPED,
    census,
    scope_consumption,
)
from harkeniq_cc.runtime import AppState


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
        """The vocabulary is fixed, and changing it is an amendment.

        E1.2 introduced no permission. A24 introduces exactly one --
        `proposal.submit` -- and deliberately gives it to NO human role:
        `ROLE_PERMISSIONS` mirrors the Console's and a test pins that
        parity, so adding a machine permission to a role would break
        parity to no purpose. A person has no token-derived agent and is
        refused by A24.5 regardless.

        So the vocabulary is role permissions UNION the machine ceiling,
        which is what the platform's vocabulary has actually been since
        A3. The invariant is unchanged: no route may demand a permission
        outside it.
        """
        from harkeniq_cc.machine_identity import MACHINE_PRINCIPAL_CEILING

        known = (
            set().union(*(set(p) for p in ROLE_PERMISSIONS.values()))
            | set(MACHINE_PRINCIPAL_CEILING)
        ) - {"*"}
        for route, (permission, _, _) in ROUTE_CONTRACT.items():
            assert permission in known, (
                f"{route} demands {permission!r}, which is not in the fixed "
                "permission vocabulary (spec §4)"
            )


class TestMachineOnlyRoutesAreChecked:
    """A25: an exclusion that only a test could see would be a hole.

    `MACHINE_ONLY_ROUTES` lets the persona sweep stop asking "holds the
    permission, therefore not 403" about a route no human may use. That
    is a legitimate exemption and a dangerous shape, so the set is
    checked in BOTH directions: every listed route must genuinely refuse
    a human, and nothing may be listed that is not declared.
    """

    def test_every_machine_only_route_is_in_the_contract(self):
        from harkeniq_cc.route_contract import (
            MACHINE_ONLY_ROUTES, ROUTE_CONTRACT,
        )

        for route in MACHINE_ONLY_ROUTES:
            assert route in ROUTE_CONTRACT, (
                f"{route} is exempted from the persona sweep but is not "
                "declared in the route contract at all"
            )

    def test_the_set_is_small_and_deliberate(self):
        """A growing exemption list is how a matrix stops meaning anything."""
        from harkeniq_cc.route_contract import MACHINE_ONLY_ROUTES

        assert len(MACHINE_ONLY_ROUTES) <= 4, (
            "the machine-only exemption is growing; each entry removes a "
            "route from the human persona matrix and needs a reason"
        )

    def test_every_machine_only_route_actually_refuses_a_human(self):
        """Source-level: the handler must reach the machine gate.

        Behaviour is asserted by the persona sweep against real personas;
        this catches the case where a route is LISTED here but its
        handler never refuses anyone -- which would silently exempt it
        from the matrix while admitting everybody.
        """
        import inspect

        from harkeniq_cc.api import operational_agents as oa
        from harkeniq_cc.route_contract import MACHINE_ONLY_ROUTES

        handlers = {
            "/api/operational-agents/{agent_id}/submissions/{submission_id}":
                oa.get_submission_receipt,
            "/api/operational-agents/{agent_id}/proposals/{proposal_id}":
                oa.get_proposal_receipt,
        }
        for _method, path in MACHINE_ONLY_ROUTES:
            handler = handlers.get(path)
            assert handler is not None, f"no handler mapped for {path}"
            assert "_machine_read_gate" in inspect.getsource(handler), (
                f"{path} is declared machine-only but never asks the gate"
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


class TestDeclaredScopeIsConsumed:
    """A23.2: declaration + runtime consumption + behaviour, together.

    The first half of failure class B. A handler that accepts
    ``scope=Depends(get_scope)`` and never reads the name has declared a
    treatment it cannot keep, and the persona sweep would have to know
    every such handler's shape to catch it. This census catches it by
    name, from the source, for every scope-consuming route at once.
    """

    def test_every_scope_consuming_route_reads_the_scope(self):
        problems = census(_app())
        assert not problems, (
            "these routes declare a scope treatment their handler does "
            "not keep -- the scope is resolved and then ignored, so strict "
            "mode cannot narrow or refuse anything here:\n  "
            + "\n  ".join(problems)
        )

    def test_the_census_detects_the_lie_it_exists_for(self):
        """The detector must fail on the exact shape A23 found."""

        async def liar(scope=None):  # declared, never read
            return {"ok": True}

        async def honest(scope=None):
            return {"sites": sorted(scope.site_ids)}

        assert scope_consumption(liar).declares
        assert not scope_consumption(liar).consumes
        assert scope_consumption(honest).consumes

    def test_scope_consuming_is_exactly_the_three_scoped_treatments(self):
        assert SCOPE_CONSUMING == {READ_SCOPED, OBJECT_GATED, TENANT_GATED}
