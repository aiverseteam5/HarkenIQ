"""A25.1: an outcome settles the proposal it actually belongs to.

The defect this module exists to pin was not that settlement was missing
-- it worked, and the whole propose→approve→dispatch→settle journey was
green. It was that settlement joined on the WRONG THING. Central Command
has held an exact execution key since A1 (`cc_outcome_history.action_id`,
written by the Site Manager as `directive:<directive_id>`) and the
projection the settle loop consumed dropped it, so proposals were matched
by device, action class, actor and a time window.

The headline test below is the one the heuristic could never pass: two
proposals, one device, one action class, one actor, dispatched moments
apart, each with its own outcome. Under proximity matching the first
outcome in the list settles the first proposal whatever it describes. A
human reading a dashboard might notice a wrong row; a machine consumer
closing a transaction cannot.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from harkeniq_cc import agent_runtime
from harkeniq_cc.app import create_app
from harkeniq_cc.auth import configure_auth
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.models import (
    CCAgentProposal, CCFleetCache, CCOutcomeHistory, CCSite,
)
from harkeniq_cc.runtime import AppState

TENANT = "t1"
AGENT = "agent-one"
ACTOR = f"op-agent:{AGENT}@v1"


async def _stack():
    config = CCConfig(tenant_id=TENANT, insecure=True)
    configure_auth("", "", "", insecure=True)
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    state = AppState(
        config=config, engine=engine, sessionmaker=make_sessionmaker(engine),
    )
    create_app(state)          # registers the A6-2 counters
    async with state.sessionmaker() as session:
        site = CCSite(tenant_id=TENANT, site_name="DC-1",
                      sm_endpoint="sm:50051", sm_token="tok")
        session.add(site)
        await session.flush()
        session.add(CCFleetCache(
            site_id=site.id, agent_id="node-1", agent_name="n1",
            vendor="Dell", model="R750", observation="observed",
        ))
        await session.commit()
        return state, site.id


async def _dispatched(state, site_id, *, directive_id, key, at):
    async with state.sessionmaker() as session:
        row = CCAgentProposal(
            tenant_id=TENANT, agent_id=AGENT, actor=ACTOR, agent_version=1,
            site_id=site_id, device_agent_id="node-1",
            action_type="SEL_CLEAR", params={}, rationale="r", evidence={},
            disposition="requires_approval",
            authorization_basis="human_approval", status="dispatched",
            dedupe_key=key, directive_id=directive_id, dispatched_at=at,
        )
        session.add(row)
        await session.commit()
        return row.id


async def _outcome(state, site_id, *, action_id, outcome, at):
    async with state.sessionmaker() as session:
        session.add(CCOutcomeHistory(
            site_id=site_id, action_id=action_id, action_type="SEL_CLEAR",
            device_agent_id="node-1", outcome=outcome, actor=ACTOR,
            ingested_at=at,
        ))
        await session.commit()


async def _status(state, proposal_id):
    async with state.sessionmaker() as session:
        row = await session.get(CCAgentProposal, proposal_id)
        return row.status, row.outcome


class TestTwoProposalsCannotCrossSettle:
    """The test the heuristic could not pass."""

    async def test_each_proposal_takes_its_own_outcome(self):
        state, site_id = await _stack()
        t0 = datetime.now(timezone.utc)

        first = await _dispatched(
            state, site_id, directive_id="dir-A", key="k1", at=t0)
        second = await _dispatched(
            state, site_id, directive_id="dir-B", key="k2",
            at=t0 + timedelta(seconds=1))

        # The SECOND proposal's execution failed; the first succeeded.
        # Under proximity matching the earliest candidate settles the
        # earliest proposal, so the failure would land on `first`.
        await _outcome(state, site_id, action_id="directive:dir-B",
                       outcome="FAILURE", at=t0 + timedelta(seconds=2))
        await _outcome(state, site_id, action_id="directive:dir-A",
                       outcome="SUCCESS", at=t0 + timedelta(seconds=3))

        assert await agent_runtime.settle_outcomes(state, TENANT) == 2
        assert await _status(state, first) == ("completed", "SUCCESS")
        assert await _status(state, second) == ("failed", "FAILURE")

    async def test_a_proposal_waits_rather_than_borrowing_an_outcome(self):
        """Unsettled and visible beats settled and wrong."""
        state, site_id = await _stack()
        t0 = datetime.now(timezone.utc)
        mine = await _dispatched(
            state, site_id, directive_id="dir-MINE", key="k1", at=t0)
        # Somebody else's execution, same device, same class, same actor.
        await _outcome(state, site_id, action_id="directive:dir-OTHER",
                       outcome="SUCCESS", at=t0 + timedelta(seconds=1))

        assert await agent_runtime.settle_outcomes(state, TENANT) == 0
        assert await _status(state, mine) == ("dispatched", "")

    async def test_the_right_outcome_settles_it_when_it_arrives(self):
        state, site_id = await _stack()
        t0 = datetime.now(timezone.utc)
        mine = await _dispatched(
            state, site_id, directive_id="dir-MINE", key="k1", at=t0)
        await _outcome(state, site_id, action_id="directive:dir-OTHER",
                       outcome="SUCCESS", at=t0 + timedelta(seconds=1))
        assert await agent_runtime.settle_outcomes(state, TENANT) == 0
        await _outcome(state, site_id, action_id="directive:dir-MINE",
                       outcome="ROLLBACK", at=t0 + timedelta(seconds=2))
        assert await agent_runtime.settle_outcomes(state, TENANT) == 1
        assert await _status(state, mine) == ("failed", "ROLLBACK")

    @pytest.mark.parametrize("outcome,status", [
        ("SUCCESS", "completed"),
        ("PARTIAL", "failed"),
        ("FAILURE", "failed"),
        ("ROLLBACK", "failed"),
        ("UNKNOWN", "failed"),
    ])
    async def test_the_canonical_outcome_survives_the_status_collapse(
        self, outcome, status
    ):
        """`settle` maps five outcomes onto two statuses.

        That collapse is existing behaviour and is not changed here -- but
        A25.4 forbids letting it be the only thing an external caller
        sees, so the canonical classification must still be on the row.
        """
        state, site_id = await _stack()
        t0 = datetime.now(timezone.utc)
        pid = await _dispatched(
            state, site_id, directive_id="dir-X", key="k1", at=t0)
        await _outcome(state, site_id, action_id="directive:dir-X",
                       outcome=outcome, at=t0 + timedelta(seconds=1))
        await agent_runtime.settle_outcomes(state, TENANT)
        assert await _status(state, pid) == (status, outcome)


class TestTheLegacyFallbackIsBounded:
    """A25.1: the heuristic survives only where no key could exist."""

    async def test_a_proposal_without_a_directive_id_still_settles(self):
        """The one case the fallback serves, and it is a fact about the
        row rather than a guess about its age."""
        state, site_id = await _stack()
        t0 = datetime.now(timezone.utc)
        pid = await _dispatched(
            state, site_id, directive_id="", key="k1", at=t0)
        await _outcome(state, site_id, action_id="legacy-action-1",
                       outcome="SUCCESS", at=t0 + timedelta(seconds=1))
        assert await agent_runtime.settle_outcomes(state, TENANT) == 1
        assert await _status(state, pid) == ("completed", "SUCCESS")

    async def test_the_fallback_still_respects_attribution(self):
        state, site_id = await _stack()
        t0 = datetime.now(timezone.utc)
        pid = await _dispatched(
            state, site_id, directive_id="", key="k1", at=t0)
        async with state.sessionmaker() as session:
            session.add(CCOutcomeHistory(
                site_id=site_id, action_id="legacy-2", action_type="SEL_CLEAR",
                device_agent_id="node-1", outcome="SUCCESS",
                actor="op-agent:somebody-else@v1",
                ingested_at=t0 + timedelta(seconds=1),
            ))
            await session.commit()
        assert await agent_runtime.settle_outcomes(state, TENANT) == 0
        assert await _status(state, pid) == ("dispatched", "")

    async def test_a_keyed_proposal_never_uses_the_fallback(self):
        """A directive id means exact-or-nothing, whatever else is nearby."""
        state, site_id = await _stack()
        t0 = datetime.now(timezone.utc)
        pid = await _dispatched(
            state, site_id, directive_id="dir-K", key="k1", at=t0)
        await _outcome(state, site_id, action_id="some-unrelated-action",
                       outcome="SUCCESS", at=t0 + timedelta(seconds=1))
        assert await agent_runtime.settle_outcomes(state, TENANT) == 0

    def test_the_prefix_is_declared_once(self):
        """The producer and the consumer must name one string."""
        import inspect

        from harkeniq_sm import outcomes as sm_outcomes

        assert agent_runtime.OUTCOME_ACTION_PREFIX == "directive:"
        # The Site Manager's directive path writes exactly this shape.
        from harkeniq_sm import directives

        assert 'f"directive:{directive_id}"' in inspect.getsource(directives)
        assert sm_outcomes.record_action_outcome is not None


class TestCorrelationIsObservable:
    """A25.1: the fallback is retired by measurement, so it is counted."""

    async def test_an_exact_settlement_is_recorded_as_exact(self):
        state, site_id = await _stack()
        t0 = datetime.now(timezone.utc)
        pid = await _dispatched(
            state, site_id, directive_id="dir-A", key="k1", at=t0)
        await _outcome(state, site_id, action_id="directive:dir-A",
                       outcome="SUCCESS", at=t0 + timedelta(seconds=1))
        await agent_runtime.settle_outcomes(state, TENANT)
        async with state.sessionmaker() as session:
            import sqlalchemy as sa
            from harkeniq_cc.db.models import CCAuditLog
            row = (await session.execute(sa.select(CCAuditLog).where(
                CCAuditLog.action == "agent_proposal.settled"
            ))).scalars().first()
        assert row is not None and row.detail["correlation"] == "exact"
        assert pid  # the settled proposal

    async def test_a_legacy_settlement_is_recorded_as_legacy(self):
        state, site_id = await _stack()
        t0 = datetime.now(timezone.utc)
        await _dispatched(state, site_id, directive_id="", key="k1", at=t0)
        await _outcome(state, site_id, action_id="legacy-1",
                       outcome="SUCCESS", at=t0 + timedelta(seconds=1))
        await agent_runtime.settle_outcomes(state, TENANT)
        async with state.sessionmaker() as session:
            import sqlalchemy as sa
            from harkeniq_cc.db.models import CCAuditLog
            row = (await session.execute(sa.select(CCAuditLog).where(
                CCAuditLog.action == "agent_proposal.settled"
            ))).scalars().first()
        assert row is not None and row.detail["correlation"] == "legacy"

    def test_a_caller_cannot_put_an_identifier_into_a_metric_name(self):
        """`/metrics` is unauthenticated: it may not say who a customer is.

        The risk is not the word "tenant" in a metric NAME -- 
        `cross_tenant_attempts_total` is a legitimate series. It is that
        these recorders build a name by interpolation, so an unbounded
        reason would be unbounded cardinality and could carry a tenant,
        agent or site identifier into a public scrape.
        """
        from harkeniq_cc import metrics as m

        seen: list[str] = []
        saved = m._registry

        class Recording:
            def inc(self, name, value=1.0):
                seen.append(name)

        try:
            m._registry = Recording()
            m.record_read_refusal("tenant-acme-42")     # hostile
            m.record_read_refusal("cross_agent")        # known
            m.record_correlation("agent-abc123")        # hostile
            m.record_correlation("exact")               # known
        finally:
            m._registry = saved

        for name in seen:
            assert "acme" not in name and "abc123" not in name, name
        assert f"{m.M_READ_REFUSED}_other" in seen, (
            "an unknown reason must fold into a bounded bucket, not vanish"
        )
        assert f"{m.M_CORRELATION}_exact" in seen

    def test_telemetry_never_raises_into_a_caller(self):
        """A settlement must not fail because a counter is missing."""
        from harkeniq_cc import metrics as m

        saved = m._registry
        try:
            class Exploding:
                def inc(self, *a, **k):
                    raise RuntimeError("registry is broken")

            m._registry = Exploding()
            m.record_correlation("exact")      # must not raise
            m.record_read_refusal("cross_agent")
            m.record_status_read(narrowed=True)
        finally:
            m._registry = saved

    async def test_the_outcome_projection_carries_the_key(self):
        """The one-line regression: the projection must not drop it again."""
        from harkeniq_cc.db.repos import OutcomeHistoryRepo

        state, site_id = await _stack()
        await _outcome(state, site_id, action_id="directive:dir-Z",
                       outcome="SUCCESS", at=datetime.now(timezone.utc))
        async with state.sessionmaker() as session:
            rows = await OutcomeHistoryRepo(session).list_outcome_dicts(TENANT)
        assert rows and rows[0]["action_id"] == "directive:dir-Z", (
            "list_outcome_dicts dropped action_id -- settlement falls back "
            "to matching on device, action class and a time window"
        )
