"""S5: automatic demotion made real at the Site Manager.

R3a ratified the A2.2 drop-back model and R3b-1 declared the
`sm_error_budgets` table — but nothing at runtime ever constructed a
KnowledgeBase, so the table had no writer, `is_action_type_dropped_back`
had no caller, and a class could fail repeatedly and keep its autonomy
on a running system. These tests pin the wiring that closes that:

  outcome reported -> error budget folded -> drop-back decided
  -> lease budget for that class becomes 0 -> agent must PROPOSE

"Propose", not "deny": the action is still the right one, it just no
longer runs without a human. Only a human ever restores it.
"""

from __future__ import annotations

import pytest

from harkeniq_sm.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_sm.db.repos import ErrorBudgetRepo
from harkeniq_sm.knowledge import MIN_OUTCOMES_TO_JUDGE, ErrorBudgetState


#: E0.2: budgets are keyed (site_id, action_type). Every persistence test
#: below seeds one site and works within it; the cross-SITE properties are
#: pinned separately in tests/unit/sm/test_e0_site_isolation.py.
SITE = "site-under-test"


async def _sessionmaker():
    from harkeniq_sm.db.models import Site

    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    sessionmaker = make_sessionmaker(engine)
    async with sessionmaker() as session:
        session.add(Site(id=SITE, name="site-under-test"))
        await session.commit()
    return sessionmaker


class TestTheDecision:
    """One model folds the outcome, whether in memory or in a row."""

    def test_no_judgement_below_the_evidence_bar(self):
        state = ErrorBudgetState(action_type="SEL_CLEAR")
        for _ in range(MIN_OUTCOMES_TO_JUDGE - 1):
            assert state.record("FAILURE") is False
        assert state.dropped_back is False

    def test_drops_back_once_the_evidence_is_there(self):
        state = ErrorBudgetState(action_type="SEL_CLEAR")
        newly = [state.record("FAILURE") for _ in range(MIN_OUTCOMES_TO_JUDGE)]
        assert state.dropped_back is True
        # Exactly one transition, however many failures follow.
        assert newly.count(True) == 1
        assert state.record("FAILURE") is False

    def test_a_healthy_class_never_drops_back(self):
        state = ErrorBudgetState(action_type="BMC_RESET")
        for _ in range(50):
            state.record("SUCCESS")
        assert state.dropped_back is False
        assert state.success_rate == 1.0

    def test_ninety_five_percent_is_the_line(self):
        state = ErrorBudgetState(action_type="SEL_CLEAR")
        for _ in range(96):
            state.record("SUCCESS")
        assert state.record("FAILURE") is False  # 96/97 is above 95%
        state2 = ErrorBudgetState(action_type="SEL_CLEAR")
        for _ in range(10):
            state2.record("SUCCESS")
        assert state2.record("FAILURE") is True  # 10/11 is below 95%

    def test_rollback_counts_as_a_failure(self):
        state = ErrorBudgetState(action_type="CONFIG_RESTORE")
        for _ in range(MIN_OUTCOMES_TO_JUDGE):
            state.record("ROLLBACK_TRIGGERED")
        assert state.dropped_back is True


class TestPersistence:
    @pytest.mark.asyncio
    async def test_the_row_is_written_and_survives(self):
        sessionmaker = await _sessionmaker()
        async with sessionmaker() as session:
            for _ in range(MIN_OUTCOMES_TO_JUDGE):
                await ErrorBudgetRepo(session).record(SITE, "SEL_CLEAR", "FAILURE")
            await session.commit()
        async with sessionmaker() as session:
            rows = await ErrorBudgetRepo(session).list_all()
            assert len(rows) == 1
            assert rows[0].action_type == "SEL_CLEAR"
            assert rows[0].total_count == MIN_OUTCOMES_TO_JUDGE
            assert rows[0].dropped_back is True
            assert rows[0].dropped_back_at is not None

    @pytest.mark.asyncio
    async def test_dropped_back_types_is_the_lease_input(self):
        sessionmaker = await _sessionmaker()
        async with sessionmaker() as session:
            repo = ErrorBudgetRepo(session)
            for _ in range(MIN_OUTCOMES_TO_JUDGE):
                await repo.record(SITE, "SEL_CLEAR", "FAILURE")
            for _ in range(MIN_OUTCOMES_TO_JUDGE):
                await repo.record(SITE, "BMC_RESET", "SUCCESS")
            await session.commit()
            assert await repo.dropped_back_types(SITE) == {"SEL_CLEAR"}

    @pytest.mark.asyncio
    async def test_recovery_is_an_explicit_human_act(self):
        sessionmaker = await _sessionmaker()
        async with sessionmaker() as session:
            repo = ErrorBudgetRepo(session)
            for _ in range(MIN_OUTCOMES_TO_JUDGE):
                await repo.record(SITE, "SEL_CLEAR", "FAILURE")
            await session.commit()
            assert await repo.recover(SITE, "SEL_CLEAR") is True
            await session.commit()
            row = (await repo.list_all())[0]
            assert row.dropped_back is False
            # Counters reset so the class is judged on a fresh period, not
            # on the failures the operator just reviewed.
            assert row.total_count == 0
            assert await repo.dropped_back_types(SITE) == set()
            # Recovering something that never dropped back is a no-op.
            assert await repo.recover(SITE, "SEL_CLEAR") is False
            assert await repo.recover(SITE, "NEVER_SEEN") is False

    @pytest.mark.asyncio
    async def test_case_is_normalised(self):
        sessionmaker = await _sessionmaker()
        async with sessionmaker() as session:
            repo = ErrorBudgetRepo(session)
            for _ in range(MIN_OUTCOMES_TO_JUDGE):
                await repo.record(SITE, "SEL_CLEAR", "failure")
            await session.commit()
            assert await repo.dropped_back_types(SITE) == {"SEL_CLEAR"}
