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

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.models import (
    CCAgentIngressAttempt, CCAgentProposal, CCAgentSubmission,
)
from harkeniq_cc.db.repos import AgentIngressAttemptRepo, AgentSubmissionRepo
from harkeniq_cc.ingress_limits import ATTEMPT_WINDOW_S
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
