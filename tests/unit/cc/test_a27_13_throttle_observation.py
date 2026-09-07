"""A27.13: a real 429 must be observable, and still not amplifiable.

THE DEFECT, AS FOUND
--------------------
A24.13 refuses an over-limit submission WITHOUT writing to the attempt
ledger. That is correct twice over: a record that grew on every refusal
would amplify the traffic it exists to bound, and a rejection counted as
an *attempt* would consume the allowance it was just refused for.

But `build_ingress_health` read `throttled` out of that same ledger --
`attempts.get("throttled", 0)` -- and `throttled` is not in the attempt
vocabulary at all (`accepted | replayed | conflict | rejected |
refused`). So the field was zero for every agent forever, A27.11's
`throttled` state was unreachable by any amount of real traffic, and an
operator could not tell a silent runtime from one being refused at the
door.

Both facts had to survive. The rejection is now counted in its own
structure, bounded by TIME rather than by request count: one row per
(tenant, agent, aligned minute), incremented in place, so a flood of a
million requests writes one row.

WHAT THIS MODULE HOLDS
----------------------
* a REJECTED request is observed -- through the real route, not a helper
* a request that merely takes the last slot is NOT throttling
* the attempt ledger is still not grown by refusals
* storage is bounded under flood
* concurrent rejections neither lose nor corrupt the count
* `/ingress` exposes it and `activity_state` can actually reach
  `throttled` from real traffic
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from harkeniq_cc.db.models import CCAgentThrottleWindow
from harkeniq_cc.db.repos import (
    AgentIngressAttemptRepo, AgentThrottleWindowRepo,
)
from harkeniq_cc.ingress_limits import (
    ATTEMPT_MAX, ATTEMPT_WINDOW_S, THROTTLE_RETENTION_S, THROTTLE_WINDOW_S,
    admit_attempt, record_throttled, throttle_window_start,
    throttling_observed,
)
from harkeniq_cc.provenance import STATE_THROTTLED, _aware, activity_state

from tests.unit.cc.test_a6_external_ingress import (
    TENANT, _body, _first_ref, _ready, _stack,
)


async def _fill_window(stack, agent_id: str, n: int = ATTEMPT_MAX) -> None:
    """Spend the agent's whole allowance, the way real traffic would."""
    async with stack.sessionmaker() as session:
        repo = AgentIngressAttemptRepo(session)
        for _ in range(n):
            await repo.record(
                tenant_id=TENANT, agent_id=agent_id, outcome="accepted",
            )
        await session.commit()


async def _throttle_rows(stack, agent_id: str) -> list[CCAgentThrottleWindow]:
    async with stack.sessionmaker() as session:
        return list((await session.execute(
            sa.select(CCAgentThrottleWindow).where(
                CCAgentThrottleWindow.tenant_id == TENANT,
                CCAgentThrottleWindow.agent_id == agent_id,
            )
        )).scalars().all())


# ---------------------------------------------------------------------------
# 1. The route: a real 429 leaves real evidence
# ---------------------------------------------------------------------------


class TestARealRejectionIsObserved:
    async def test_a_429_from_the_route_is_recorded(self):
        """The whole point: drive the real handler, not the helper."""
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        ref = await _first_ref(stack, agent_id)

        async with stack.sessionmaker() as session:
            before, _ = await throttling_observed(
                session, tenant_id=TENANT, agent_id=agent_id,
            )
        assert before == 0, "nothing has been refused yet"

        await _fill_window(stack, agent_id)
        async with stack.as_machine(agent_id).client() as c:
            res = await c.post(
                f"/api/operational-agents/{agent_id}/proposals", json=_body(ref),
            )
        assert res.status_code == 429, res.text

        async with stack.sessionmaker() as session:
            count, last_at = await throttling_observed(
                session, tenant_id=TENANT, agent_id=agent_id,
            )
        assert count == 1, "a real 429 left no observation"
        assert last_at is not None, "the rejection has no timestamp"

    async def test_the_observation_commits_with_the_429(self):
        """A 429 whose evidence rolled back would be worse than none."""
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        ref = await _first_ref(stack, agent_id)
        await _fill_window(stack, agent_id)

        async with stack.as_machine(agent_id).client() as c:
            assert (await c.post(
                f"/api/operational-agents/{agent_id}/proposals", json=_body(ref),
            )).status_code == 429
        # Read in a SEPARATE session: an observation visible only inside
        # the request's own transaction is not durable evidence.
        assert len(await _throttle_rows(stack, agent_id)) == 1

    async def test_the_attempt_ledger_is_still_not_grown_by_refusals(self):
        """A24.13's bound is preserved, not traded away for the signal."""
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        ref = await _first_ref(stack, agent_id)
        await _fill_window(stack, agent_id)

        async with stack.as_machine(agent_id).client() as c:
            for n in range(5):
                assert (await c.post(
                    f"/api/operational-agents/{agent_id}/proposals",
                    json=_body(ref, key=f"idem-{n:04d}-aaaa"),
                )).status_code == 429

        async with stack.sessionmaker() as session:
            since = datetime.now(timezone.utc) - timedelta(
                seconds=ATTEMPT_WINDOW_S)
            used = await AgentIngressAttemptRepo(session).count_since(
                TENANT, agent_id, since,
            )
        assert used == ATTEMPT_MAX, (
            "a refused request entered the attempt ledger, where it would "
            "consume the allowance it was refused for"
        )

    async def test_a_rejection_is_charged_to_the_caller_alone(self):
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        ref = await _first_ref(stack, agent_id)
        await _fill_window(stack, agent_id)
        async with stack.as_machine(agent_id).client() as c:
            await c.post(f"/api/operational-agents/{agent_id}/proposals",
                         json=_body(ref))

        async with stack.sessionmaker() as session:
            other, _ = await throttling_observed(
                session, tenant_id=TENANT, agent_id="some-other-agent",
            )
        assert other == 0


# ---------------------------------------------------------------------------
# 2. Reaching the limit is not the same fact as being refused
# ---------------------------------------------------------------------------


class TestReachingTheLimitIsNotThrottling:
    async def test_the_request_that_takes_the_last_slot_is_served(self):
        """It is not throttling. That request happened."""
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        await _fill_window(stack, agent_id, ATTEMPT_MAX - 1)

        async with stack.sessionmaker() as session:
            permitted, used = await admit_attempt(
                session, tenant_id=TENANT, agent_id=agent_id,
            )
        assert permitted is True
        assert used == ATTEMPT_MAX - 1
        assert await _throttle_rows(stack, agent_id) == [], (
            "taking the last slot was recorded as throttling, which would "
            "make the state mean 'at the limit' -- a commoner, different fact"
        )

    async def test_admit_attempt_never_writes_the_observation_itself(self):
        """The DECISION does not write; the caller records the outcome."""
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        await _fill_window(stack, agent_id)

        async with stack.sessionmaker() as session:
            permitted, _ = await admit_attempt(
                session, tenant_id=TENANT, agent_id=agent_id,
            )
            await session.commit()
        assert permitted is False
        assert await _throttle_rows(stack, agent_id) == [], (
            "the rate decision wrote an observation on its own, so a caller "
            "that only asked would also have recorded"
        )


# ---------------------------------------------------------------------------
# 3. Bounded by time, not by traffic
# ---------------------------------------------------------------------------


class TestStorageIsBounded:
    async def test_a_flood_writes_one_row_per_minute_and_increments_it(self):
        """Storage follows the CLOCK, not the request count.

        The bound is stated against elapsed time rather than a fixed
        number: a slow run legitimately crosses a bucket boundary, and
        asserting `== 1` would be asserting that the test was fast --
        which the full suite duly falsified.
        """
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        ref = await _first_ref(stack, agent_id)
        await _fill_window(stack, agent_id)

        started = datetime.now(timezone.utc)
        async with stack.as_machine(agent_id).client() as c:
            for n in range(40):
                assert (await c.post(
                    f"/api/operational-agents/{agent_id}/proposals",
                    json=_body(ref, key=f"flood-{n:04d}-aaaa"),
                )).status_code == 429
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()

        rows = await _throttle_rows(stack, agent_id)
        ceiling = int(elapsed // THROTTLE_WINDOW_S) + 1
        assert len(rows) <= ceiling, (
            f"40 rejected requests over {elapsed:.1f}s created {len(rows)} "
            f"rows, more than the {ceiling} the clock allows: storage grows "
            "with the traffic it is meant to bound"
        )
        assert sum(r.rejected for r in rows) == 40, "a rejection was lost"

    async def test_row_count_is_capped_by_the_window_not_the_request_count(self):
        """One bucket per minute, whatever arrives inside it."""
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        base = datetime.now(timezone.utc)

        async with stack.sessionmaker() as session:
            for minute in range(5):
                at = base - timedelta(seconds=THROTTLE_WINDOW_S * minute)
                for _ in range(50):
                    await record_throttled(
                        session, tenant_id=TENANT, agent_id=agent_id, now=at,
                    )
            await session.commit()

        rows = await _throttle_rows(stack, agent_id)
        assert len(rows) == 5, (
            f"250 rejections across 5 minutes made {len(rows)} rows"
        )
        assert sum(r.rejected for r in rows) == 250, "a rejection was lost"

    async def test_spent_buckets_are_pruned(self):
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        now = datetime.now(timezone.utc)
        ancient = now - timedelta(seconds=THROTTLE_RETENTION_S * 2)

        async with stack.sessionmaker() as session:
            await record_throttled(
                session, tenant_id=TENANT, agent_id=agent_id, now=ancient,
            )
            await session.commit()
        assert len(await _throttle_rows(stack, agent_id)) == 1

        # Opening a NEW bucket is what runs housekeeping -- deliberately
        # not every rejection, which would put a DELETE in front of the
        # flood this design exists to survive.
        async with stack.sessionmaker() as session:
            await record_throttled(
                session, tenant_id=TENANT, agent_id=agent_id, now=now,
            )
            await session.commit()

        rows = await _throttle_rows(stack, agent_id)
        assert len(rows) == 1, "the expired bucket outlived its horizon"
        # sqlite hands back a naive datetime for a tz-aware column; the
        # surviving bucket is the CURRENT one either way.
        assert _aware(rows[0].window_start) >= throttle_window_start(now)

    async def test_pruning_never_drops_a_bucket_still_reported(self):
        """The horizon is wider than the window the projection reads."""
        assert THROTTLE_RETENTION_S > ATTEMPT_WINDOW_S


# ---------------------------------------------------------------------------
# 4. Concurrency
# ---------------------------------------------------------------------------


class TestConcurrentRejections:
    async def test_interleaved_rejections_are_not_lost(self):
        """Sequential-but-interleaved writers must total exactly.

        sqlite `:memory:` is a StaticPool -- one shared connection -- so
        two sessions here cannot be genuinely isolated. This asserts the
        arithmetic; the real-engine proof lives in the PostgreSQL suite,
        where separate connections actually race.
        """
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        at = datetime.now(timezone.utc)

        async def one():
            async with stack.sessionmaker() as session:
                await record_throttled(
                    session, tenant_id=TENANT, agent_id=agent_id, now=at,
                )
                await session.commit()

        for _ in range(25):
            await one()

        rows = await _throttle_rows(stack, agent_id)
        assert len(rows) == 1
        assert rows[0].rejected == 25, (
            f"25 rejections totalled {rows[0].rejected}: increments were lost"
        )

    async def test_a_lost_open_race_costs_only_that_statement(self):
        """A25.12's rule: a helper never rolls back its caller's work.

        The savepoint is what makes the loser of an open race retry
        instead of discarding a transaction it knows nothing about.
        """
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        window = throttle_window_start()

        async with stack.sessionmaker() as session:
            # Somebody else opened this window first.
            session.add(CCAgentThrottleWindow(
                tenant_id=TENANT, agent_id=agent_id, window_start=window,
                rejected=1, last_at=datetime.now(timezone.utc),
            ))
            await session.commit()

        async with stack.sessionmaker() as session:
            marker = await AgentIngressAttemptRepo(session).record(
                tenant_id=TENANT, agent_id=agent_id, outcome="accepted",
            )
            total = await AgentThrottleWindowRepo(session).record(
                tenant_id=TENANT, agent_id=agent_id, window_start=window,
                at=datetime.now(timezone.utc),
            )
            await session.commit()

        assert total == 2
        async with stack.sessionmaker() as session:
            assert await session.get(type(marker), marker.id) is not None, (
                "the counter discarded work its caller had already done"
            )


# ---------------------------------------------------------------------------
# 5. The operator can see it, end to end
# ---------------------------------------------------------------------------


class TestTheOperatorCanSeeIt:
    async def test_ingress_reports_the_throttling_and_the_state_follows(self):
        """Fill -> 429 -> read /ingress -> throttled, through real HTTP."""
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        ref = await _first_ref(stack, agent_id)
        await _fill_window(stack, agent_id)

        async with stack.as_machine(agent_id).client() as c:
            assert (await c.post(
                f"/api/operational-agents/{agent_id}/proposals", json=_body(ref),
            )).status_code == 429

        async with stack.as_person().client() as c:
            res = await c.get(f"/api/operational-agents/{agent_id}/ingress")
        assert res.status_code == 200, res.text
        body = res.json()

        activity = body["submission_activity"]
        assert activity["throttled"] == 1, (
            "the operator cannot see that the runtime is being refused"
        )
        assert activity["last_throttled_at"], "no time for the refusal"
        assert body["activity_state"] == STATE_THROTTLED, body["activity_state"]

    async def test_the_state_was_genuinely_unreachable_before(self):
        """The signal is the ONLY thing that can produce this state.

        With the attempt ledger alone -- which is all the projection had
        -- every input combination the ledger can produce lands somewhere
        else, which is what made this a defect rather than a rare case.
        """
        now = datetime.now(timezone.utc)
        reachable = {
            activity_state(
                last_authenticated_at=now, last_attempt_at=now,
                accepted=a, refused=r, throttled=0, now=now,
            )
            for a in (0, 1, 5)
            for r in (0, 1, 5)
        }
        assert STATE_THROTTLED not in reachable

        assert activity_state(
            last_authenticated_at=now, last_attempt_at=now,
            accepted=5, refused=0, throttled=1, now=now,
        ) == STATE_THROTTLED

    async def test_a_quiet_agent_still_reports_zero(self):
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        async with stack.as_person().client() as c:
            body = (await c.get(
                f"/api/operational-agents/{agent_id}/ingress")).json()
        assert body["submission_activity"]["throttled"] == 0
        assert body["submission_activity"]["last_throttled_at"] is None
        assert body["activity_state"] != STATE_THROTTLED

    async def test_the_reported_window_matches_the_attempt_window(self):
        """One horizon on the screen, not two that disagree."""
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        now = datetime.now(timezone.utc)

        async with stack.sessionmaker() as session:
            # Inside the attempt window, but in an older bucket.
            await record_throttled(
                session, tenant_id=TENANT, agent_id=agent_id,
                now=now - timedelta(seconds=ATTEMPT_WINDOW_S // 2),
            )
            # Outside it entirely.
            await record_throttled(
                session, tenant_id=TENANT, agent_id=agent_id,
                now=now - timedelta(seconds=ATTEMPT_WINDOW_S * 2),
            )
            await session.commit()

        async with stack.as_person().client() as c:
            body = (await c.get(
                f"/api/operational-agents/{agent_id}/ingress")).json()
        assert body["submission_activity"]["window_seconds"] == ATTEMPT_WINDOW_S
        assert body["submission_activity"]["throttled"] == 1, (
            "a rejection outside the reported window was counted inside it"
        )

    async def test_no_new_field_carries_identity_material(self):
        stack = await _stack()
        agent_id, _ = await _ready(stack)
        async with stack.as_person().client() as c:
            body = (await c.get(
                f"/api/operational-agents/{agent_id}/ingress")).json()
        blob = str(body).lower()
        for banned in ("client_id", "secret", "keycloak", "realm", "sub-"):
            assert banned not in blob, banned


# ---------------------------------------------------------------------------
# 6. The contract did not move
# ---------------------------------------------------------------------------


class TestNothingElseMoved:
    def test_the_attempt_vocabulary_is_unchanged(self):
        """The fix did NOT add a `throttled` outcome to the ledger.

        Doing so would have made a rejection consume the allowance it was
        refused for -- the limit eating itself.
        """
        from harkeniq_cc import ingress_limits

        outcomes = {
            v for k, v in vars(ingress_limits).items()
            if k.startswith("OUTCOME_")
        }
        assert outcomes == {
            "accepted", "replayed", "conflict", "rejected", "refused",
        }
        assert "throttled" not in outcomes

    def test_the_route_still_declares_fleet_view_and_read_scoped(self):
        from harkeniq_cc.route_contract import READ_SCOPED, ROUTE_CONTRACT

        assert ROUTE_CONTRACT[
            ("GET", "/api/operational-agents/{agent_id}/ingress")
        ] == ("fleet.view", READ_SCOPED, False)

    def test_the_machine_ceiling_is_unchanged(self):
        from harkeniq_cc.machine_identity import MACHINE_PRINCIPAL_CEILING

        assert MACHINE_PRINCIPAL_CEILING == frozenset(
            {"fleet.view", "incident.view", "proposal.submit"}
        )

    def test_the_limits_themselves_are_unchanged(self):
        assert ATTEMPT_WINDOW_S == 3600
        assert ATTEMPT_MAX == 240

    def test_only_the_route_records_a_rejection(self):
        """One writer, so the signal cannot be manufactured elsewhere."""
        import ast
        import pathlib

        root = pathlib.Path(
            "services/central_command/src/harkeniq_cc"
        ).resolve()
        callers = []
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "record_throttled"
                ):
                    callers.append(path.name)
        assert callers == ["operational_agents.py"], callers
