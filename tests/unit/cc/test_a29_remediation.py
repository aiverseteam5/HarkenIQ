"""A29.15 / A29.16 (A6-4A remediation): the two independent-review findings.

HIGH 1 -- THE WRITE DID NOT CONSUME ITS OWN DECLARATION
-------------------------------------------------------
`POST .../proposals` was declared `MACHINE` with `job=proposals`, and its
live path never asked. It reached the handler through
`Depends(get_current_user)` rather than `require_permission`, so the guard
that consumes `MACHINE_SURFACE` was never on its path at all. The
declaration was authoritative for reads and decorative for the write:
removing it changed nothing, and `proposals` was enforced only indirectly,
through the permission `machine_permissions()` happens to derive from the
binding.

These tests prove RUNTIME behaviour, not the table. Each one mutates the
declaration and drives a real submission.

MEDIUM 2 -- REFUSALS WERE CHARGED BUT NOT ATTRIBUTABLE
------------------------------------------------------
An off-plane refusal moved `reads`, so it was not free -- and the only
record of WHY was a process-local counter. `/metrics` is unauthenticated,
so a tenant or agent id can never be a label there (A25.11), which is
precisely the attribution an operator needs. The evidence now rides the
window row that already exists: bounded by (tenant, agent, window), never
one row per request.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from harkeniq_cc.db.models import CCAgentReadWindow
from harkeniq_cc.db.repos import AgentReadWindowRepo
from harkeniq_cc.ingress_limits import READ_MAX_PER_WINDOW, read_window_start
from harkeniq_cc.route_contract import (
    JOB_ATTENTION, JOB_PROPOSALS, JOB_SELF, MACHINE_JOBS, MACHINE_SURFACE,
    SURFACE_MACHINE,
)

from tests.unit.cc.test_a6_external_ingress import (
    TENANT, _body, _first_ref, _ready, _stack,
)

pytestmark = pytest.mark.asyncio

SUBMIT = ("POST", "/api/operational-agents/{agent_id}/proposals")


class _Declaration:
    """Temporarily mutate the ONE canonical declaration, then restore it.

    Mutating the source of truth is the point: if the write consumed some
    private copy, or nothing at all, these mutations would not reach it.
    """

    def __init__(self, route, value):
        self.route, self.value = route, value

    def __enter__(self):
        self.saved = MACHINE_SURFACE.get(self.route)
        if self.value is None:
            MACHINE_SURFACE.pop(self.route, None)
        else:
            MACHINE_SURFACE[self.route] = self.value
        return self

    def __exit__(self, *exc):
        if self.saved is None:
            MACHINE_SURFACE.pop(self.route, None)
        else:
            MACHINE_SURFACE[self.route] = self.saved
        return False


#: A realistic external runtime: it holds `self` (which is not a binding)
#: and the `proposals` ingress binding, and nothing else. Giving it every
#: job would make the job assertions vacuous -- an agent that holds
#: everything cannot demonstrate that the declared job is what decides.
INGRESS_JOBS = frozenset({JOB_SELF, JOB_PROPOSALS})


async def _submit(stack, agent_id, ref, key="a29-remed-0001",
                  jobs=INGRESS_JOBS):
    async with stack.as_machine(agent_id, jobs=jobs).client() as c:
        return await c.post(
            f"/api/operational-agents/{agent_id}/proposals",
            json=_body(ref, key=key),
        )


# ---------------------------------------------------------------------------
# HIGH 1 -- A through E, as the review specified them
# ---------------------------------------------------------------------------


class TestTheWriteConsumesTheDeclaration:
    async def test_A_a_bound_machine_submits_as_before(self):
        """A6-1 behaviour, unchanged, with the declaration in place."""
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        ref = await _first_ref(stack, agent_id)
        res = await _submit(stack, agent_id, ref)
        assert res.status_code == 201, res.text
        assert res.json()["proposal"]["id"]

    async def test_B_removing_the_declaration_fails_the_write_CLOSED(self):
        """The defect, in one assertion.

        Before the fix this submission still returned 201 with the route
        absent from the declaration, which is what "the declaration is not
        authoritative" means.
        """
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        ref = await _first_ref(stack, agent_id)
        with _Declaration(SUBMIT, None):
            res = await _submit(stack, agent_id, ref, key="a29-remed-0002")
        assert res.status_code == 403, res.text
        assert "External Agent API plane" in res.text

    async def test_C_a_different_declared_job_fails_the_write_CLOSED(self):
        """The typed job is load-bearing, not the permission alone.

        The agent's permissions are untouched -- it still holds
        `proposal.submit` -- and only the route's declared JOB moves. It
        binds `proposals` and not `attention`, so it must be refused, and
        nothing but the declared job can explain the difference from test
        A, which is the same agent making the same call.
        """
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        ref = await _first_ref(stack, agent_id)
        with _Declaration(SUBMIT, (SURFACE_MACHINE, JOB_ATTENTION)):
            res = await _submit(stack, agent_id, ref, key="a29-remed-0003")
        assert res.status_code == 403, res.text
        assert "attention" in res.text and "binding" in res.text

    async def test_D_restoring_the_declaration_restores_the_write(self):
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        ref = await _first_ref(stack, agent_id)
        with _Declaration(SUBMIT, None):
            assert (await _submit(
                stack, agent_id, ref, key="a29-remed-0004")).status_code == 403
        res = await _submit(stack, agent_id, ref, key="a29-remed-0005")
        assert res.status_code == 201, res.text

    async def test_E_the_refusal_is_metered_by_the_ATTEMPT_ledger(self):
        """A24.13 is preserved: a surface refusal on the write is not free.

        The write cannot meter through the read window -- it is not a read
        -- so the decision is consumed INSIDE the attempt ledger, which is
        also why it cannot be a route dependency: a dependency resolves
        before the handler, and the refusal would arrive before
        `admit_attempt`.
        """
        from harkeniq_cc.db.repos import AgentIngressAttemptRepo
        from harkeniq_cc.ingress_limits import ATTEMPT_WINDOW_S

        stack = await _stack()
        agent_id, _ = await _ready(stack)
        ref = await _first_ref(stack, agent_id)

        async def used():
            async with stack.sessionmaker() as s:
                return await AgentIngressAttemptRepo(s).count_since(
                    TENANT, agent_id,
                    datetime.now(timezone.utc) - timedelta(seconds=ATTEMPT_WINDOW_S),
                )

        before = await used()
        with _Declaration(SUBMIT, None):
            assert (await _submit(
                stack, agent_id, ref, key="a29-remed-0006")).status_code == 403
        assert await used() == before + 1, (
            "a surface-refused submission was free; A24.13 requires every "
            "authenticated attempt to be charged"
        )

    async def test_the_decision_has_ONE_source(self):
        """No write-specific machine policy path was introduced."""
        import ast
        import inspect

        from harkeniq_cc.api import deps
        from harkeniq_cc.api.operational_agents import submit_proposal

        source = inspect.getsource(submit_proposal)
        assert "evaluate_route_surface" in source, (
            "the write does not consume the canonical decision"
        )
        # It must not re-derive the answer for itself.
        assert "MACHINE_SURFACE" not in source and "machine_surface" not in source, (
            "the write reads the declaration directly instead of asking the "
            "one decision function"
        )
        # And the read plane's enforcer must be built ON the same function
        # rather than repeating its logic.
        enforcer = inspect.getsource(deps.enforce_route_surface)
        assert "evaluate_route_surface" in enforcer

    async def test_the_declaration_is_unchanged_by_this_remediation(self):
        assert MACHINE_SURFACE[SUBMIT] == (SURFACE_MACHINE, JOB_PROPOSALS)
        assert len(MACHINE_SURFACE) == 13


# ---------------------------------------------------------------------------
# MEDIUM 2 -- bounded, attributable refusal evidence
# ---------------------------------------------------------------------------


async def _windows(stack, agent_id=None, tenant=TENANT):
    async with stack.sessionmaker() as s:
        q = sa.select(CCAgentReadWindow).where(
            CCAgentReadWindow.tenant_id == tenant
        )
        if agent_id:
            q = q.where(CCAgentReadWindow.agent_id == agent_id)
        return list((await s.execute(q)).scalars().all())


async def _refuse(stack, agent_id, n=1, path="/api/fleet/", jobs=None):
    async with stack.as_machine(agent_id, jobs=jobs).client() as c:
        for _ in range(n):
            res = await c.get(path)
    return res


class TestRefusalsAreAttributableAndBounded:
    async def test_1_an_off_plane_read_marks_its_OWN_window(self):
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        assert (await _refuse(stack, agent_id)).status_code == 403
        rows = await _windows(stack, agent_id)
        assert rows and sum(r.surface_refused for r in rows) == 1
        assert sum(r.refused_surface_not_allowed for r in rows) == 1
        assert sum(r.refused_job_not_bound for r in rows) == 0

    async def test_2_another_agents_window_does_not_move(self):
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        await _refuse(stack, agent_id, n=3)
        other = await _windows(stack, "some-other-agent")
        assert sum(r.surface_refused for r in other) == 0

    async def test_3_another_tenants_window_does_not_move(self):
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        await _refuse(stack, agent_id, n=2)
        async with stack.sessionmaker() as s:
            rows = list((await s.execute(
                sa.select(CCAgentReadWindow).where(
                    CCAgentReadWindow.tenant_id != TENANT)
            )).scalars().all())
        assert rows == []

    async def test_4_the_last_refusal_timestamp_updates(self):
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        await _refuse(stack, agent_id)
        first = [r.last_surface_refused_at for r in await _windows(stack, agent_id)]
        assert all(t is not None for t in first) and first
        await _refuse(stack, agent_id)
        second = [r.last_surface_refused_at for r in await _windows(stack, agent_id)]
        assert max(t for t in second if t) >= max(t for t in first if t)

    async def test_5_the_reason_vocabulary_is_closed_and_in_the_schema(self):
        """A caller can never mint a column, and no free text is stored."""
        assert set(AgentReadWindowRepo.REFUSAL_COLUMNS) == {
            "surface_not_allowed", "machine_job_not_bound",
        }
        cols = set(CCAgentReadWindow.__table__.columns.keys())
        assert set(AgentReadWindowRepo.REFUSAL_COLUMNS.values()) <= cols
        # An unrecognised reason counts in the TOTAL and nowhere else.
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        await _refuse(stack, agent_id)
        async with stack.sessionmaker() as s:
            total = await AgentReadWindowRepo(s).record_refusal(
                tenant_id=TENANT, agent_id=agent_id,
                window_start=read_window_start(), reason="<injected>",
                at=datetime.now(timezone.utc),
            )
            await s.commit()
        assert total == 2
        rows = await _windows(stack, agent_id)
        assert sum(r.refused_surface_not_allowed for r in rows) == 1
        assert sum(r.refused_job_not_bound for r in rows) == 0

    async def test_6_the_job_refusal_is_counted_in_its_own_column(self):
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        # On the plane, but this agent holds no `attention` binding.
        res = await _refuse(
            stack, agent_id, path="/api/attention/",
            jobs={JOB_SELF, JOB_PROPOSALS},
        )
        assert res.status_code == 403 and "binding" in res.text
        rows = await _windows(stack, agent_id)
        assert sum(r.refused_job_not_bound for r in rows) == 1
        assert sum(r.refused_surface_not_allowed for r in rows) == 0

    async def test_the_charge_and_the_evidence_share_ONE_window(self):
        """The minute-boundary defect, pinned deterministically.

        `_charge_machine_read` opens the row for the window it computes
        and RETURNS which one. If the evidence recomputed the window it
        would target the next one across a boundary, and `record_refusal`
        deliberately does not create rows -- so the evidence vanished. CI
        found it as a job that passed on one run and failed on another at
        the SAME commit, which is what a boundary looks like when you
        assume it is a flake.

        The window is threaded rather than taken as a `now` parameter,
        because a bucket-selecting value in `_charge_machine_read`'s
        signature is exactly the property A25.9 asserts is absent.
        """
        from harkeniq_cc.api.operational_agents import (
            _record_surface_refusal_window,
        )

        stack = await _stack()
        agent_id, _ = await _ready(stack)

        # A window that is deliberately NOT the current one. An
        # implementation that recomputes `now` targets the current window
        # instead, finds no row, and drops the evidence.
        past = read_window_start(datetime.now(timezone.utc) - timedelta(seconds=180))
        assert past != read_window_start(datetime.now(timezone.utc)), (
            "the fixture is vacuous: both windows are the same"
        )

        class _Req:
            app = type("A", (), {"state": type("S", (), {})()})()

        request = _Req()
        request.app.state.cc = stack.app.state.cc
        user = type("U", (), {
            "tenant_id": TENANT, "user_id": agent_id, "species": "agent",
        })()

        # Open the row the charge would have opened for that window.
        async with stack.sessionmaker() as s:
            await AgentReadWindowRepo(s).increment(
                tenant_id=TENANT, agent_id=agent_id, window_start=past,
            )
            await s.commit()

        await _record_surface_refusal_window(
            request, user, "surface_not_allowed", window=past,
        )

        rows = await _windows(stack, agent_id)
        marked = [r for r in rows if r.surface_refused]
        assert marked, (
            "the evidence was written to a window the charge never opened"
        )
        assert sum(r.surface_refused for r in marked) == 1
        assert all(
            r.window_start.replace(tzinfo=timezone.utc) == past
            for r in marked
        ), [r.window_start for r in marked]

    async def test_the_charge_does_not_take_a_bucket_selecting_argument(self):
        """A25.9's property, preserved by the remediation.

        The first fix threaded an instant as a PARAMETER, which put a
        window-selecting value in the signature and turned two A25.9
        structural tests red. Returning the window instead keeps the
        boundary correct and the property decidable where A25.9 decides
        it.
        """
        import inspect

        from harkeniq_cc.api.operational_agents import _charge_machine_read

        params = set(inspect.signature(_charge_machine_read).parameters)
        assert params == {"request", "user"}, params

    async def test_7_many_refusals_create_NO_per_request_rows(self):
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        await _refuse(stack, agent_id, n=25)
        rows = await _windows(stack, agent_id)
        assert len(rows) <= 2, (
            f"25 refusals created {len(rows)} rows: storage follows requests"
        )
        assert sum(r.surface_refused for r in rows) == 25

    async def test_8_the_evidence_shares_the_existing_retention(self):
        """No second lifecycle: it is the read window's row."""
        import inspect

        src = inspect.getsource(AgentReadWindowRepo.prune)
        assert "CCAgentReadWindow" in src
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        await _refuse(stack, agent_id)
        async with stack.sessionmaker() as s:
            await AgentReadWindowRepo(s).prune(
                datetime.now(timezone.utc) + timedelta(days=1)
            )
            await s.commit()
        assert await _windows(stack, agent_id) == []

    async def test_9_refusals_are_still_CHARGED_and_still_429(self):
        """A25.10 and the rate limit both survive the new evidence."""
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        async with stack.sessionmaker() as s:
            for _ in range(READ_MAX_PER_WINDOW):
                await AgentReadWindowRepo(s).increment(
                    tenant_id=TENANT, agent_id=agent_id,
                    window_start=read_window_start(),
                )
            await s.commit()
        res = await _refuse(stack, agent_id)
        assert res.status_code == 429, (
            "an exhausted agent kept probing the plane for 403s"
        )

    async def test_10_no_authenticated_off_plane_probe_is_free(self):
        stack = await _stack()
        agent_id, _ = await _ready(stack)

        async def reads():
            async with stack.sessionmaker() as s:
                return await AgentReadWindowRepo(s).usage(
                    tenant_id=TENANT, agent_id=agent_id,
                    window_start=read_window_start(),
                )

        before = await reads()
        await _refuse(stack, agent_id, n=5)
        assert await reads() >= before + 5

    async def test_the_metrics_surface_still_carries_no_identifier(self):
        """A25.11: the durable evidence is attributable; the metric is not."""
        from harkeniq_cc.metrics import (
            SURFACE_REFUSAL_REASONS, M_SURFACE_REFUSED,
        )

        assert SURFACE_REFUSAL_REASONS == {
            "surface_not_allowed", "machine_job_not_bound",
            "machine_only_route", "other",
        }
        assert "{" not in M_SURFACE_REFUSED
