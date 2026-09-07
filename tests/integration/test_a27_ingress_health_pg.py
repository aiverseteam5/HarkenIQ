"""A27 (A6-3) on real PostgreSQL: bounded aggregation and the migration.

The unit suite drives the same code on sqlite. This runs the parts that
are not engine-neutral in the ways this codebase has already paid for:

* **Window filtering is a timestamptz comparison.** sqlite has no native
  timezone-aware type and hands back naive datetimes; PostgreSQL hands
  back aware ones. `activity_state` and `counts_since` both compare
  against an aware `now`, and a naive/aware mix raises rather than
  answering wrongly -- which is why the projection normalises and why
  that normalisation has to be exercised on the engine that produces the
  other shape.
* **`GROUP BY` and `MAX` are executed by the database.** A27.9's whole
  claim is that these are aggregates, not row reads. Proving that on
  sqlite proves it for sqlite.
* **The migration is additive with NO backfill.** A27.4 is a promise
  about existing customer data, so the column is verified nullable on
  the real engine and a proposal written without provenance is proven to
  keep NULL. The rewind-and-upgrade-with-rows-present half runs against
  the LIVE database in the compose gate, following A23-2.

Gated on ``HARKEN_TEST_CC_PG_DSN``; skipped when unset.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.models import (
    CCAgentIngressAttempt, CCAgentProposal, CCAgentReadWindow,
    CCAgentSubmission, CCAgentThrottleWindow,
)
from harkeniq_cc.db.repos import AgentIngressAttemptRepo, AgentSubmissionRepo
from harkeniq_cc.ingress_limits import (
    ATTEMPT_WINDOW_S, record_throttled, throttle_window_start,
    throttling_observed,
)
from harkeniq_cc.provenance import REFUSAL_SAMPLE, activity_state

DSN = os.environ.get("HARKEN_TEST_CC_PG_DSN", "")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not DSN, reason="HARKEN_TEST_CC_PG_DSN not set"),
    pytest.mark.asyncio,
]


async def _engine():
    engine = make_engine(DSN)
    await create_all(engine)
    return engine


@pytest.mark.asyncio
async def test_attempt_aggregation_is_bounded_and_grouped_in_sql():
    """A27.9, on the engine that will actually run it."""
    engine = await _engine()
    sm = make_sessionmaker(engine)
    tenant = f"t-a27-{uuid.uuid4().hex[:8]}"
    agent = f"agent-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    inside = now - timedelta(seconds=60)
    outside = now - timedelta(seconds=ATTEMPT_WINDOW_S * 3)

    try:
        async with sm() as session:
            for outcome, when, n in (
                ("accepted", inside, 3),
                ("rejected", inside, 2),
                ("replayed", inside, 1),
                # Outside the window: must not be counted at all.
                ("accepted", outside, 9),
            ):
                for _ in range(n):
                    row = CCAgentIngressAttempt(
                        tenant_id=tenant, agent_id=agent, outcome=outcome,
                    )
                    row.created_at = when
                    session.add(row)
            await session.commit()

        async with sm() as session:
            repo = AgentIngressAttemptRepo(session)
            counts = await repo.counts_since(
                tenant, agent, now - timedelta(seconds=ATTEMPT_WINDOW_S),
            )
            last = await repo.last_attempt_at(tenant, agent)

        assert counts == {"accepted": 3, "rejected": 2, "replayed": 1}, counts
        assert sum(counts.values()) == 6, "an out-of-window attempt was counted"
        # `last_attempt_at` is deliberately NOT window-bounded: the honest
        # answer to "when did it last try" includes an old attempt.
        assert last is not None
    finally:
        async with sm() as session:
            await session.execute(sa.delete(CCAgentIngressAttempt).where(
                CCAgentIngressAttempt.tenant_id == tenant))
            await session.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_refusal_sample_never_scans_the_durable_ledger():
    """`cc_agent_submissions` is never pruned, so it is never read whole."""
    engine = await _engine()
    sm = make_sessionmaker(engine)
    tenant = f"t-a27-{uuid.uuid4().hex[:8]}"
    agent = f"agent-{uuid.uuid4().hex[:8]}"
    base = datetime.now(timezone.utc) - timedelta(days=1)

    try:
        async with sm() as session:
            for n in range(REFUSAL_SAMPLE * 5):
                row = CCAgentSubmission(
                    tenant_id=tenant, agent_id=agent, agent_version=1,
                    idempotency_key=f"k-{n}", request_digest="d",
                    candidate_ref="c", proposal_id=None,
                    code="candidate_not_current", reason="x" * 2000,
                )
                row.created_at = base + timedelta(seconds=n)
                session.add(row)
            accepted = CCAgentSubmission(
                tenant_id=tenant, agent_id=agent, agent_version=1,
                idempotency_key="k-ok", request_digest="d", candidate_ref="c",
                proposal_id="prop-1", code="", reason="",
            )
            accepted.created_at = base + timedelta(seconds=10_000)
            session.add(accepted)
            await session.commit()

        async with sm() as session:
            refusals, last_accepted = await AgentSubmissionRepo(
                session
            ).recent_refusals(tenant, agent, limit=REFUSAL_SAMPLE)

        assert len(refusals) == REFUSAL_SAMPLE
        # Newest first, deterministically.
        stamps = [r.created_at for r in refusals]
        assert stamps == sorted(stamps, reverse=True)
        # The accepted one is not a refusal, and IS the last acceptance.
        assert all(r.proposal_id is None for r in refusals)
        assert last_accepted is not None
    finally:
        async with sm() as session:
            await session.execute(sa.delete(CCAgentSubmission).where(
                CCAgentSubmission.tenant_id == tenant))
            await session.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_activity_state_agrees_across_engines():
    """The timestamps PostgreSQL returns must produce the same answer.

    A naive/aware mix would raise rather than answer wrongly, which is
    exactly the failure this pins: the projection normalises, and this
    proves the normalisation against the shape PostgreSQL produces.
    """
    engine = await _engine()
    sm = make_sessionmaker(engine)
    tenant = f"t-a27-{uuid.uuid4().hex[:8]}"
    agent = f"agent-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)

    try:
        async with sm() as session:
            row = CCAgentIngressAttempt(
                tenant_id=tenant, agent_id=agent, outcome="accepted",
            )
            row.created_at = now - timedelta(seconds=30)
            session.add(row)
            await session.commit()

        async with sm() as session:
            stored = await AgentIngressAttemptRepo(session).last_attempt_at(
                tenant, agent,
            )
        assert stored is not None
        state = activity_state(
            last_authenticated_at=None, last_attempt_at=stored,
            accepted=1, refused=0, throttled=0, now=now,
        )
        assert state == "active_recently", state
    finally:
        async with sm() as session:
            await session.execute(sa.delete(CCAgentIngressAttempt).where(
                CCAgentIngressAttempt.tenant_id == tenant))
            await session.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_0024_is_additive_and_backfills_nothing():
    """A27.4, on the engine where a NOT NULL would break an upgrade.

    Verified by writing a proposal with NO provenance and confirming the
    column stays NULL -- not by reading the migration and believing it.
    The 0023->0024 upgrade with rows already present is proven against
    the live database in the compose gate.
    """
    engine = await _engine()
    sm = make_sessionmaker(engine)
    tenant = f"t-a27-{uuid.uuid4().hex[:8]}"

    try:
        async with sm() as session:
            row = CCAgentProposal(
                tenant_id=tenant, agent_id="agent-x", actor="op-agent:x@v1",
                agent_version=1, site_id="s", device_agent_id="d",
                action_type="SEL_CLEAR", params={}, rationale="r", evidence={},
                disposition="requires_approval", disposition_reason="d",
                authorization_basis="human_approval",
                status="awaiting_approval", dedupe_key=f"k-{uuid.uuid4().hex}",
            )
            session.add(row)
            await session.commit()
            pid = row.id

        async with sm() as session:
            stored = (await session.execute(
                sa.select(CCAgentProposal.provenance_type).where(
                    CCAgentProposal.id == pid)
            )).scalar_one_or_none()
        assert stored is None, (
            "a proposal written without provenance acquired one: A27.4 "
            "forbids manufacturing historical certainty"
        )

        # And the column is genuinely nullable on the real engine, which
        # is where a NOT NULL would have surfaced as a broken upgrade.
        async with engine.connect() as conn:
            cols = await conn.run_sync(
                lambda sync_conn: sa.inspect(sync_conn).get_columns(
                    "cc_agent_proposals"
                )
            )
        nullable = [c["nullable"] for c in cols if c["name"] == "provenance_type"]
        assert nullable == [True], nullable
    finally:
        async with sm() as session:
            await session.execute(sa.delete(CCAgentProposal).where(
                CCAgentProposal.tenant_id == tenant))
            await session.commit()
        await engine.dispose()


# ---------------------------------------------------------------------------
# A27.13: the throttle counter, where connections actually race
# ---------------------------------------------------------------------------
#
# sqlite `:memory:` is a StaticPool -- one shared connection -- so the
# unit suite can assert the arithmetic and nothing about contention. This
# is the engine where two writers genuinely collide, which is the only
# place the savepoint-and-unique-constraint design can be judged.


@pytest.mark.asyncio
async def test_concurrent_rejections_neither_lose_nor_corrupt_the_count():
    """Many writers, one bucket, exact total.

    Every task opens its own session on its own connection and records a
    rejection into the SAME aligned window. One of them wins the insert
    race; the rest must fall through to the update rather than raise, and
    none may be dropped.
    """
    engine = await _engine()
    sm = make_sessionmaker(engine)
    tenant = f"t-a27-{uuid.uuid4().hex[:8]}"
    agent = f"agent-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    writers = 40

    async def one():
        async with sm() as session:
            await record_throttled(
                session, tenant_id=tenant, agent_id=agent, now=now,
            )
            await session.commit()

    try:
        await asyncio.gather(*(one() for _ in range(writers)))

        async with sm() as session:
            rows = list((await session.execute(
                sa.select(CCAgentThrottleWindow).where(
                    CCAgentThrottleWindow.tenant_id == tenant)
            )).scalars().all())
            total, last_at = await throttling_observed(
                session, tenant_id=tenant, agent_id=agent, now=now,
            )

        assert len(rows) == 1, (
            f"{writers} concurrent rejections opened {len(rows)} buckets: the "
            "unique constraint did not serialize the open race"
        )
        assert rows[0].rejected == writers, (
            f"{writers} rejections totalled {rows[0].rejected}: increments "
            "were lost under contention"
        )
        assert total == writers
        assert last_at is not None
    finally:
        async with sm() as session:
            await session.execute(sa.delete(CCAgentThrottleWindow).where(
                CCAgentThrottleWindow.tenant_id == tenant))
            await session.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_flood_across_minutes_is_bounded_by_time_not_traffic():
    """The storage claim, on the real engine.

    500 rejections spread over five aligned minutes must produce five
    rows -- the bound that lets A24.13's no-amplification rule survive
    while the refusal is still observed.
    """
    engine = await _engine()
    sm = make_sessionmaker(engine)
    tenant = f"t-a27-{uuid.uuid4().hex[:8]}"
    agent = f"agent-{uuid.uuid4().hex[:8]}"
    base = datetime.now(timezone.utc)

    try:
        async with sm() as session:
            for minute in range(5):
                at = base - timedelta(seconds=60 * minute)
                for _ in range(100):
                    await record_throttled(
                        session, tenant_id=tenant, agent_id=agent, now=at,
                    )
            await session.commit()

        async with sm() as session:
            rows = list((await session.execute(
                sa.select(CCAgentThrottleWindow).where(
                    CCAgentThrottleWindow.tenant_id == tenant)
            )).scalars().all())
            total, _ = await throttling_observed(
                session, tenant_id=tenant, agent_id=agent, now=base,
            )

        assert len(rows) == 5, f"500 rejections made {len(rows)} rows"
        assert sum(r.rejected for r in rows) == 500, "a rejection was lost"
        assert total == 500
        # And every bucket is aligned, so two replicas writing the same
        # second cannot disagree about which bucket it is.
        for row in rows:
            assert row.window_start == throttle_window_start(row.window_start)
    finally:
        async with sm() as session:
            await session.execute(sa.delete(CCAgentThrottleWindow).where(
                CCAgentThrottleWindow.tenant_id == tenant))
            await session.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_window_filter_is_a_timestamptz_comparison():
    """A rejection outside the reported window is not counted inside it.

    The filter runs in SQL against a `timestamptz`, which is the shape
    sqlite cannot produce -- the same class of divergence `_aware` exists
    for on the Python side.
    """
    engine = await _engine()
    sm = make_sessionmaker(engine)
    tenant = f"t-a27-{uuid.uuid4().hex[:8]}"
    agent = f"agent-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)

    try:
        async with sm() as session:
            await record_throttled(
                session, tenant_id=tenant, agent_id=agent,
                now=now - timedelta(seconds=ATTEMPT_WINDOW_S // 2),
            )
            await record_throttled(
                session, tenant_id=tenant, agent_id=agent,
                now=now - timedelta(seconds=ATTEMPT_WINDOW_S * 4),
            )
            await session.commit()

        async with sm() as session:
            total, last_at = await throttling_observed(
                session, tenant_id=tenant, agent_id=agent, now=now,
            )
        assert total == 1, "a rejection outside the window was counted"
        assert last_at is not None and last_at.tzinfo is not None, (
            "PostgreSQL returned a naive timestamp for a tz-aware column"
        )
    finally:
        async with sm() as session:
            await session.execute(sa.delete(CCAgentThrottleWindow).where(
                CCAgentThrottleWindow.tenant_id == tenant))
            await session.commit()
        await engine.dispose()


# ---------------------------------------------------------------------------
# A29.16 (A6-4A remediation): refusal evidence, where connections race
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_surface_refusals_are_exact_and_bounded():
    """Many writers, one window row, exact totals per closed reason.

    sqlite `:memory:` is a StaticPool -- one shared connection -- so the
    unit suite asserts arithmetic and nothing about contention. This is
    the engine where two writers genuinely collide, and the only place the
    single-statement UPDATE can be judged.
    """
    from harkeniq_cc.db.repos import AgentReadWindowRepo
    from harkeniq_cc.ingress_limits import read_window_start

    engine = await _engine()
    sm = make_sessionmaker(engine)
    tenant = f"t-a29-{uuid.uuid4().hex[:8]}"
    agent = f"agent-{uuid.uuid4().hex[:8]}"
    other = f"agent-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    window = read_window_start(now)
    writers = 40

    async def refuse(agent_id, reason):
        async with sm() as session:
            await AgentReadWindowRepo(session).record_refusal(
                tenant_id=tenant, agent_id=agent_id, window_start=window,
                reason=reason, at=datetime.now(timezone.utc),
            )
            await session.commit()

    try:
        # The row must exist first: `record_refusal` deliberately does NOT
        # create it, because accounting may never be what grows storage.
        for who in (agent, other):
            async with sm() as session:
                await AgentReadWindowRepo(session).increment(
                    tenant_id=tenant, agent_id=who, window_start=window,
                )
                await session.commit()

        await asyncio.gather(*[
            refuse(agent, "surface_not_allowed" if i % 2 == 0
                   else "machine_job_not_bound")
            for i in range(writers)
        ])

        async with sm() as session:
            rows = list((await session.execute(
                sa.select(CCAgentReadWindow).where(
                    CCAgentReadWindow.tenant_id == tenant)
            )).scalars().all())

        mine = [r for r in rows if r.agent_id == agent]
        theirs = [r for r in rows if r.agent_id == other]

        assert len(mine) == 1, (
            f"{writers} concurrent refusals produced {len(mine)} rows for one "
            "agent: storage followed request volume"
        )
        assert mine[0].surface_refused == writers, (
            f"{writers} refusals totalled {mine[0].surface_refused}: "
            "increments were lost under contention"
        )
        assert mine[0].refused_surface_not_allowed == writers // 2
        assert mine[0].refused_job_not_bound == writers // 2
        assert mine[0].last_surface_refused_at is not None
        assert mine[0].last_surface_refused_at.tzinfo is not None

        # ATTRIBUTION: the other agent's window is untouched.
        assert len(theirs) == 1 and theirs[0].surface_refused == 0, (
            "one agent's refusals were charged to another"
        )
    finally:
        async with sm() as session:
            await session.execute(sa.delete(CCAgentReadWindow).where(
                CCAgentReadWindow.tenant_id == tenant))
            await session.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_refusal_evidence_never_creates_a_row_of_its_own():
    """A29.16's storage bound, on the real engine.

    With no window open, a refusal records nothing rather than minting a
    row. The alternative -- create-on-refusal -- would let unauthenticated
    -shaped traffic grow the table it is meant to be bounded by.
    """
    from harkeniq_cc.db.repos import AgentReadWindowRepo
    from harkeniq_cc.ingress_limits import read_window_start

    engine = await _engine()
    sm = make_sessionmaker(engine)
    tenant = f"t-a29-{uuid.uuid4().hex[:8]}"
    agent = f"agent-{uuid.uuid4().hex[:8]}"

    try:
        async with sm() as session:
            for _ in range(50):
                await AgentReadWindowRepo(session).record_refusal(
                    tenant_id=tenant, agent_id=agent,
                    window_start=read_window_start(),
                    reason="surface_not_allowed",
                    at=datetime.now(timezone.utc),
                )
            await session.commit()
        async with sm() as session:
            rows = (await session.execute(
                sa.select(sa.func.count()).select_from(CCAgentReadWindow)
                .where(CCAgentReadWindow.tenant_id == tenant)
            )).scalar()
        assert rows == 0, f"refusal accounting minted {rows} row(s)"
    finally:
        async with sm() as session:
            await session.execute(sa.delete(CCAgentReadWindow).where(
                CCAgentReadWindow.tenant_id == tenant))
            await session.commit()
        await engine.dispose()
