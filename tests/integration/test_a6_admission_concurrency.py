"""A24.6: two callers cannot admit the same candidate concurrently.

This is the one A6-1 guarantee the unit suite CANNOT prove, and saying so
precisely matters. On sqlite the admission lock is a deliberate no-op --
sqlite is single-writer, so the unit tests pass because the requests are
serialized by the engine, not because the lock works. A lock that was
silently broken would look identical there.

The failure being excluded is specific. Idempotency keys make a REPLAY
safe: same key, same work, one row. They cannot see a LOGICAL duplicate,
because two callers presenting different keys collide on nothing --
`unique(tenant_id, agent_id, idempotency_key)` is satisfied by both. So
without the lock, two concurrent submissions of the same governed
candidate each read a dedupe key set that does not yet contain it, and
each create a proposal. One piece of work, approved twice, dispatched
twice.

Proved here the way A23-3 proved concurrent last-admin revokes: two real
sessions on real PostgreSQL, overlapping deliberately, asserting exactly
one winner.

Gated on ``HARKEN_TEST_CC_PG_DSN``; skipped when unset.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
import sqlalchemy as sa

from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.models import CCAgentProposal
from harkeniq_cc.proposal_admission import (
    CODE_DUPLICATE,
    admit_proposal,
    lock_proposal_admission,
)

DSN = os.environ.get("HARKEN_TEST_CC_PG_DSN", "")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not DSN, reason="HARKEN_TEST_CC_PG_DSN not set"),
]


def _payload(tenant: str, agent_id: str, dedupe_key: str) -> dict:
    return {
        "tenant_id": tenant,
        "agent_id": agent_id,
        "actor": f"op-agent:{agent_id}@v1",
        "agent_version": 1,
        "site_id": "site-1",
        "device_agent_id": "node-1",
        "action_type": "IDENTIFY_LED",
        "params": {"target": "Disk.Bay.1"},
        "rationale": "concurrency proof",
        "evidence": {},
        "disposition": "requires_approval",
        "disposition_reason": "",
        "blocking_conditions": [],
        "authorization_basis": "human_approval",
        "status": "awaiting_approval",
        "decided_by": "",
        "decided_at": None,
        "dedupe_key": dedupe_key,
    }


async def _engine():
    engine = make_engine(DSN)
    await create_all(engine)
    return engine, make_sessionmaker(engine)


class TestTheAdmissionLockIsReal:
    async def test_the_lock_is_actually_taken_on_postgres(self):
        """If this ever returns False, every test below proves nothing."""
        engine, sessionmaker = await _engine()
        try:
            async with sessionmaker() as session:
                assert await lock_proposal_admission(session, "t-lock") is True
        finally:
            await engine.dispose()

    async def test_a_second_caller_waits_until_the_first_transaction_ends(self):
        """The PRIMARY guard, and the only deterministic one.

        The end-to-end race below is the honest scenario but an unreliable
        detector: whether the loser's dedupe SELECT lands before the
        winner's INSERT depends on I/O interleaving, so with the lock
        deliberately removed it produced two rows only about three times
        in five. A regression test that misses the regression 40% of the
        time is worse than no test, because it reads as coverage.

        This asserts the property itself, in a fixed order: a second
        caller cannot pass the lock until the first caller's transaction
        ends. Without a real lock the waiter acquires immediately and the
        recorded order inverts -- so this fails every time, not sometimes.
        """
        engine, sessionmaker = await _engine()
        tenant = f"t-{uuid.uuid4().hex[:8]}"
        acquired = asyncio.Event()
        order: list[str] = []

        async def holder():
            async with sessionmaker() as session:
                await lock_proposal_admission(session, tenant)
                acquired.set()
                # Long enough that a waiter which is NOT blocked will
                # certainly record itself first.
                await asyncio.sleep(0.5)
                order.append("holder-committed")
                await session.commit()

        async def waiter():
            await acquired.wait()
            async with sessionmaker() as session:
                await lock_proposal_admission(session, tenant)
                order.append("waiter-acquired")
                await session.commit()

        try:
            await asyncio.wait_for(
                asyncio.gather(holder(), waiter()), timeout=20
            )
            assert order == ["holder-committed", "waiter-acquired"], (
                "the second caller passed the admission lock while the first "
                "still held it: admission is not serialized, and two callers "
                "can admit the same candidate"
            )
        finally:
            await engine.dispose()

    async def test_two_concurrent_admissions_yield_exactly_one_proposal(self):
        """The end-to-end scenario, complementing the test above.

        With the lock present this is deterministic: exactly one proposal,
        every run. It is kept because it exercises the real path -- two
        `admit_proposal` calls, two transactions, one governed candidate --
        rather than the lock in isolation. It is NOT the primary guard;
        see the ordering test above for why.
        """
        engine, sessionmaker = await _engine()
        tenant = f"t-{uuid.uuid4().hex[:8]}"
        agent_id = uuid.uuid4().hex[:32]
        dedupe_key = f"{agent_id}:node-1:IDENTIFY_LED:inc-1:Disk.Bay.1"
        # The overlap has to be real, and getting it wrong is easy: an
        # earlier version of this test delayed the second caller BEFORE
        # admission, so the first had already committed and the two never
        # raced. It passed with the lock removed -- proving nothing.
        #
        # Both callers must be inside `admit_proposal` before either
        # commits. A barrier releases them together, and the winner then
        # holds its transaction open briefly so the loser's dedupe read
        # is guaranteed to happen against UNCOMMITTED state -- which is
        # precisely the window the lock exists to close.
        gate = asyncio.Barrier(2)

        async def submit(hold: float):
            async with sessionmaker() as session:
                await gate.wait()
                row, code, _reason = await admit_proposal(
                    session,
                    tenant_id=tenant,
                    payload=_payload(tenant, agent_id, dedupe_key),
                    origin="ingress",
                    actor_ref=agent_id,
                )
                if hold:
                    await asyncio.sleep(hold)
                await session.commit()
                return (row.id if row is not None else None), code

        try:
            first, second = await asyncio.gather(submit(0.4), submit(0.0))

            created = [r for r, _ in (first, second) if r is not None]
            refused = [c for _, c in (first, second) if c]

            assert len(created) == 1, (
                "two concurrent submissions of ONE governed candidate both "
                "created a proposal -- the admission lock is not holding, and "
                "the same work would be approved and dispatched twice"
            )
            assert refused == [CODE_DUPLICATE]

            async with sessionmaker() as session:
                rows = (
                    await session.execute(
                        sa.select(CCAgentProposal).where(
                            CCAgentProposal.tenant_id == tenant
                        )
                    )
                ).scalars().all()
            assert len(rows) == 1
            assert rows[0].dedupe_key == dedupe_key
        finally:
            async with sessionmaker() as session:
                await session.execute(
                    sa.delete(CCAgentProposal).where(
                        CCAgentProposal.tenant_id == tenant
                    )
                )
                await session.commit()
            await engine.dispose()

    async def test_a_different_tenant_is_not_serialized_by_it(self):
        """The lock is per tenant: one tenant must not block another."""
        engine, sessionmaker = await _engine()
        a, b = f"t-{uuid.uuid4().hex[:8]}", f"t-{uuid.uuid4().hex[:8]}"
        try:
            async with sessionmaker() as s1, sessionmaker() as s2:
                assert await lock_proposal_admission(s1, a) is True
                # Would block forever on the same key; must not on another.
                assert await asyncio.wait_for(
                    lock_proposal_admission(s2, b), timeout=5
                ) is True
        finally:
            await engine.dispose()


class TestIngressSerialization:
    """A24.11: the idempotency lock, proved the way the admission lock was.

    The failure being excluded was reproduced before it was fixed: with
    both callers completing the replay lookup before either inserted, the
    loser raised `UniqueViolationError` -- a 500 on precisely the retry an
    idempotency key exists to make safe.
    """

    async def test_a_second_caller_waits_until_the_first_transaction_ends(self):
        """Deterministic, for the reason the admission test records."""
        from harkeniq_cc.ingress_limits import lock_agent_ingress

        engine, sessionmaker = await _engine()
        tenant, agent = f"t-{uuid.uuid4().hex[:8]}", uuid.uuid4().hex[:32]
        acquired = asyncio.Event()
        order: list[str] = []

        async def holder():
            async with sessionmaker() as session:
                await lock_agent_ingress(session, tenant, agent)
                acquired.set()
                await asyncio.sleep(0.5)
                order.append("holder-committed")
                await session.commit()

        async def waiter():
            await acquired.wait()
            async with sessionmaker() as session:
                await lock_agent_ingress(session, tenant, agent)
                order.append("waiter-acquired")
                await session.commit()

        try:
            await asyncio.wait_for(
                asyncio.gather(holder(), waiter()), timeout=20)
            assert order == ["holder-committed", "waiter-acquired"], (
                "a second ingress request passed the lock while the first "
                "still held it: the replay lookup and its insert are not "
                "atomic, and the loser will raise an integrity error"
            )
        finally:
            await engine.dispose()

    async def test_a_different_agent_is_not_serialized_by_it(self):
        from harkeniq_cc.ingress_limits import lock_agent_ingress

        engine, sessionmaker = await _engine()
        tenant = f"t-{uuid.uuid4().hex[:8]}"
        a, b = uuid.uuid4().hex[:32], uuid.uuid4().hex[:32]
        try:
            async with sessionmaker() as s1, sessionmaker() as s2:
                assert await lock_agent_ingress(s1, tenant, a) is True
                assert await asyncio.wait_for(
                    lock_agent_ingress(s2, tenant, b), timeout=5) is True
        finally:
            await engine.dispose()

    async def test_the_ledger_is_safe_under_the_lock(self):
        """The end-to-end shape, complementing the ordering test above.

        NOT a guard, and verified as such: with the lock removed this
        still passed, because whether the loser's lookup lands before the
        winner's insert depends on I/O interleaving. It exercises the real
        sequence; the ordering test above is what actually fails when the
        lock does.
        """
        from harkeniq_cc.db.repos import AgentSubmissionRepo
        from harkeniq_cc.ingress_limits import lock_agent_ingress

        engine, sessionmaker = await _engine()
        tenant, agent = f"t-{uuid.uuid4().hex[:8]}", uuid.uuid4().hex[:32]
        key = "one-key-0001"
        gate = asyncio.Barrier(2)

        async def submit():
            async with sessionmaker() as session:
                await gate.wait()
                # The route's order: lock, THEN look up, then insert.
                await lock_agent_ingress(session, tenant, agent)
                repo = AgentSubmissionRepo(session)
                prior = await repo.find(tenant, agent, key)
                if prior is not None:
                    return "replayed"
                await repo.record(
                    tenant_id=tenant, agent_id=agent, agent_version=1,
                    idempotency_key=key, request_digest="d",
                    candidate_ref="c", proposal_id=None, code="", reason="")
                await session.commit()
                return "inserted"

        try:
            got = sorted(await asyncio.gather(submit(), submit()))
            assert got == ["inserted", "replayed"], got
        finally:
            await engine.dispose()


class TestAttemptRateIsAtomic:
    """A24.13: count and admit are one decision, or the limit is advisory."""

    async def test_concurrent_callers_cannot_both_spend_the_last_slot(self):
        """The scenario, not the guard.

        Measured with the lock removed: this still passed, for the same
        interleaving reason recorded above. The property it describes is
        guaranteed by `TestIngressSerialization`'s ordering test, which
        fails deterministically when the lock is gone. Kept because it
        exercises `admit_attempt` against a real window boundary.
        """
        from harkeniq_cc.db.repos import AgentIngressAttemptRepo
        from harkeniq_cc.ingress_limits import (
            ATTEMPT_MAX, admit_attempt, lock_agent_ingress,
        )

        engine, sessionmaker = await _engine()
        tenant, agent = f"t-{uuid.uuid4().hex[:8]}", uuid.uuid4().hex[:32]
        try:
            # Fill the window to exactly one below the limit.
            async with sessionmaker() as session:
                repo = AgentIngressAttemptRepo(session)
                for _ in range(ATTEMPT_MAX - 1):
                    await repo.record(
                        tenant_id=tenant, agent_id=agent, outcome="accepted")
                await session.commit()

            gate = asyncio.Barrier(2)

            async def attempt():
                async with sessionmaker() as session:
                    await gate.wait()
                    await lock_agent_ingress(session, tenant, agent)
                    permitted, _used = await admit_attempt(
                        session, tenant_id=tenant, agent_id=agent)
                    if permitted:
                        await AgentIngressAttemptRepo(session).record(
                            tenant_id=tenant, agent_id=agent,
                            outcome="accepted")
                    await session.commit()
                    return permitted

            results = await asyncio.gather(attempt(), attempt())
            assert sorted(results) == [False, True], (
                f"both callers spent the last slot ({results}): the rate "
                "decision is not atomic and two replicas would each permit "
                "the whole allowance"
            )
        finally:
            await engine.dispose()


class TestEvaluatorAndIngressConverge:
    """The internal evaluator and external ingress are ONE admission path."""

    async def test_they_produce_one_logical_proposal_for_one_operation(self):
        from harkeniq.capabilities import operation_key
        from harkeniq_cc.proposal_admission import admit_proposal

        engine, sessionmaker = await _engine()
        tenant = f"t-{uuid.uuid4().hex[:8]}"
        agent_a, agent_b = uuid.uuid4().hex[:32], uuid.uuid4().hex[:32]

        def payload(agent_id):
            # DIFFERENT agents, so different dedupe keys -- nothing the
            # per-agent rule can see. Same physical operation.
            return {
                "tenant_id": tenant, "agent_id": agent_id,
                "actor": f"op-agent:{agent_id}@v1", "agent_version": 1,
                "site_id": "site-1", "device_agent_id": "node-1",
                "action_type": "IDENTIFY_LED",
                "params": {"target": "Disk.Bay.1"},
                "rationale": "r", "evidence": {},
                "disposition": "requires_approval", "disposition_reason": "",
                "blocking_conditions": [],
                "authorization_basis": "human_approval",
                "status": "awaiting_approval", "decided_by": "",
                "decided_at": None,
                "dedupe_key": f"{agent_id}:node-1:IDENTIFY_LED:inc-1:Disk.Bay.1",
            }

        gate = asyncio.Barrier(2)

        async def admit(agent_id, origin, hold):
            async with sessionmaker() as session:
                await gate.wait()
                row, code, _ = await admit_proposal(
                    session, tenant_id=tenant, payload=payload(agent_id),
                    origin=origin, actor_ref=agent_id)
                if hold:
                    await asyncio.sleep(0.3)
                await session.commit()
                return (row.id if row is not None else None), code

        try:
            first, second = await asyncio.gather(
                admit(agent_a, "evaluator", 0.3),
                admit(agent_b, "ingress", 0.0),
            )
            created = [r for r, _ in (first, second) if r is not None]
            assert len(created) == 1, (
                "the evaluator and external ingress each created a proposal "
                "for ONE physical operation"
            )
            async with sessionmaker() as session:
                rows = (await session.execute(
                    sa.select(CCAgentProposal).where(
                        CCAgentProposal.tenant_id == tenant)
                )).scalars().all()
            assert len(rows) == 1
            assert rows[0].operation_key == operation_key(
                tenant, "node-1", "IDENTIFY_LED", {"target": "Disk.Bay.1"})
        finally:
            async with sessionmaker() as session:
                await session.execute(sa.delete(CCAgentProposal).where(
                    CCAgentProposal.tenant_id == tenant))
                await session.commit()
            await engine.dispose()


class TestTheCatalogueSeedsExactlyOnce:
    """The sixth finding: a lazy first-seed that raced.

    `CapabilityCatalogueRepo.list_for_tenant` seeded with
    read -> if-empty -> seed and no serialization, so two concurrent
    requests on a tenant whose catalogue was unseeded both saw nothing,
    both seeded, and one raised a unique violation. That is the 500 the
    very first A6 concurrency probe actually hit, before any of the
    findings were fixed.

    Proved by ORDER, not by racing two callers and hoping: a caller that
    finds the catalogue empty cannot finish its read until the seeding
    transaction commits, and must then see the committed rows rather than
    seed again.
    """

    async def test_a_second_reader_waits_and_then_sees_the_committed_seed(self):
        from harkeniq.audit.chain import pg_advisory_chain_lock
        from harkeniq_cc.capability_catalogue import SEED
        from harkeniq_cc.db.models import CCCapabilityCatalogue
        from harkeniq_cc.db.repos import CapabilityCatalogueRepo

        engine, sessionmaker = await _engine()
        tenant = f"t-{uuid.uuid4().hex[:8]}"
        seeding = asyncio.Event()
        order: list[str] = []
        failures: list[str] = []

        async def seeder():
            """The first request: holds the lock across its seed."""
            async with sessionmaker() as session:
                await pg_advisory_chain_lock(
                    session, f"cc.capability_catalogue.{tenant}")
                await CapabilityCatalogueRepo(session).seed(
                    tenant, actor="test-seeder")
                seeding.set()
                # Long enough that an UNSERIALIZED reader would certainly
                # have finished its own seed by now.
                await asyncio.sleep(0.5)
                order.append("seeder-committed")
                await session.commit()

        async def reader():
            """The second request, arriving mid-seed."""
            await seeding.wait()
            async with sessionmaker() as session:
                try:
                    rows = await CapabilityCatalogueRepo(
                        session).list_for_tenant(tenant)
                except Exception as exc:  # noqa: BLE001 - reported, not raised
                    failures.append(f"{type(exc).__name__}: {exc}")
                    return []
                order.append("reader-read")
                await session.commit()
                return rows

        try:
            _, rows = await asyncio.wait_for(
                asyncio.gather(seeder(), reader()), timeout=30)

            assert not failures, (
                f"the second reader raised instead of waiting: {failures}"
            )
            assert order == ["seeder-committed", "reader-read"], (
                "the second reader completed its read before the seeding "
                "transaction committed: it saw an empty catalogue and would "
                "seed a second copy"
            )

            async with sessionmaker() as session:
                stored = (await session.execute(
                    sa.select(CCCapabilityCatalogue).where(
                        CCCapabilityCatalogue.tenant_id == tenant)
                )).scalars().all()

            # Exactly one seed: not doubled, not partial.
            assert len(stored) == len(SEED), (
                f"{len(stored)} catalogue rows for a {len(SEED)}-row seed -- "
                "the catalogue was seeded more than once or incompletely"
            )
            pairs = {(r.subsystem, r.action_type) for r in stored}
            assert len(pairs) == len(stored), "duplicate catalogue entries"
            # And the waiting reader saw the whole thing, not a fragment.
            assert len(rows) == len(SEED)
        finally:
            async with sessionmaker() as session:
                await session.execute(
                    sa.delete(CCCapabilityCatalogue).where(
                        CCCapabilityCatalogue.tenant_id == tenant))
                await session.commit()
            await engine.dispose()


class TestReadMeteringIsAtomic:
    """A25.6: the read counter must be correct across replicas.

    The submission limiter is serialized by an advisory lock. The read
    counter deliberately is NOT: it is a single atomic UPDATE, which is
    both cheaper and correct for traffic where a row per request would
    make the meter the largest writer in the system. That claim is only
    worth making if it is proved on a real engine, because sqlite is
    single-writer and would pass either way.
    """

    async def test_concurrent_reads_are_all_counted(self):
        from harkeniq_cc.db.models import CCAgentReadWindow
        from harkeniq_cc.ingress_limits import admit_read, read_window_start

        engine, sessionmaker = await _engine()
        tenant, agent = f"t-{uuid.uuid4().hex[:8]}", uuid.uuid4().hex[:32]
        window = read_window_start()
        gate = asyncio.Barrier(8)

        async def read_once():
            async with sessionmaker() as session:
                await gate.wait()
                permitted, used = await admit_read(
                    session, tenant_id=tenant, agent_id=agent)
                await session.commit()
                return permitted, used

        try:
            results = await asyncio.gather(*(read_once() for _ in range(8)))
            assert all(permitted for permitted, _ in results)

            async with sessionmaker() as session:
                total = (await session.execute(
                    sa.select(CCAgentReadWindow.reads).where(
                        CCAgentReadWindow.tenant_id == tenant,
                        CCAgentReadWindow.agent_id == agent,
                        CCAgentReadWindow.window_start == window,
                    )
                )).scalar()
            assert total == 8, (
                f"eight concurrent reads counted as {total}: the increment "
                "is losing updates, so the limit is advisory"
            )
            # And exactly one window row exists -- the unique constraint is
            # what makes the race to open it safe.
            async with sessionmaker() as session:
                rows = (await session.execute(
                    sa.select(CCAgentReadWindow).where(
                        CCAgentReadWindow.tenant_id == tenant)
                )).scalars().all()
            assert len(rows) == 1
        finally:
            async with sessionmaker() as session:
                await session.execute(sa.delete(CCAgentReadWindow).where(
                    CCAgentReadWindow.tenant_id == tenant))
                await session.commit()
            await engine.dispose()

    async def test_one_agent_does_not_consume_another_agents_window(self):
        from harkeniq_cc.db.models import CCAgentReadWindow
        from harkeniq_cc.ingress_limits import admit_read

        engine, sessionmaker = await _engine()
        tenant = f"t-{uuid.uuid4().hex[:8]}"
        a, b = uuid.uuid4().hex[:32], uuid.uuid4().hex[:32]
        try:
            async with sessionmaker() as session:
                for _ in range(5):
                    await admit_read(session, tenant_id=tenant, agent_id=a)
                _permitted, used_b = await admit_read(
                    session, tenant_id=tenant, agent_id=b)
                await session.commit()
            assert used_b == 1
        finally:
            async with sessionmaker() as session:
                await session.execute(sa.delete(CCAgentReadWindow).where(
                    CCAgentReadWindow.tenant_id == tenant))
                await session.commit()
            await engine.dispose()
