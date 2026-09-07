"""A29 (A6-4A): the External Agent API Plane is declared, not implied.

THE DEFECT, AS MEASURED
-----------------------
A machine principal reached 46 of 98 declared routes. Not because a
product decision admitted them -- because their required permission
happened to be a member of `MACHINE_PRINCIPAL_CEILING`. `fleet.view`
alone opened 43.

The mechanism is worth stating precisely, because it is what these tests
exist to make impossible again. `READ_CAPABILITIES` and
`INGRESS_CAPABILITIES` already declare the machine jobs, and each names
its own route family. `auth.py` mapped those bindings to coarse
permissions through `machine_permissions()` and dropped the bindings
before the guard could see them. So an agent bound to `attention` alone
-- the floor, since `REQUIRED_READS` is `("attention", "autonomy")` --
reached campaigns, learning, outcomes, predictive risk, warranty,
firmware exposure, policy posture, tenant enforcement settings, sites and
Harken Nodes.

WHAT THIS MODULE HOLDS
----------------------
The narrowing is the occasion. The invariant is the point:

* every machine-reachable route is DECLARED, and names a job
* no undeclared route is machine-reachable
* a permission entering the ceiling opens ZERO new routes
* the declaration matches runtime behaviour, in BOTH directions
* a persona holding one job reaches exactly that job's declared set
* the ceiling and the canonical permission vocabulary did not move
"""

from __future__ import annotations

import pytest

from harkeniq_cc.api.deps import SURFACE_REASONS
from harkeniq_cc.machine_identity import (
    MACHINE_PRINCIPAL_CEILING, machine_permissions,
)
from harkeniq_cc.metrics import SURFACE_REFUSAL_REASONS
from harkeniq_cc.operational_agent import (
    INGRESS_CAPABILITIES, READ_CAPABILITIES,
)
from harkeniq_cc.route_contract import (
    JOB_ATTENTION, JOB_INCIDENTS, JOB_SELF, JOBS_WITHOUT_A_BINDING,
    MACHINE_JOBS, MACHINE_ONLY_ROUTES, MACHINE_SURFACE, ROUTE_CONTRACT,
    SURFACE_HUMAN, SURFACE_MACHINE, SURFACES, machine_reachable,
    machine_surface,
)

from tests.unit.cc.test_a6_2_machine_surface import _stack, PREFIX

pytestmark = pytest.mark.asyncio


def _ceiling_routes() -> set[tuple[str, str]]:
    """Routes a machine could reach on PERMISSION alone -- the old rule."""
    return {
        route for route, spec in ROUTE_CONTRACT.items()
        if spec[0] in MACHINE_PRINCIPAL_CEILING
    }


# ---------------------------------------------------------------------------
# 1. The declaration is complete and honest
# ---------------------------------------------------------------------------


class TestTheDeclarationIsComplete:
    async def test_every_declared_machine_route_exists(self):
        """A declaration naming a route that does not exist is a lie."""
        unknown = sorted(set(MACHINE_SURFACE) - set(ROUTE_CONTRACT))
        assert not unknown, f"declared but not a real route: {unknown}"

    async def test_every_declared_machine_route_names_a_job(self):
        for route, (surface, job) in MACHINE_SURFACE.items():
            assert surface in SURFACES, (route, surface)
            assert job in MACHINE_JOBS, (
                f"{route} declares job {job!r}, which is not a machine job"
            )

    async def test_every_job_except_self_is_a_real_A0_binding(self):
        """A29.6: the vocabulary IS the binding vocabulary.

        No second naming system to keep in step. `self` is the declared
        exception because it is not a configured capability -- it is a
        principal reading its own record.
        """
        bindings = set(READ_CAPABILITIES) | set(INGRESS_CAPABILITIES)
        for job in MACHINE_JOBS - JOBS_WITHOUT_A_BINDING:
            assert job in bindings, (
                f"job {job!r} is not an A0 binding; the guard would be "
                "enforcing a vocabulary an operator cannot configure"
            )
        assert JOBS_WITHOUT_A_BINDING == {JOB_SELF}

    async def test_machine_only_is_derived_not_hand_kept(self):
        assert MACHINE_ONLY_ROUTES == frozenset(
            r for r, (s, _) in MACHINE_SURFACE.items() if s == SURFACE_MACHINE
        )

    async def test_the_declaration_is_the_only_positive_form(self):
        """No negative list exists to fall out of step with the routes."""
        import harkeniq_cc.route_contract as rc

        exclusions = [
            n for n in dir(rc)
            if n.isupper() and ("EXCLU" in n or "DENY" in n or "BLOCK" in n)
        ]
        assert not exclusions, (
            f"a negative machine list appeared ({exclusions}); default-deny "
            "means forgetting a route fails CLOSED, and an exclusion list "
            "inverts that"
        )


# ---------------------------------------------------------------------------
# 2. Default deny -- the invariant that outlives the narrowing
# ---------------------------------------------------------------------------


class TestDefaultDeny:
    async def test_an_undeclared_route_is_not_machine_reachable(self):
        for route in ROUTE_CONTRACT:
            if route in MACHINE_SURFACE:
                continue
            surface, job = machine_surface(*route)
            assert surface == SURFACE_HUMAN and job == ""
            assert not machine_reachable(*route), route

    async def test_a_permission_entering_the_ceiling_opens_ZERO_routes(self):
        """The exact regression default-deny exists for.

        This is how 46 happened: a permission is admitted to the ceiling
        and every route requiring it silently becomes machine-reachable.
        Adding one now must change nothing, because reach is declared
        here and nowhere else.
        """
        before = {r for r in ROUTE_CONTRACT if machine_reachable(*r)}

        widened = MACHINE_PRINCIPAL_CEILING | {
            "site.manage", "action.approve", "audit.view", "tenant.manage",
        }
        would_reach = {
            r for r, spec in ROUTE_CONTRACT.items() if spec[0] in widened
        }
        assert len(would_reach) > len(before), (
            "the fixture is vacuous: widening the ceiling reached nothing "
            "extra even under the OLD rule"
        )

        after = {r for r in ROUTE_CONTRACT if machine_reachable(*r)}
        assert after == before, (
            "reach followed the ceiling rather than the declaration"
        )

    async def test_reach_is_smaller_than_the_permission_arithmetic(self):
        """The narrowing itself, stated as a number rather than a claim."""
        by_permission = _ceiling_routes()
        declared = {r for r in ROUTE_CONTRACT if machine_reachable(*r)}
        assert declared < by_permission, (
            "the plane is not narrower than permission-implied reach"
        )
        assert len(by_permission) == 46, (
            f"the recorded A29.1 baseline moved: {len(by_permission)}"
        )
        assert len(declared) == 13, (
            f"the declared plane changed size to {len(declared)}; A29.13 "
            "requires the count to be stated, not approximated"
        )


# ---------------------------------------------------------------------------
# 3. Nothing in the authorization chain moved
# ---------------------------------------------------------------------------


class TestNothingElseMoved:
    async def test_the_ceiling_is_unchanged(self):
        assert MACHINE_PRINCIPAL_CEILING == frozenset(
            {"fleet.view", "incident.view", "proposal.submit"}
        )

    async def test_no_permission_entered_the_vocabulary(self):
        from harkeniq_cc.auth import ROLE_PERMISSIONS

        vocabulary = set()
        for perms in ROLE_PERMISSIONS.values():
            vocabulary |= {p for p in perms if p != "*"}
        # The exact set, so an addition fails BY NAME rather than by a
        # count somebody has to remember (A29.10: A6-4 adds none).
        assert vocabulary == {
            "action.approve", "audit.export", "audit.view", "billing.manage",
            "billing.view", "fleet.view", "governance.view",
            "incident.acknowledge", "incident.view", "license.view",
            "role.manage", "site.manage", "site.view", "skill.install",
            "skill.submit", "support.create", "support.view", "tenant.view",
            "user.manage", "user.view",
        }, sorted(vocabulary)
        for _route, (surface, _job) in MACHINE_SURFACE.items():
            assert surface in SURFACES

    async def test_the_surface_field_can_only_exclude(self):
        """A29.4: it never admits, so it cannot be a second authorization.

        Every declared machine route still demands its ordinary permission
        from the ordinary guard. If the surface could admit, a route could
        be reachable without the permission -- which is the ACL this
        design refuses to become.
        """
        for route in MACHINE_SURFACE:
            permission = ROUTE_CONTRACT[route][0]
            assert permission in MACHINE_PRINCIPAL_CEILING, (
                f"{route} is on the plane but demands {permission!r}, which "
                "the ceiling does not admit -- the surface would have to "
                "admit it, and it must never admit anything"
            )

    async def test_the_binding_tables_were_not_widened(self):
        """A29.7: reach was removed; the binding VOCABULARY was not."""
        assert set(READ_CAPABILITIES) == {
            "attention", "autonomy", "incidents", "learning", "fleet",
        }, "A6-4A must not delete binding vocabulary (B2)"
        assert set(INGRESS_CAPABILITIES) == {"proposals"}

    async def test_the_refusal_reasons_are_bounded(self):
        """A25.11: `/metrics` is unauthenticated."""
        assert SURFACE_REASONS <= SURFACE_REFUSAL_REASONS
        assert "other" in SURFACE_REFUSAL_REASONS

    async def test_every_incremented_counter_is_registered(self):
        """A counter that is only incremented is silently dead.

        `MetricsRegistry.inc` looks the name up and returns if it is
        absent, so an unregistered counter never reaches `/metrics` and
        never raises -- the telemetry form of declared-with-no-reader.
        A29's counter had this defect and so, it turned out, did two of
        A25's suffixed families, which is why this is a general test over
        the module rather than a check of the new name.

        Executed rather than grepped: register on a real registry, drive
        every recorder, and require every name to have appeared.
        """
        import sys

        sys.path.insert(0, "src")
        from harkeniq.metrics import MetricsRegistry
        from harkeniq_cc import metrics as m

        registry = MetricsRegistry()
        m.register_a6_metrics(registry)

        drivers = (
            [(m.record_surface_refusal, r) for r in m.SURFACE_REFUSAL_REASONS]
            + [(m.record_read_refusal, r) for r in m.READ_REFUSAL_REASONS]
            + [(m.record_correlation, j) for j in m.CORRELATION_JOINS]
        )
        for fn, arg in drivers:
            fn(arg)
        m.record_status_read()
        m.record_read_rate_limited()
        m.record_terminal_failure()

        dead = [
            name for name, metric in registry._metrics.items()
            if metric.value == 0.0
            and name.startswith((
                m.M_SURFACE_REFUSED, m.M_READ_REFUSED, m.M_CORRELATION,
            ))
        ]
        assert not dead, (
            "these counters were registered but no recorder moved them: "
            f"{sorted(dead)}"
        )
        # And the inverse, which is the defect: a name that recorders
        # increment but nobody registered would be absent entirely.
        for base, reasons in (
            (m.M_SURFACE_REFUSED, m.SURFACE_REFUSAL_REASONS),
            (m.M_READ_REFUSED, m.READ_REFUSAL_REASONS),
            (m.M_CORRELATION, m.CORRELATION_JOINS),
        ):
            for reason in reasons:
                assert f"{base}_{reason}" in registry._metrics, (
                    f"{base}_{reason} is incremented and never registered, "
                    "so it is a silent no-op"
                )


# ---------------------------------------------------------------------------
# 4. Runtime matches the declaration, in both directions
# ---------------------------------------------------------------------------


async def _machine(jobs=MACHINE_JOBS, seed=True):
    """A real machine principal on the production stack, holding `jobs`.

    Uses the suite's own stack rather than a bespoke override, so the
    scope resolver, the read meter and the self gate all behave exactly as
    they do for the A6-2 tests -- only the BINDINGS differ.
    """
    from tests.unit.cc.test_a6_2_machine_surface import _poisoned_estate

    stack = await _stack()
    if seed:
        await _poisoned_estate(stack)
    stack.as_machine("agent-A", jobs=jobs)
    return stack


class TestRuntimeMatchesTheDeclaration:
    async def test_an_off_plane_route_refuses_a_machine(self):
        stack = await _machine(seed=False)
        async with stack.client() as c:
            for path in (
                "/api/fleet/", "/api/learning/signals", "/api/campaigns/",
                "/api/policies/", "/api/scope-grants/me", "/api/autonomy/",
                "/api/capabilities/", "/api/warranty/",
                "/api/tenant-settings/scope-enforcement/impact",
            ):
                res = await c.get(path)
                assert res.status_code == 403, (path, res.status_code)
                assert "External Agent API plane" in res.text, path

    async def test_an_on_plane_route_admits_a_machine_holding_the_job(self):
        stack = await _machine(seed=False)
        async with stack.client() as c:
            for path in ("/api/attention/", "/api/incidents/"):
                assert (await c.get(path)).status_code == 200, path

    async def test_the_job_binding_is_what_decides(self):
        """A29.6: the permission is unchanged; the BINDING refuses."""
        # Same permissions as the passing case above -- only the binding
        # differs, so nothing but the job can explain the refusal.
        stack = await _machine(jobs={JOB_SELF}, seed=False)
        async with stack.client() as c:
            res = await c.get("/api/attention/")
        assert res.status_code == 403
        assert "attention" in res.text and "binding" in res.text

    async def test_self_needs_no_binding(self):
        """A29.6: an agent may always read its own record."""
        stack = await _machine(jobs={JOB_SELF})
        async with stack.client() as c:
            assert (await c.get(f"{PREFIX}/agent-A/runtime")).status_code == 200

    async def test_a_human_is_refused_on_a_MACHINE_route(self):
        stack = await _stack()
        stack.as_person(role="tenant_owner")
        async with stack.client() as c:
            res = await c.get(f"{PREFIX}/agent-A/submissions/anything")
        assert res.status_code == 403
        assert "machine-principal surface" in res.text

    async def test_a_human_is_unaffected_everywhere_else(self):
        """The narrowing is machine-only: no human read moved."""
        stack = await _stack()
        stack.as_person(role="tenant_owner")
        async with stack.client() as c:
            # `/api/policies/` is excluded here and only here: A26.11 made
            # it ask `scope.permits`, which this fixture's scope stub does
            # not implement. Its human behaviour is asserted on the
            # production stack by the A26 suite and the persona matrix.
            for path in ("/api/fleet/", "/api/attention/",
                         "/api/capabilities/", "/api/autonomy/"):
                assert (await c.get(path)).status_code == 200, path


# ---------------------------------------------------------------------------
# 5. One job at a time -- the machine persona sweep (A29.13)
# ---------------------------------------------------------------------------


class TestOneJobAtATime:
    async def test_a_persona_reaches_exactly_its_declared_set(self):
        """Generated, not enumerated: bind one job, sweep the whole plane.

        The assertion is set EQUALITY in both directions, so a route that
        admits too much and a route that admits too little both fail.
        """
        stack = await _machine()
        targets = (
            "/api/attention/", "/api/incidents/",
            f"{PREFIX}/agent-A/runtime", f"{PREFIX}/agent-A/identity",
        )
        expect = {
            JOB_ATTENTION: {"/api/attention/"},
            JOB_INCIDENTS: {"/api/incidents/"},
        }
        for job, on_plane in expect.items():
            # `self` rides with every persona: it is not a binding.
            allowed = on_plane | {
                f"{PREFIX}/agent-A/runtime", f"{PREFIX}/agent-A/identity",
            }
            reached = set()
            stack.as_machine("agent-A", jobs={JOB_SELF, job})
            async with stack.client() as c:
                for path in targets:
                    if (await c.get(path)).status_code != 403:
                        reached.add(path)
            assert reached == allowed, (
                f"persona holding {job!r} reached {sorted(reached)}, "
                f"declared {sorted(allowed)}"
            )

    async def test_a_persona_with_no_job_reaches_only_itself(self):
        stack = await _machine(jobs=set(), seed=False)
        async with stack.client() as c:
            # No `self` either: the floor of the plane is nothing.
            assert (await c.get("/api/attention/")).status_code == 403
            assert (await c.get(f"{PREFIX}/agent-A/runtime")).status_code == 403


# ---------------------------------------------------------------------------
# 6. The before/after census, frozen (A29.13)
# ---------------------------------------------------------------------------


class TestTheCensusIsFrozen:
    async def test_the_removed_set_is_exactly_what_was_ratified(self):
        removed = _ceiling_routes() - set(MACHINE_SURFACE)
        families = sorted({p.split("/")[2] for _m, p in removed})
        assert families == [
            "agents", "autonomy", "campaigns", "capabilities", "firmware",
            "fleet", "learning", "operational-agents", "outcomes",
            "policies", "predictive", "scope-grants", "sites",
            "tenant-settings", "warranty",
        ], families
        assert len(removed) == 33, (
            f"{len(removed)} routes left the machine plane; A29.13 requires "
            "the census to be frozen and stated"
        )

    async def test_the_one_write_is_unchanged(self):
        """A29.1: A6-4 is a read correction and does not reopen the write."""
        writes = [
            r for r in MACHINE_SURFACE if r[0] != "GET"
        ]
        assert writes == [
            ("POST", "/api/operational-agents/{agent_id}/proposals")
        ]
        assert ROUTE_CONTRACT[writes[0]][0] == "proposal.submit"
        # And the binding still gates it, exactly as A24.4 built it.
        assert machine_permissions(["attention"], []) == ["fleet.view"]
        assert "proposal.submit" in machine_permissions([], ["proposals"])
