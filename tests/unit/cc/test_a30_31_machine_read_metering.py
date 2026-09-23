"""A30.31 (A6-4B0c): every machine read is metered, exactly once, from ONE path.

THE DEFECT, AS MEASURED ON UNMODIFIED MAIN
------------------------------------------
Twelve machine reads are declared. Nine charged one read each; Attention,
the incident list and the incident detail charged NOTHING -- served, not
found and malformed alike (F3). And a malformed read was free on every
machine read route, the nine included: FastAPI resolves dependencies,
validates query parameters, and only then calls the handler, so a 422
never reached a charge that lived on the handler's first line.

WHAT THIS MODULE HOLDS
----------------------
* The meter is DECLARED: every machine job has exactly one meter, a
  route's meter is its job's, and the declaration agrees with the A0
  binding vocabulary.
* The census reads the RUNNING app, anchored on `MACHINE_SURFACE` and
  `ROUTE_CONTRACT` -- never a router prefix -- and fails by name under
  every mutation it exists for.
* ONE path: `_charge_machine_read` <- `meter_machine_read` <- the guard,
  and nothing else, anywhere in Central Command.
* The matrix, generated from `MACHINE_SURFACE`: every declared read,
  served, refused and malformed, costs exactly ONE read -- on a double-
  declared guard, across helpers, and across a minute boundary.
* The write is never read-charged; a person is never charged or
  throttled; the plane did not move.
* The window: within budget served, then 429, then a new window serves;
  one allowance across every job.
* No resource detail reaches the meter's record or `/metrics`.
"""

from __future__ import annotations

import ast
import inspect
import pathlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from fastapi import Depends, HTTPException

import harkeniq_cc
from harkeniq_cc import ingress_limits, metrics as m
from harkeniq_cc.api.deps import (
    ROUTE_SURFACE_GUARD, get_current_user, is_route_surface_guard,
    require_any_permission, require_permission,
)
from harkeniq_cc.db.models import (
    CCAgentIngressAttempt, CCAgentReadWindow, CCFleetCache, CCIncident, CCSite,
)
from harkeniq_cc.ingress_limits import READ_MAX_PER_WINDOW, read_window_start
from harkeniq_cc.machine_identity import MACHINE_PRINCIPAL_CEILING
from harkeniq_cc.operational_agent import (
    INGRESS_CAPABILITIES, READ_CAPABILITIES,
)
from harkeniq_cc.route_contract import (
    JOB_ATTENTION, JOB_INCIDENTS, JOB_METER, JOB_PROPOSALS, JOB_SELF,
    JOBS_WITHOUT_A_BINDING, MACHINE_JOBS, MACHINE_ONLY_ROUTES,
    MACHINE_SURFACE, METER_ATTEMPT, METER_READ, METERS, ROUTE_CONTRACT,
    SURFACE_BOTH, api_routes, dependency_calls, machine_meter, meter_census,
)

from tests.unit.cc.test_a6_external_ingress import (
    TENANT, _agent, _body, _first_ref, _ready, _stack,
)

# `asyncio_mode = "auto"` (pyproject) drives the async tests; a module
# mark would only warn on the structural ones, which are sync.

#: Everything the ceiling admits -- these suites test what the METER does
#: with a request, so the principal is given every permission it could
#: hold; the A29 suites narrow permissions and jobs to test the gates.
EVERYTHING = ("proposal.submit", "fleet.view", "incident.view")

SUBMIT = ("POST", "/api/operational-agents/{agent_id}/proposals")

#: Derived from the declaration, never listed: a new machine read is in
#: this matrix the moment it is declared.
READ_ROUTES = sorted(
    route for route, (_surface, job) in MACHINE_SURFACE.items()
    if JOB_METER.get(job) == METER_READ
)

#: The machine reads that take a `limit` query parameter, i.e. the ones a
#: caller can malform. Asserted against the running app below, so it can
#: neither miss one nor keep one that is gone.
LIMIT_BEARING = sorted([
    ("GET", "/api/attention/"),
    ("GET", "/api/incidents/"),
    ("GET", "/api/operational-agents/{agent_id}/proposals"),
])

#: A value that must never reach accounting, planted in every request
#: shape a caller controls.
PLANTED = "SECRET-B0C-PLANTED"


# ---------------------------------------------------------------------------
# The estate
# ---------------------------------------------------------------------------


@dataclass
class Estate:
    agent_id: str
    other_id: str
    site_id: str
    submission_id: str
    proposal_id: str
    incident_id: str
    hidden_incident_id: str


async def _estate(stack) -> Estate:
    """A live agent with a real governed submission, a second agent, and
    an incident at a site the agent does not reach."""
    agent_id, site_id = await _ready(stack)
    ref = await _first_ref(stack, agent_id)
    async with stack.as_machine(agent_id, permissions=EVERYTHING).client() as c:
        res = await c.post(
            f"/api/operational-agents/{agent_id}/proposals",
            json=_body(ref, key="b0c-estate-0001"),
        )
    assert res.status_code == 201, res.text
    body = res.json()
    stack.as_person()
    async with stack.client() as c:
        other_id = await _agent(c, site_id, name="Another Agent")
    hidden = f"inc-{PLANTED}"
    async with stack.sessionmaker() as session:
        far = CCSite(tenant_id=TENANT, site_name="DC-FAR",
                     sm_endpoint="sm:50052", sm_token="tok2")
        session.add(far)
        await session.flush()
        session.add(CCFleetCache(
            site_id=far.id, agent_id="node-far", agent_name="far",
            vendor="Dell", model="R750", device_class="server",
            observation="observed", health="Critical",
        ))
        session.add(CCIncident(
            incident_id=hidden, tenant_id=TENANT, site_id=far.id,
            kind="device", status="open", title="hidden", subsystem="fan",
            device_agent_id="node-far", confidence=0.9,
        ))
        await session.commit()
    return Estate(
        agent_id=agent_id, other_id=other_id, site_id=site_id,
        submission_id=body["submission_id"], proposal_id=body["proposal_id"],
        incident_id="inc-1", hidden_incident_id=hidden,
    )


def _url(path: str, e: Estate, *, agent: str | None = None) -> str:
    """Fill a declared template. A KeyError means a new machine route has a
    path parameter this matrix has not been taught -- which is the point:
    it cannot be declared without deciding what it costs here."""
    return path.format(
        agent_id=agent or e.agent_id, proposal_id=e.proposal_id,
        submission_id=e.submission_id, incident_id=e.incident_id,
    )


async def _reads(stack, agent_id: str, tenant: str = TENANT) -> int:
    async with stack.sessionmaker() as session:
        return int((await session.execute(
            sa.select(sa.func.coalesce(sa.func.sum(CCAgentReadWindow.reads), 0))
            .where(CCAgentReadWindow.tenant_id == tenant,
                   CCAgentReadWindow.agent_id == agent_id)
        )).scalar_one())


async def _windows(stack) -> list:
    async with stack.sessionmaker() as session:
        return list((await session.execute(
            sa.select(CCAgentReadWindow))).scalars().all())


async def _cost(stack, agent_id, url, *, jobs=None, permissions=EVERYTHING):
    """(status, reads charged) for ONE machine request."""
    before = await _reads(stack, agent_id)
    async with stack.as_machine(
        agent_id, permissions=permissions, jobs=jobs,
    ).client() as c:
        res = await c.get(url)
    return res.status_code, await _reads(stack, agent_id) - before


def _metric(stack, name: str) -> float:
    metric = stack.state.metrics._metrics.get(name)
    return 0.0 if metric is None else metric.value


# ---------------------------------------------------------------------------
# 1. The meter is declared
# ---------------------------------------------------------------------------


class TestTheMeterIsDeclared:
    def test_every_machine_job_has_exactly_one_meter(self):
        assert set(JOB_METER) == set(MACHINE_JOBS), (
            sorted(set(JOB_METER) ^ set(MACHINE_JOBS))
        )
        assert set(JOB_METER.values()) <= METERS

    def test_the_declaration_is_the_A0_distinction(self):
        """Reads are read-metered, the ingress binding is attempt-metered."""
        for job, meter in JOB_METER.items():
            if meter == METER_READ:
                assert job in set(READ_CAPABILITIES) | JOBS_WITHOUT_A_BINDING, job
            else:
                assert job in INGRESS_CAPABILITIES, job
        assert JOB_METER[JOB_SELF] == METER_READ
        assert JOB_METER[JOB_PROPOSALS] == METER_ATTEMPT

    def test_a_routes_meter_is_its_jobs(self):
        for route, (_surface, job) in MACHINE_SURFACE.items():
            assert machine_meter(*route) == JOB_METER[job], route
        for route in ROUTE_CONTRACT:
            if route not in MACHINE_SURFACE:
                assert machine_meter(*route) == "", route

    def test_the_inventory(self):
        """The Phase-1 inventory, frozen: 12 reads, 1 write, 85 human."""
        assert READ_ROUTES == sorted([
            ("GET", "/api/attention/"),
            ("GET", "/api/incidents/"),
            ("GET", "/api/incidents/{incident_id}"),
            ("GET", "/api/operational-agents/{agent_id}"),
            ("GET", "/api/operational-agents/{agent_id}/dry-run"),
            ("GET", "/api/operational-agents/{agent_id}/identity"),
            ("GET", "/api/operational-agents/{agent_id}/ingress"),
            ("GET", "/api/operational-agents/{agent_id}/preflight"),
            ("GET", "/api/operational-agents/{agent_id}/proposals"),
            ("GET", "/api/operational-agents/{agent_id}/proposals/{proposal_id}"),
            ("GET", "/api/operational-agents/{agent_id}/runtime"),
            ("GET", "/api/operational-agents/{agent_id}/submissions/{submission_id}"),
        ])
        writes = [r for r, (_s, j) in MACHINE_SURFACE.items()
                  if JOB_METER[j] == METER_ATTEMPT]
        assert writes == [SUBMIT]
        human = [r for r in ROUTE_CONTRACT if not machine_meter(*r)]
        assert len(human) == 85, len(human)

    def test_the_three_F3_routes_are_read_metered(self):
        """Outside the Operational Agent router, where the old guard was blind."""
        for route in (("GET", "/api/attention/"), ("GET", "/api/incidents/"),
                      ("GET", "/api/incidents/{incident_id}")):
            assert machine_meter(*route) == METER_READ, route
            assert not route[1].startswith("/api/operational-agents")


# ---------------------------------------------------------------------------
# 2. The census reads the running app
# ---------------------------------------------------------------------------


class _Swap:
    """Replace one route's dependency list, and ALWAYS put it back.

    Routers are module singletons: the `APIRoute` a test app holds is the
    same object every later test's app holds, so a mutation that leaked
    would silently change the suite.
    """

    def __init__(self, route, dependencies):
        self.route, self.dependencies = route, dependencies

    def __enter__(self):
        self.saved = self.route.dependant.dependencies
        self.route.dependant.dependencies = self.dependencies
        return self

    def __exit__(self, *exc):
        self.route.dependant.dependencies = self.saved
        return False


class TestTheCensus:
    async def test_it_is_clean_on_the_real_app(self):
        stack = await _stack()
        assert meter_census(stack.app) == []

    async def test_every_declared_read_crosses_a_marked_guard(self):
        stack = await _stack()
        routes = api_routes(stack.app)
        for route in READ_ROUTES:
            calls = dependency_calls(routes[route])
            assert any(is_route_surface_guard(c) for c in calls), route
        write = dependency_calls(routes[SUBMIT])
        assert not any(is_route_surface_guard(c) for c in write)

    async def test_attention_losing_its_guard_is_named(self):
        """The F3 route, from the router the old guard never looked at."""
        stack = await _stack()
        route = api_routes(stack.app)[("GET", "/api/attention/")]
        kept = [d for d in route.dependant.dependencies
                if not is_route_surface_guard(d.call)]
        with _Swap(route, kept):
            problems = meter_census(stack.app)
        assert problems and all("/api/attention/" in p for p in problems), problems
        assert any("served unmetered" in p for p in problems), problems
        assert meter_census(stack.app) == [], "the mutation leaked"

    async def test_the_write_carrying_the_read_guard_is_named(self):
        stack = await _stack()
        route = api_routes(stack.app)[SUBMIT]
        from fastapi.dependencies.utils import get_parameterless_sub_dependant

        guard = get_parameterless_sub_dependant(
            depends=Depends(require_permission("proposal.submit")),
            path=route.path_format,
        )
        with _Swap(route, [guard, *route.dependant.dependencies]):
            problems = meter_census(stack.app)
        assert any("attempt-metered but carries the read-plane guard" in p
                   for p in problems), problems

    async def test_a_job_with_no_meter_is_named(self):
        stack = await _stack()
        saved = JOB_METER.pop(JOB_INCIDENTS)
        try:
            problems = meter_census(stack.app)
        finally:
            JOB_METER[JOB_INCIDENTS] = saved
        assert any("'incidents' has no decided meter answer" in p
                   for p in problems), problems
        assert meter_census(stack.app) == []

    async def test_a_human_route_that_crosses_no_guard_is_named(self):
        """Default deny, checked against the running app: an unguarded route
        would be machine-reachable in fact, unrefused and unmetered."""
        stack = await _stack()
        route = api_routes(stack.app)[("GET", "/api/fleet/")]
        kept = [d for d in route.dependant.dependencies
                if not is_route_surface_guard(d.call)]
        with _Swap(route, kept):
            problems = meter_census(stack.app)
        assert any("GET /api/fleet/ is not on the machine plane and crosses "
                   "no route guard" in p for p in problems), problems

    async def test_a_new_machine_read_declared_without_a_guard_is_named(self):
        """The regression the census exists for, end to end."""
        stack = await _stack()
        probe = ("GET", "/api/b0c-probe/")

        @stack.app.get("/api/b0c-probe/")
        async def b0c_probe(user=Depends(get_current_user)):  # noqa: B008
            return {}

        MACHINE_SURFACE[probe] = (SURFACE_BOTH, JOB_ATTENTION)
        ROUTE_CONTRACT[probe] = ("fleet.view", "unscoped", False)
        try:
            problems = meter_census(stack.app)
        finally:
            MACHINE_SURFACE.pop(probe)
            ROUTE_CONTRACT.pop(probe)
        assert any("GET /api/b0c-probe/ is read-metered but no guard" in p
                   for p in problems), problems

    async def test_a_handler_that_meters_itself_is_named(self):
        stack = await _stack()
        probe = ("GET", "/api/b0c-probe-self/")

        @stack.app.get(
            "/api/b0c-probe-self/",
            dependencies=[Depends(require_permission("fleet.view"))],
        )
        async def b0c_probe_self(request=None):
            from harkeniq_cc.read_meter import meter_machine_read

            await meter_machine_read(request, None, job="attention")
            return {}

        MACHINE_SURFACE[probe] = (SURFACE_BOTH, JOB_ATTENTION)
        ROUTE_CONTRACT[probe] = ("fleet.view", "unscoped", False)
        try:
            problems = meter_census(stack.app)
        finally:
            MACHINE_SURFACE.pop(probe)
            ROUTE_CONTRACT.pop(probe)
        assert any("second metering path" in p for p in problems), problems


# ---------------------------------------------------------------------------
# 3. ONE path
# ---------------------------------------------------------------------------


def _callers(name: str) -> set[tuple[str, str]]:
    """(module, enclosing function) for every call to `name` in Central Command."""
    root = pathlib.Path(harkeniq_cc.__file__).parent
    found: set[tuple[str, str]] = set()

    class Visitor(ast.NodeVisitor):
        def __init__(self, module):
            self.module, self.stack = module, []

        def _fn(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_FunctionDef = visit_AsyncFunctionDef = _fn

        def visit_Call(self, node):
            called = getattr(node.func, "id", getattr(node.func, "attr", ""))
            if called == name:
                found.add((self.module, self.stack[-1] if self.stack else ""))
            self.generic_visit(node)

    for path in sorted(root.rglob("*.py")):
        module = ".".join(path.relative_to(root.parent).with_suffix("").parts)
        Visitor(module).visit(ast.parse(path.read_text()))
    return found


class TestOnePath:
    def test_the_charge_has_one_caller(self):
        assert _callers("_charge_machine_read") == {
            ("harkeniq_cc.read_meter", "meter_machine_read"),
        }

    def test_the_entry_point_has_one_caller_the_guard(self):
        assert _callers("meter_machine_read") == {
            ("harkeniq_cc.api.deps", "enforce_route_surface"),
        }

    def test_the_window_is_admitted_in_one_place(self):
        assert _callers("admit_read") == {
            ("harkeniq_cc.read_meter", "_charge_machine_read"),
        }

    def test_refusal_evidence_is_written_by_the_guard_only(self):
        assert _callers("record_surface_refusal_window") == {
            ("harkeniq_cc.api.deps", "enforce_route_surface"),
        }

    def test_no_api_module_meters_itself(self):
        for name in ("_charge_machine_read", "meter_machine_read", "admit_read"):
            offenders = {c for c in _callers(name)
                         if c[0].startswith("harkeniq_cc.api.")
                         and c[0] != "harkeniq_cc.api.deps"}
            assert not offenders, (name, offenders)

    def test_only_the_two_permission_guards_carry_the_mark(self):
        root = pathlib.Path(harkeniq_cc.__file__).parent
        marked: set[tuple[str, str]] = set()
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text())
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(fn):
                    if (isinstance(node, ast.Call)
                            and getattr(node.func, "id", "") == "setattr"
                            and len(node.args) >= 2
                            and ast.unparse(node.args[1]) == "ROUTE_SURFACE_GUARD"):
                        marked.add((path.name, fn.name))
        assert marked == {
            ("deps.py", "require_permission"),
            ("deps.py", "require_any_permission"),
        }, marked

    def test_every_marked_guard_awaits_the_surface_guard_first(self):
        for guard in (require_permission("fleet.view"),
                      require_any_permission("fleet.view", "audit.view")):
            assert is_route_surface_guard(guard)
            assert getattr(guard, ROUTE_SURFACE_GUARD) is True
            body = ast.parse(textwrap_dedent(inspect.getsource(guard))).body[0]
            first = next(n for n in body.body if isinstance(n, ast.Expr))
            assert ast.unparse(first) == "await enforce_route_surface(request, user)", (
                ast.unparse(first)
            )

    def test_the_bucket_is_still_the_tokens(self):
        """A25.10, on the entry point: it reads the request's own state and
        passes (request, user) through -- nothing else can choose a bucket."""
        from harkeniq_cc import read_meter

        fn = ast.parse(textwrap_dedent(inspect.getsource(
            read_meter.meter_machine_read))).body[0]
        request_uses = {
            ast.unparse(n) for n in ast.walk(fn)
            if isinstance(n, ast.Attribute)
            and ast.unparse(n).split(".")[0] == "request"
        }
        assert request_uses == {"request.state"}, request_uses
        user_uses = {
            n.attr for n in ast.walk(fn)
            if isinstance(n, ast.Attribute)
            and isinstance(n.value, ast.Name) and n.value.id == "user"
        }
        assert user_uses == set(), user_uses

    def test_the_self_helpers_no_longer_charge(self):
        from harkeniq_cc.api import operational_agents as oa

        for fn in (oa._machine_self_read, oa._machine_read_gate):
            assert not inspect.iscoroutinefunction(fn), fn.__name__
            code = inspect.getsource(fn)
            assert "_charge_machine_read(" not in code
            assert "meter_machine_read(" not in code


def textwrap_dedent(source: str) -> str:
    import textwrap

    return textwrap.dedent(source)


# ---------------------------------------------------------------------------
# 4. The matrix, generated from MACHINE_SURFACE
# ---------------------------------------------------------------------------


class TestEveryMachineReadCostsExactlyOne:
    @pytest.mark.parametrize("route", READ_ROUTES, ids=lambda r: r[1])
    async def test_served(self, route):
        stack = await _stack()
        e = await _estate(stack)
        status, charged = await _cost(stack, e.agent_id, _url(route[1], e))
        assert status == 200, (route, status)
        assert charged == 1, (route, charged)

    @pytest.mark.parametrize("route", READ_ROUTES, ids=lambda r: r[1])
    async def test_refused(self, route):
        """`self` routes refuse another agent; the others refuse an unbound job."""
        stack = await _stack()
        e = await _estate(stack)
        job = MACHINE_SURFACE[route][1]
        if job == JOB_SELF:
            status, charged = await _cost(
                stack, e.agent_id, _url(route[1], e, agent=e.other_id))
        else:
            status, charged = await _cost(
                stack, e.agent_id, _url(route[1], e), jobs={JOB_SELF})
        assert status == 403, (route, status)
        assert charged == 1, (route, charged)
        # And the agent named in the path is never charged.
        assert await _reads(stack, e.other_id) == 0

    @pytest.mark.parametrize("route", LIMIT_BEARING, ids=lambda r: r[1])
    async def test_malformed(self, route):
        """Refused before any handler line -- and charged all the same."""
        stack = await _stack()
        e = await _estate(stack)
        status, charged = await _cost(
            stack, e.agent_id, _url(route[1], e) + "?limit=not-a-number")
        assert status == 422, (route, status)
        assert charged == 1, (route, charged)

    async def test_the_limit_bearing_reads_are_the_ones_measured(self):
        """The malformed matrix leaves out nothing it should exercise: its
        list is asserted against the running app's own query parameters."""
        stack = await _stack()
        routes = api_routes(stack.app)
        with_limit = sorted(
            r for r in READ_ROUTES
            if any(p.name == "limit" for p in routes[r].dependant.query_params)
        )
        assert with_limit == LIMIT_BEARING


class TestTheThreeF3Routes:
    async def test_attention_is_metered(self):
        stack = await _stack()
        e = await _estate(stack)
        assert await _cost(stack, e.agent_id, "/api/attention/") == (200, 1)
        assert await _cost(
            stack, e.agent_id, "/api/attention/?band=high&limit=5") == (200, 1)

    async def test_the_incident_list_is_metered(self):
        stack = await _stack()
        e = await _estate(stack)
        assert await _cost(stack, e.agent_id, "/api/incidents/") == (200, 1)
        assert await _cost(
            stack, e.agent_id, "/api/incidents/?status=all") == (200, 1)

    async def test_the_incident_detail_is_metered(self):
        stack = await _stack()
        e = await _estate(stack)
        assert await _cost(stack, e.agent_id, "/api/incidents/inc-1") == (200, 1)

    async def test_a_hidden_incident_is_denied_and_still_charged(self):
        """Scope still decides: the incident at a site the agent does not
        reach is absent -- and asking for it costs what a 200 costs."""
        stack = await _stack()
        e = await _estate(stack)
        status, charged = await _cost(
            stack, e.agent_id, f"/api/incidents/{e.hidden_incident_id}")
        assert (status, charged) == (404, 1)

    async def test_a_nonexistent_incident_is_charged(self):
        stack = await _stack()
        e = await _estate(stack)
        assert await _cost(
            stack, e.agent_id, "/api/incidents/does-not-exist") == (404, 1)

    async def test_the_hidden_incident_does_not_reach_the_list(self):
        stack = await _stack()
        e = await _estate(stack)
        async with stack.as_machine(e.agent_id, permissions=EVERYTHING).client() as c:
            ids = {i["incident_id"]
                   for i in (await c.get("/api/incidents/?status=all")).json()["incidents"]}
        assert e.hidden_incident_id not in ids
        assert "inc-1" in ids


class TestNoDoubleMetering:
    async def test_the_doubly_declared_guard_charges_once(self):
        """Both declarations run; the second finds the first's window."""
        stack = await _stack()
        e = await _estate(stack)
        route = api_routes(stack.app)[("GET", "/api/attention/")]
        guards = [c for c in dependency_calls(route) if is_route_surface_guard(c)]
        assert len(guards) == 2, "the fixture no longer exercises the double guard"
        assert await _cost(stack, e.agent_id, "/api/attention/") == (200, 1)

    async def test_repeated_requests_each_cost_one(self):
        """The memo is the REQUEST's -- it never carries into the next one."""
        stack = await _stack()
        e = await _estate(stack)
        before = await _reads(stack, e.agent_id)
        async with stack.as_machine(e.agent_id, permissions=EVERYTHING).client() as c:
            for _ in range(5):
                assert (await c.get("/api/incidents/")).status_code == 200
        assert await _reads(stack, e.agent_id) - before == 5

    async def test_a_minute_boundary_between_the_two_guards_charges_once(
        self, monkeypatch,
    ):
        """The window is computed ONCE per request. An implementation that
        recomputed per guard would open a second row on the later minute."""
        stack = await _stack()
        e = await _estate(stack)
        base = read_window_start(datetime.now(timezone.utc)) + timedelta(hours=1)
        calls: list[datetime] = []

        def advancing(now=None):
            window = base + timedelta(seconds=60 * len(calls))
            calls.append(window)
            return window

        monkeypatch.setattr(ingress_limits, "read_window_start", advancing)
        async with stack.as_machine(e.agent_id, permissions=EVERYTHING).client() as c:
            assert (await c.get("/api/attention/")).status_code == 200
        assert len(calls) == 1, calls
        future = [w for w in await _windows(stack)
                  if w.agent_id == e.agent_id
                  and w.window_start.replace(tzinfo=timezone.utc) >= base]
        assert [(w.reads) for w in future] == [1], [
            (w.window_start, w.reads) for w in future]

    async def test_the_counter_mirrors_the_durable_count(self):
        stack = await _stack()
        e = await _estate(stack)
        before = _metric(stack, m.M_MACHINE_READ_METERED)
        durable = await _reads(stack, e.agent_id)
        async with stack.as_machine(e.agent_id, permissions=EVERYTHING).client() as c:
            for url in ("/api/attention/", "/api/incidents/",
                        f"/api/operational-agents/{e.agent_id}/runtime",
                        "/api/fleet/"):
                await c.get(url)
        assert (_metric(stack, m.M_MACHINE_READ_METERED) - before
                == await _reads(stack, e.agent_id) - durable == 4)


# ---------------------------------------------------------------------------
# 5. What each refusal records
# ---------------------------------------------------------------------------


class TestRefusalAccounting:
    async def test_a_permission_refusal_after_the_surface_is_charged(self):
        """Unreachable for today's agents (REQUIRED_READS gives every one
        `fleet.view`) -- and not free if it ever happens."""
        stack = await _stack()
        e = await _estate(stack)
        before = _metric(stack, f"{m.M_READ_REFUSED}_permission")
        status, charged = await _cost(
            stack, e.agent_id, f"/api/operational-agents/{e.agent_id}/runtime",
            permissions=())
        assert (status, charged) == (403, 1)
        assert _metric(stack, f"{m.M_READ_REFUSED}_permission") == before + 1

    async def test_an_off_plane_refusal_is_charged_once_with_evidence(self):
        stack = await _stack()
        e = await _estate(stack)
        status, charged = await _cost(stack, e.agent_id, "/api/fleet/")
        assert (status, charged) == (403, 1)
        rows = [w for w in await _windows(stack) if w.agent_id == e.agent_id]
        assert sum(w.refused_surface_not_allowed for w in rows) == 1
        assert _metric(stack, f"{m.M_MACHINE_READ_METERED}_{m.OFF_PLANE}") >= 1

    async def test_an_unbound_job_is_charged_once_with_evidence(self):
        stack = await _stack()
        e = await _estate(stack)
        status, charged = await _cost(
            stack, e.agent_id, "/api/incidents/", jobs={JOB_SELF})
        assert (status, charged) == (403, 1)
        rows = [w for w in await _windows(stack) if w.agent_id == e.agent_id]
        assert sum(w.refused_job_not_bound for w in rows) == 1

    async def test_a_dry_run_naming_another_agent_is_counted(self):
        """The one self rule that went unrecorded."""
        stack = await _stack()
        e = await _estate(stack)
        before = _metric(stack, f"{m.M_READ_REFUSED}_cross_agent")
        status, charged = await _cost(
            stack, e.agent_id, f"/api/operational-agents/{e.other_id}/dry-run")
        assert (status, charged) == (403, 1)
        assert _metric(stack, f"{m.M_READ_REFUSED}_cross_agent") == before + 1

    async def test_an_unauthenticated_request_has_no_bucket(self):
        stack = await _stack()

        async def refuse():
            raise HTTPException(status_code=401, detail="invalid token")

        stack.app.dependency_overrides[get_current_user] = refuse
        async with stack.client() as c:
            assert (await c.get("/api/attention/")).status_code == 401
        assert await _windows(stack) == []

    async def test_the_meter_record_names_no_resource(self):
        """Hidden ids, planted query values and refused paths are asked for;
        none of them may appear in accounting, or on /metrics."""
        stack = await _stack()
        e = await _estate(stack)
        async with stack.as_machine(e.agent_id, permissions=EVERYTHING).client() as c:
            for url in (
                f"/api/incidents/{e.hidden_incident_id}",
                f"/api/incidents/?site_id={PLANTED}&device_agent_id={PLANTED}",
                f"/api/attention/?site_id={PLANTED}&limit={PLANTED}",
                f"/api/operational-agents/{e.agent_id}/submissions/{PLANTED}",
                f"/api/fleet/{PLANTED}",
            ):
                await c.get(url)
        for row in await _windows(stack):
            values = [str(getattr(row, col.name))
                      for col in CCAgentReadWindow.__table__.columns]
            assert not any(PLANTED in v for v in values), values
        async with stack.client() as c:
            text = (await c.get("/metrics")).text
        # The unit tenant id ("t1") is too short to be a meaningful probe;
        # the live gate checks the real one.
        for leaked in (PLANTED, e.agent_id, e.other_id, e.site_id,
                       e.hidden_incident_id):
            assert leaked not in text, leaked

    async def test_the_counter_vocabulary_is_closed_and_registered(self):
        read_jobs = {j for j, meter in JOB_METER.items() if meter == METER_READ}
        assert m.MACHINE_READ_METER_LABELS == read_jobs | {m.OFF_PLANE, "other"}
        assert JOB_PROPOSALS not in m.MACHINE_READ_METER_LABELS
        stack = await _stack()
        registered = stack.state.metrics._metrics
        assert m.M_MACHINE_READ_METERED in registered
        for label in m.MACHINE_READ_METER_LABELS:
            assert f"{m.M_MACHINE_READ_METERED}_{label}" in registered, label
        m.record_machine_read_metered(f"agent-{PLANTED}")
        assert not any(PLANTED in name for name in registered)


# ---------------------------------------------------------------------------
# 6. The write is not read-metered
# ---------------------------------------------------------------------------


class TestTheWriteKeepsItsOwnLedger:
    async def _attempts(self, stack, agent_id) -> int:
        async with stack.sessionmaker() as session:
            return int((await session.execute(
                sa.select(sa.func.count()).select_from(CCAgentIngressAttempt)
                .where(CCAgentIngressAttempt.agent_id == agent_id)
            )).scalar_one())

    async def test_a_served_write_charges_no_read(self):
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        ref = await _first_ref(stack, agent_id)
        reads, attempts = await _reads(stack, agent_id), await self._attempts(stack, agent_id)
        async with stack.as_machine(agent_id, permissions=EVERYTHING).client() as c:
            res = await c.post(f"/api/operational-agents/{agent_id}/proposals",
                               json=_body(ref, key="b0c-write-0001"))
        assert res.status_code == 201, res.text
        assert await _reads(stack, agent_id) == reads
        assert await self._attempts(stack, agent_id) == attempts + 1

    async def test_the_guard_reads_the_declaration_at_runtime(self):
        """Defence in depth, exercised: were an attempt-metered route ever to
        carry the read guard (the census forbids it), the guard would still
        not read-charge a SERVED request -- the declaration decides at
        runtime, not only in CI. A REFUSED one is still charged: an
        authenticated refusal is never free (A25.10)."""
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        probe = ("POST", "/api/b0c-probe-write/")

        @stack.app.post(
            "/api/b0c-probe-write/",
            dependencies=[Depends(require_permission("proposal.submit"))],
        )
        async def b0c_probe_write():
            return {"ok": True}

        from harkeniq_cc.route_contract import SURFACE_MACHINE

        MACHINE_SURFACE[probe] = (SURFACE_MACHINE, JOB_PROPOSALS)
        ROUTE_CONTRACT[probe] = ("proposal.submit", "object_gated", True)
        try:
            before = await _reads(stack, agent_id)
            async with stack.as_machine(
                agent_id, permissions=EVERYTHING,
                jobs={JOB_SELF, JOB_PROPOSALS},
            ).client() as c:
                served = await c.post("/api/b0c-probe-write/")
            assert served.status_code == 200, served.text
            assert await _reads(stack, agent_id) == before, (
                "a served attempt-metered request was read-charged"
            )
            async with stack.as_machine(
                agent_id, permissions=EVERYTHING, jobs={JOB_SELF},
            ).client() as c:
                refused = await c.post("/api/b0c-probe-write/")
            assert refused.status_code == 403, refused.text
            assert await _reads(stack, agent_id) == before + 1
        finally:
            MACHINE_SURFACE.pop(probe)
            ROUTE_CONTRACT.pop(probe)

    async def test_a_refused_write_charges_no_read(self):
        """A29.15's shape, unchanged: the refusal is in the attempt ledger."""
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        ref = await _first_ref(stack, agent_id)
        reads, attempts = await _reads(stack, agent_id), await self._attempts(stack, agent_id)
        async with stack.as_machine(
            agent_id, permissions=EVERYTHING, jobs={JOB_SELF},
        ).client() as c:
            res = await c.post(f"/api/operational-agents/{agent_id}/proposals",
                               json=_body(ref, key="b0c-write-0002"))
        assert res.status_code == 403, res.text
        assert await _reads(stack, agent_id) == reads
        assert await self._attempts(stack, agent_id) == attempts + 1


# ---------------------------------------------------------------------------
# 7. A person is never metered
# ---------------------------------------------------------------------------


class TestHumansAreUntouched:
    async def test_no_human_read_of_the_plane_writes_a_window(self):
        stack = await _stack()
        e = await _estate(stack)
        before = await _windows(stack)
        stack.as_person()
        async with stack.client() as c:
            for route in READ_ROUTES:
                res = await c.get(_url(route[1], e))
                expected = 403 if route in MACHINE_ONLY_ROUTES else 200
                assert res.status_code == expected, (route, res.status_code)
        after = await _windows(stack)
        assert [(w.agent_id, w.reads) for w in after] == [
            (w.agent_id, w.reads) for w in before
        ], "a person's reads were billed to a machine bucket"

    async def test_a_person_is_never_throttled(self, monkeypatch):
        """Even with every machine bucket spent, a person reads on."""
        stack = await _stack()
        e = await _estate(stack)
        # One frozen window, so no minute boundary can empty the bucket
        # between spending it and proving it is spent.
        window = read_window_start(datetime.now(timezone.utc)) + timedelta(days=4)
        monkeypatch.setattr(
            ingress_limits, "read_window_start", lambda now=None: window,
        )
        for agent in (e.agent_id, e.other_id):
            await _spend(stack, agent, window, READ_MAX_PER_WINDOW * 10)
        # The premise, proved: the agents themselves are refused for rate.
        for agent in (e.agent_id, e.other_id):
            async with stack.as_machine(agent, permissions=EVERYTHING).client() as c:
                assert (await c.get("/api/attention/")).status_code == 429, agent
        stack.as_person()
        async with stack.client() as c:
            for _ in range(3):
                for url in ("/api/attention/", "/api/incidents/", "/api/incidents/inc-1",
                            f"/api/operational-agents/{e.agent_id}/runtime"):
                    assert (await c.get(url)).status_code == 200, url

    async def test_a_person_moves_no_machine_counter(self):
        stack = await _stack()
        await _estate(stack)
        before = _metric(stack, m.M_MACHINE_READ_METERED)
        stack.as_person()
        async with stack.client() as c:
            for url in ("/api/attention/", "/api/incidents/", "/api/fleet/"):
                await c.get(url)
        assert _metric(stack, m.M_MACHINE_READ_METERED) == before


# ---------------------------------------------------------------------------
# 8. The window
# ---------------------------------------------------------------------------


async def _spend(stack, agent_id, window, reads):
    """Put this agent's window at `reads`, as if polled that often.

    The estate's own setup already read through the meter (its dry-run),
    so under a frozen clock the row may exist: set it either way.
    """
    async with stack.sessionmaker() as session:
        row = (await session.execute(
            sa.select(CCAgentReadWindow).where(
                CCAgentReadWindow.tenant_id == TENANT,
                CCAgentReadWindow.agent_id == agent_id,
                CCAgentReadWindow.window_start == window,
            )
        )).scalar_one_or_none()
        if row is None:
            session.add(CCAgentReadWindow(
                tenant_id=TENANT, agent_id=agent_id, window_start=window,
                reads=reads,
            ))
        else:
            row.reads = reads
        await session.commit()


class TestTheWindow:
    @pytest.fixture
    def frozen(self, monkeypatch):
        """One window for the whole test, well clear of the real clock, so
        no minute boundary can split the budget (the A27.13 flake's shape).

        Frozen for EVERY reader of the window: the meter's `admit_read`
        and the A6-3 ingress projection, which imported the function by
        name and would otherwise read the real current minute.
        """
        from harkeniq_cc import provenance

        window = read_window_start(datetime.now(timezone.utc)) + timedelta(days=1)
        for module in (ingress_limits, provenance):
            monkeypatch.setattr(
                module, "read_window_start", lambda now=None: window,
            )
        return window

    async def test_within_budget_served_then_429(self, frozen):
        stack = await _stack()
        e = await _estate(stack)
        await _spend(stack, e.agent_id, frozen, READ_MAX_PER_WINDOW - 1)
        async with stack.as_machine(e.agent_id, permissions=EVERYTHING).client() as c:
            last = await c.get("/api/attention/")
            over = await c.get("/api/attention/")
        assert last.status_code == 200, last.text
        assert over.status_code == 429, over.text
        assert "status reads" in over.json()["detail"]
        assert "items" not in over.text, "a throttled request was served"

    async def test_the_429_is_observable(self, frozen):
        stack = await _stack()
        e = await _estate(stack)
        await _spend(stack, e.agent_id, frozen, READ_MAX_PER_WINDOW)
        before = _metric(stack, m.M_READ_RATE_LIMITED)
        async with stack.as_machine(e.agent_id, permissions=EVERYTHING).client() as c:
            for _ in range(3):
                assert (await c.get("/api/incidents/")).status_code == 429
        assert _metric(stack, m.M_READ_RATE_LIMITED) == before + 3
        row = next(w for w in await _windows(stack)
                   if w.agent_id == e.agent_id
                   and w.window_start.replace(tzinfo=timezone.utc) == frozen)
        # The counter moves before the comparison: reads beyond the limit
        # in a window ARE the 429s.
        assert row.reads - READ_MAX_PER_WINDOW == 3
        stack.as_person()
        async with stack.client() as c:
            health = (await c.get(
                f"/api/operational-agents/{e.agent_id}/ingress")).json()
        assert health["read_throttle"]["exhausted"] is True
        assert health["read_throttle"]["used"] == READ_MAX_PER_WINDOW + 3

    async def test_one_allowance_across_every_job(self, frozen):
        """Attention, incidents and `self` spend the same bucket (A25.10)."""
        stack = await _stack()
        e = await _estate(stack)
        await _spend(stack, e.agent_id, frozen, READ_MAX_PER_WINDOW - 3)
        async with stack.as_machine(e.agent_id, permissions=EVERYTHING).client() as c:
            assert (await c.get("/api/attention/")).status_code == 200
            assert (await c.get(
                f"/api/operational-agents/{e.agent_id}/runtime")).status_code == 200
            assert (await c.get("/api/incidents/inc-1")).status_code == 200
            assert (await c.get("/api/incidents/")).status_code == 429
            assert (await c.get(
                f"/api/operational-agents/{e.agent_id}/proposals")).status_code == 429

    async def test_a_new_window_serves_again(self, monkeypatch):
        stack = await _stack()
        e = await _estate(stack)
        first = read_window_start(datetime.now(timezone.utc)) + timedelta(days=2)
        current = {"window": first}
        monkeypatch.setattr(
            ingress_limits, "read_window_start",
            lambda now=None: current["window"],
        )
        await _spend(stack, e.agent_id, first, READ_MAX_PER_WINDOW)
        async with stack.as_machine(e.agent_id, permissions=EVERYTHING).client() as c:
            assert (await c.get("/api/attention/")).status_code == 429
            current["window"] = first + timedelta(seconds=60)
            assert (await c.get("/api/attention/")).status_code == 200
        fresh = [w for w in await _windows(stack)
                 if w.agent_id == e.agent_id
                 and w.window_start.replace(tzinfo=timezone.utc) == current["window"]]
        assert [w.reads for w in fresh] == [1]

    async def test_another_agents_spent_window_is_not_mine(self, frozen):
        stack = await _stack()
        e = await _estate(stack)
        await _spend(stack, e.other_id, frozen, READ_MAX_PER_WINDOW * 5)
        assert await _cost(stack, e.agent_id, "/api/attention/") == (200, 1)


# ---------------------------------------------------------------------------
# 9. Nothing else moved
# ---------------------------------------------------------------------------


class TestThePlaneDidNotMove:
    def test_the_ceiling(self):
        assert MACHINE_PRINCIPAL_CEILING == frozenset(
            {"fleet.view", "incident.view", "proposal.submit"})

    def test_the_surface(self):
        assert len(MACHINE_SURFACE) == 13
        assert len(MACHINE_ONLY_ROUTES) == 3
        assert len(ROUTE_CONTRACT) == 98
        assert SUBMIT in MACHINE_ONLY_ROUTES

    async def test_off_plane_routes_still_refuse(self):
        stack = await _stack()
        e = await _estate(stack)
        async with stack.as_machine(e.agent_id, permissions=EVERYTHING).client() as c:
            for path in ("/api/fleet/", "/api/autonomy/", "/api/capabilities/",
                         "/api/learning/signals", "/api/campaigns/",
                         "/api/operational-agents/", "/api/scope-grants/me"):
                res = await c.get(path)
                assert res.status_code == 403, (path, res.status_code)
                assert "External Agent API plane" in res.text, path
