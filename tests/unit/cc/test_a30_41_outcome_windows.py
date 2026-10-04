"""A30.41: outcome window correctness.

Every outcome-history read is exact over the rows its caller may read. Two
windows went: `list_outcome_dicts` kept the OLDEST 10,000 rows by
`ingested_at` and fed settlement, the autonomy contract, new proposals'
frozen evidence, S3-E2's viewer, outcome metrics and the learning engine;
`list_device_outcome_dicts` kept the OLDEST 50,000 by `recorded_at` for the
predictive route and human and machine Attention. Settlement also read only
the oldest 500 dispatched proposals. The internal decision paths keep their
50,000-row window, byte-identical, until Phase 3 owns evaluator ordering.

What this module proves, from both sides of each boundary:
  * below the windows nothing moved -- 519 reads byte-identical to a golden
    recorded from the unmodified code;
  * at 9,999 / 10,000 / 10,001 and 49,999 / 50,000 / 50,001 rows every
    consumer is exact, and the NEWEST row -- the one the windows dropped --
    decides what it should;
  * hidden rows never decide which visible rows count, at any size;
  * equal timestamps, late arrivals, backfills and moved devices;
  * every reader's count is the owner rule's, zero for zero reach;
  * settlement reaches past 500 stuck proposals; the receipt and settlement
    name one outcome; a stored proposal is never rewritten;
  * structure: no principal outcome read has a LIMIT, every ordered read
    ends in `id`, and the evaluator's window is the internal paths' alone.
"""

from __future__ import annotations

import ast
import inspect
import json
import pathlib
import random
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, func, insert, select

import harkeniq_cc
from harkeniq_cc import agent_runtime
from harkeniq_cc.app import create_app
from harkeniq_cc.auth import configure_auth
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.models import (
    CCAgentProposal,
    CCFleetCache,
    CCOutcomeHistory,
    CCSite,
)
from harkeniq_cc.db.outcome_sql import (
    SQLITE_DECAY_FUNCTION,
    _sqlite_decay_weight,
    decay_weight as decay_weight_sql,
)
from harkeniq_cc.db.repos import OutcomeHistoryRepo
from harkeniq_cc.intelligence import IntelligenceEngine
from harkeniq_cc.outcome_aggregator import OutcomeAggregator
from harkeniq_cc.predictive import (
    DECAY_HALF_LIFE_DAYS,
    decay_weight,
    weighted_failure_rate,
)
from harkeniq_cc.runtime import AppState

from tests.unit.cc import a30_40_estate as P
from tests.unit.cc import a30_41_golden as G
from tests.unit.cc import b2_2_estate as E
from tests.unit.cc import s3e2_estate as X

SRC = pathlib.Path(harkeniq_cc.__file__).parent
GOLDEN = json.loads((pathlib.Path(__file__).parent / G.GOLDEN_PATH).read_text())
NOW = datetime.now(timezone.utc)
#: Rows OLDER than every estate row, by both clocks.
T_OLD = datetime(2025, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _bulk(stack, *, site: str, device: str, n: int, outcome: str = "SUCCESS",
                action: str = "SEL_CLEAR", recorded: datetime = T_OLD,
                ingested: datetime = T_OLD, prefix: str = "w", key: str = "",
                vendor: str = "Dell", model: str = "R750") -> None:
    """`n` outcome rows, recorded a second apart from `recorded` (no ties)."""
    async with stack.sessionmaker() as session:
        for start in range(0, n, 10_000):
            await session.execute(insert(CCOutcomeHistory), [
                {"site_id": site, "action_id": key or f"{prefix}-{k}",
                 "action_type": action, "device_agent_id": device,
                 "vendor": vendor, "model": model, "outcome": outcome,
                 "fault_resolved": outcome == "SUCCESS", "actor": "a3041",
                 "recorded_at": recorded + timedelta(seconds=k), "ingested_at": ingested}
                for k in range(start, min(n, start + 10_000))
            ])
        await session.commit()


async def _count(stack, **where) -> int:
    """An independent count: raw SQL over the table, no repository."""
    stmt = select(func.count()).select_from(CCOutcomeHistory)
    for column, value in where.items():
        stmt = stmt.where(getattr(CCOutcomeHistory, column) == value)
    async with stack.sessionmaker() as session:
        return int((await session.execute(stmt)).scalar())


async def _as_owner(stack, path: str, **params) -> dict:
    stack.as_person()
    res = await stack.get(path, **params)
    assert res.status_code == 200, (path, res.status_code, res.text[:300])
    return res.json()


def _klass(contract: dict, action: str) -> dict:
    return next(c for c in contract["action_classes"] if c["action_type"] == action)


def _oracle_count(name: str, variant: str = "heavy") -> int:
    """The outcome rows the owner rule gives this A30.40 persona -- restated
    from the estate, never read through the code under test."""
    keep = P.visible(name, variant)
    base = sum(ok + bad for i, (*_r, ok, bad) in enumerate(E.OUTCOMES)
               if i in keep.base.outcomes)
    extra = sum(ok + bad for key, *_r, ok, bad in P.extra_outcomes(variant)
                if key in keep.outcomes)
    return base + extra


async def _rows_of(stack, device: str) -> list[dict]:
    async with stack.sessionmaker() as session:
        return [
            {"outcome": r.outcome, "recorded_at": r.recorded_at}
            for r in (await session.execute(
                select(CCOutcomeHistory)
                .where(CCOutcomeHistory.device_agent_id == device)
                .order_by(CCOutcomeHistory.recorded_at, CCOutcomeHistory.id)
            )).scalars()
        ]


def _risk(payload: dict, agent_id: str) -> dict:
    return next(r for r in payload["risks"] if r["agent_id"] == agent_id)


def _item(payload: dict, agent_id: str) -> dict:
    return next(i for i in payload["items"] if i["agent_id"] == agent_id)


async def _small_stack():
    """One tenant, one site, one device: a stack where the row count is
    exactly what a test seeds."""
    config = CCConfig(tenant_id="t1", insecure=True)
    configure_auth("", "", "", insecure=True)
    engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(engine)
    state = AppState(config=config, engine=engine, sessionmaker=make_sessionmaker(engine))
    create_app(state)
    async with state.sessionmaker() as session:
        site = CCSite(tenant_id="t1", site_name="DC-1", sm_endpoint="sm:1", sm_token="tok")
        session.add(site)
        await session.flush()
        session.add(CCFleetCache(site_id=site.id, agent_id="node-1", agent_name="n1",
                                 vendor="Dell", model="R750", observation="observed"))
        await session.commit()
        return state, site.id


async def _dispatched(state, site_id, *, directive_id, key, at, created=None,
                      dispatched_at=None, tenant="t1"):
    async with state.sessionmaker() as session:
        row = CCAgentProposal(
            tenant_id=tenant, agent_id="agent-one", actor="op-agent:agent-one@v1",
            agent_version=1, site_id=site_id, device_agent_id="node-1",
            action_type="SEL_CLEAR", params={}, rationale="r", evidence={},
            disposition="requires_approval", authorization_basis="human_approval",
            status="dispatched", dedupe_key=key, directive_id=directive_id,
            dispatched_at=dispatched_at or at,
        )
        if created is not None:
            row.created_at = created
        session.add(row)
        await session.commit()
        return row.id


async def _status(state, proposal_id):
    async with state.sessionmaker() as session:
        row = await session.get(CCAgentProposal, proposal_id)
        return row.status, row.outcome


# ---------------------------------------------------------------------------
# 1. Below both windows nothing moved: 519 reads, byte-identical
# ---------------------------------------------------------------------------


def _expected(prefix: str) -> dict:
    return {k: v for k, v in GOLDEN["digests"].items() if k.startswith(prefix)}


def _section_digests(name: str, value) -> dict:
    skeleton = {"humans_heavy": {}, "humans_light": {}, "machines": {"attention": {}, "internal": {}},
                "viewer": {}, "learning": None}
    skeleton[name] = value
    out = G.digests(skeleton)
    return {k: v for k, v in out.items() if k.startswith(name)}


class TestBelowTheWindowsNothingMoved:
    """Recorded by `a30_41_golden` with production code byte-identical to
    main 565154b, in two processes that agreed. Below 10,000 and 50,000 rows
    the windows cut nothing, so every answer must be unchanged."""

    def test_the_golden_was_recorded_from_main_and_covers_every_persona(self):
        assert GOLDEN["production_code_equals"] == G.MAIN
        assert len(GOLDEN["digests"]) == 519
        heavy = {k.split("/")[1] for k in GOLDEN["digests"] if k.startswith("humans_heavy/")}
        machines = {k.split("/")[2] for k in GOLDEN["digests"] if k.startswith("machines/attention/")}
        assert heavy == set(P.PERSONAS) and machines == set(E.MACHINES)

    async def test_every_human_read_heavy(self):
        assert _section_digests("humans_heavy", await G.humans("heavy")) == _expected("humans_heavy")

    async def test_every_human_read_light(self):
        assert _section_digests("humans_light", await G.humans("light")) == _expected("humans_light")

    async def test_every_machine_read_and_the_internal_paths(self):
        assert _section_digests("machines", await G.machines()) == _expected("machines")

    async def test_the_s3_e2_viewer_for_every_persona(self):
        viewer = await G.viewer()
        assert viewer == GOLDEN["full"]["viewer"]
        assert _section_digests("viewer", viewer) == _expected("viewer")

    async def test_the_learning_engine_over_four_cycles(self):
        learning = await G.learning()
        assert learning == GOLDEN["full"]["learning"]


# ---------------------------------------------------------------------------
# 2. The 10,000-row consumers at 9,999 / 10,000 / 10,001
# ---------------------------------------------------------------------------


class TestTheTenThousandBoundary:
    """The tenant holds exactly N rows. The NEWEST is one FAILURE: the row a
    10,000-row oldest-first window drops at 10,001, and the row settlement
    must find. Every consumer counts every row, on both sides."""

    @pytest.mark.parametrize("n", [9_999, 10_000, 10_001])
    async def test_every_consumer_is_exact_and_the_newest_row_counts(self, n):
        stack = await X.build()
        site, device = stack.site("A"), stack.tagged("node-s3-a")
        base = await _count(stack)
        await _bulk(stack, site=site, device=device, n=n - base - 1)
        await _bulk(stack, site=site, device=device, n=1, outcome="FAILURE",
                    key="directive:the-newest", recorded=NOW, ingested=NOW + timedelta(hours=1))
        assert await _count(stack) == n

        sel = await _count(stack, action_type="SEL_CLEAR")
        sel_failed = await _count(stack, action_type="SEL_CLEAR", outcome="FAILURE")

        # The tally itself.
        async with stack.sessionmaker() as session:
            tally = await OutcomeHistoryRepo(session).tally(stack.tenant)
        assert sum(r["count"] for r in tally) == n

        # The autonomy contract (its evidence says all_time, and is).
        evidence = _klass(await _as_owner(stack, "/api/autonomy/"), "SEL_CLEAR")["evidence"]
        assert (evidence["executions"], evidence["failure"], evidence["window"]) == \
            (sel, sel_failed, "all_time")

        # S3-E2's viewer for a tenant-wide reader.
        view = await X.evidence_view(stack, await X.persona(stack, "tenant"))
        viewed = view.viewer("SEL_CLEAR")["outcome_evidence"]
        assert (viewed["executions"], viewed["failure"]) == (sel, sel_failed)

        # Outcome metrics.
        metrics = await _as_owner(stack, "/api/outcomes/metrics")
        assert metrics["total_outcomes"] == n
        assert sum(m["failure_count"] for m in metrics["metrics"]
                   if m["action_type"] == "SEL_CLEAR") == sel_failed

        # The learning engine.
        engine = IntelligenceEngine()
        async with stack.sessionmaker() as session:
            await engine.run_cycle(session, stack.tenant)
            await session.commit()
        sel_metrics = engine.aggregator.get_metrics(action_type="SEL_CLEAR")
        assert sum(m.total_count for m in sel_metrics) == sel
        assert sum(m.failure_count for m in sel_metrics) == sel_failed

        # Settlement finds the newest outcome by its key.
        proposal = await _dispatched(stack.state, site, directive_id="the-newest",
                                     key=f"k-{n}", at=NOW - timedelta(minutes=1),
                                     tenant=stack.tenant)
        await agent_runtime.settle_outcomes(stack.state, stack.tenant)
        assert await _status(stack.state, proposal) == ("failed", "FAILURE")


# ---------------------------------------------------------------------------
# 3. The 50,000-row consumers at 49,999 / 50,000 / 50,001
# ---------------------------------------------------------------------------


class TestTheFiftyThousandBoundary:
    """One estate grows across the boundary. a1's history is 50,000 old
    successes and, at 50,001, ONE recent failure -- the newest row, the one
    an oldest-first window drops and the heaviest under recency weighting.
    The principal answer is exact at every size; the internal decision
    paths keep their window, which is pinned for Phase 3."""

    async def test_principals_are_exact_and_the_internal_window_stays(self):
        stack = await P.build()
        site, a1 = stack.site("A"), P.agent(stack, "a1")
        base = await _count(stack)
        await _bulk(stack, site=site, device=a1, n=49_999 - base,
                    recorded=E.T0 - timedelta(days=400))
        machine = await E.machine(stack, "m-tenant")
        seen = {}
        for n in (49_999, 50_000, 50_001):
            if n == 50_000:
                await _bulk(stack, site=site, device=a1, n=1, prefix="extra",
                            recorded=E.T0 - timedelta(days=399))
            if n == 50_001:
                await _bulk(stack, site=site, device=a1, n=1, outcome="FAILURE",
                            prefix="newest", recorded=E.T0 + timedelta(days=1))
            assert await _count(stack) == n
            predictive = await P.predictive(stack, "h-owner")
            assert predictive["outcomes_considered"] == n
            risk = _risk(predictive, a1)
            rows = await _rows_of(stack, a1)
            rate, _ = weighted_failure_rate(rows, now=datetime.now(timezone.utc))
            assert risk["sample_count"] == len(rows)
            assert risk["factors"]["weighted_failure_rate"] == round(rate, 4)
            # The person's and the machine's Attention agree with it.
            person = _item(await P.attention(stack, "h-owner"), a1)
            assert person["risk_score"] == risk["risk_score"]
            stack.as_machine(machine, permissions=E.MACHINES["m-tenant"][1])
            async with stack.client() as client:
                res = await client.get("/api/attention/")
            assert res.status_code == 200
            mine = next(i for i in res.json()["items"]
                        if i["target"]["device_agent_id"] == a1)
            assert mine["risk"] == {"basis": "device_history", "band": person["band"]}
            seen[n] = (risk["risk_score"], mine)
            stack.as_person()

        # The newest row moved the answer, as it should.
        assert seen[50_001][0] != seen[50_000][0]
        # The internal decision paths still read the OLDEST 50,000 rows:
        # at 50,001 they miss exactly the newest -- Phase 3 owns this.
        from harkeniq_cc.governance import load_attention

        async with stack.sessionmaker() as session:
            internal = await load_attention(session, tenant_id=stack.tenant, learning=None)
        assert _item(internal, a1)["risk_score"] == seen[50_000][0]


# ---------------------------------------------------------------------------
# 4. Hidden rows around the boundary: no side channel at any size
# ---------------------------------------------------------------------------


class TestHiddenRowsDecideNothing:
    async def test_the_autonomy_contract_cannot_be_crowded_out(self):
        """The F-E2-4 side channel. The contract's window ran over the WHOLE
        tenant before S3's site selection, so 10,001 hidden rows older than
        every site-A row filled it and a site-A reader's evidence went to
        nothing. Exact tallies: unchanged, while the tenant control moves."""
        stack = await X.build()
        subject = await X.persona(stack, "site_a")

        async def site_a() -> dict:
            stack.as_person(subject, "site_admin")
            res = await stack.get("/api/autonomy/")
            assert res.status_code == 200
            return _klass(res.json(), "SEL_CLEAR")["evidence"]

        before = await site_a()
        owner_before = _klass(await _as_owner(stack, "/api/autonomy/"), "SEL_CLEAR")["evidence"]
        await _bulk(stack, site=stack.site("C"), device=stack.tagged("node-s3-c"),
                    n=10_001, outcome="FAILURE", prefix="hidden")
        assert await site_a() == before
        owner_after = _klass(await _as_owner(stack, "/api/autonomy/"), "SEL_CLEAR")["evidence"]
        assert owner_after["executions"] == owner_before["executions"] + 10_001

    async def test_a_scoped_metrics_reader_is_unmoved_by_hidden_volume(self):
        stack = await P.build()
        before = await P.get(stack, "h-site-a", "/api/outcomes/metrics")
        await _bulk(stack, site=stack.site("C"), device=P.agent(stack, "c1"),
                    n=10_001, prefix="hidden")
        assert await P.get(stack, "h-site-a", "/api/outcomes/metrics") == before
        owner = await P.get(stack, "h-owner", "/api/outcomes/metrics")
        assert owner["total_outcomes"] == _oracle_count("h-owner") + 10_001


# ---------------------------------------------------------------------------
# 5. Every reader's count is the owner rule's
# ---------------------------------------------------------------------------


class TestEveryReaderCountsWhatTheOwnerRuleGives:
    """For every A30.40 persona -- tenant, org unit, site, device (incl. the
    moved device), device class, mixed grants, revoked, expired, lapsed, no
    grant, approve-only -- the metrics tally, the predictive statistics and
    an independent restatement of the owner rule agree."""

    @pytest.mark.parametrize("name", list(P.PERSONAS))
    async def test_tally_and_statistics_agree_with_the_oracle(self, name):
        stack = await P.build()
        metrics = await P.get(stack, name, "/api/outcomes/metrics")
        predictive = await P.predictive(stack, name)
        expected = _oracle_count(name)
        assert metrics["total_outcomes"] == predictive["outcomes_considered"] == expected
        if name in P.ZERO_REACH:
            assert expected == 0 and metrics["metrics"] == []


# ---------------------------------------------------------------------------
# 6. Equal timestamps, late arrival, backfill, determinism
# ---------------------------------------------------------------------------


class TestOrderIsTotal:
    async def test_equal_timestamps_settle_on_the_smallest_id_and_the_receipt_agrees(self):
        state, site_id = await _small_stack()
        at = datetime(2026, 9, 1, tzinfo=timezone.utc)
        async with state.sessionmaker() as session:
            # Stored FIRST, larger id; same ingested_at to the microsecond.
            for row_id, result in (("b-row", "FAILURE"), ("a-row", "SUCCESS")):
                session.add(CCOutcomeHistory(
                    id=row_id, site_id=site_id, action_id="directive:dup",
                    action_type="SEL_CLEAR", device_agent_id="node-1",
                    outcome=result, actor="op-agent:agent-one@v1", ingested_at=at))
                await session.flush()
            await session.commit()
        proposal = await _dispatched(state, site_id, directive_id="dup", key="k",
                                     at=at - timedelta(seconds=1))
        await agent_runtime.settle_outcomes(state, "t1")
        assert await _status(state, proposal) == ("completed", "SUCCESS")
        async with state.sessionmaker() as session:
            receipt = await OutcomeHistoryRepo(session).find_by_action_id("t1", "directive:dup")
        assert receipt.id == "a-row"

    async def test_equal_counts_keep_the_order_the_rows_first_appeared_in(self):
        """`get_metrics` breaks a tie in count by the order the aggregator met
        the keys, and the old scan met them in ingested_at order -- which is
        the order `/api/outcomes/metrics` lists and the detector walks. The
        tally hands its groups over by first appearance to keep it. Here
        the key seen FIRST sorts LAST by name, so a tally in key order would
        reverse the list."""
        state, site_id = await _small_stack()
        t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
        rows = ([("Zeta", "Z1", t0 + timedelta(seconds=k)) for k in range(3)]
                + [("Alpha", "A1", t0 + timedelta(minutes=5, seconds=k)) for k in range(3)])
        async with state.sessionmaker() as session:
            for n, (vendor, model, at) in enumerate(rows):
                session.add(CCOutcomeHistory(
                    site_id=site_id, action_id=f"tie-{n}", action_type="SEL_CLEAR",
                    device_agent_id="node-1", vendor=vendor, model=model,
                    outcome="SUCCESS", actor="x", ingested_at=at))
            await session.commit()
            tally = await OutcomeHistoryRepo(session).tally("t1")
        got, oracle = OutcomeAggregator(), OutcomeAggregator()
        got.ingest(tally)
        oracle.ingest([{"action_type": "SEL_CLEAR", "vendor": v, "model": m,
                        "outcome": "SUCCESS", "site_id": site_id}
                       for v, m, _at in sorted(rows, key=lambda r: r[2])])
        assert [m.vendor for m in got.get_metrics()] == \
            [m.vendor for m in oracle.get_metrics()] == ["Zeta", "Alpha"]

    async def test_tallies_and_statistics_rerun_identically_over_ties(self):
        state, site_id = await _small_stack()
        tie = datetime(2026, 9, 1, tzinfo=timezone.utc)
        async with state.sessionmaker() as session:
            await session.execute(insert(CCOutcomeHistory), [
                {"site_id": site_id, "action_id": f"t-{k}", "action_type": "SEL_CLEAR",
                 "device_agent_id": "node-1", "outcome": ("FAILURE" if k % 3 else "SUCCESS"),
                 "actor": "x", "recorded_at": tie, "ingested_at": tie}
                for k in range(300)
            ])
            await session.commit()
        async with state.sessionmaker() as session:
            repo = OutcomeHistoryRepo(session)
            runs = [(await repo.tally("t1"),
                     await repo.device_stats("t1", scope=None, now=NOW)) for _ in range(3)]
        assert runs[0] == runs[1] == runs[2]

    async def test_a_late_committed_row_is_counted_the_next_cycle(self):
        """The cursor skipped a row committed after a cycle with an EARLIER
        ingested_at, forever. A rebuilt tally counts it."""
        state, site_id = await _small_stack()
        early = datetime(2026, 9, 1, tzinfo=timezone.utc)
        async with state.sessionmaker() as session:
            for k in range(5):
                session.add(CCOutcomeHistory(
                    site_id=site_id, action_id=f"r-{k}", action_type="SEL_CLEAR",
                    device_agent_id="node-1", outcome="SUCCESS", actor="x",
                    ingested_at=early + timedelta(minutes=10 + k)))
            await session.commit()
        engine = IntelligenceEngine()
        async with state.sessionmaker() as session:
            await engine.run_cycle(session, "t1")
            await session.commit()
            first = engine.aggregator.get_metrics()[0].total_count
            # Late: ingested BEFORE everything the first cycle saw; and a
            # row sharing the first cycle's latest timestamp exactly.
            session.add(CCOutcomeHistory(
                site_id=site_id, action_id="late", action_type="SEL_CLEAR",
                device_agent_id="node-1", outcome="FAILURE", actor="x", ingested_at=early))
            session.add(CCOutcomeHistory(
                site_id=site_id, action_id="tie", action_type="SEL_CLEAR",
                device_agent_id="node-1", outcome="SUCCESS", actor="x",
                ingested_at=early + timedelta(minutes=14)))
            await session.commit()
            await engine.run_cycle(session, "t1")
            await session.commit()
        assert first == 5
        assert engine.aggregator.get_metrics()[0].total_count == 7
        assert engine.aggregator.get_metrics()[0].failure_count == 1

    async def test_a_backfilled_old_failure_counts_with_its_small_weight(self):
        stack = await P.build()
        a1 = P.agent(stack, "a1")
        before = _risk(await P.predictive(stack, "h-owner"), a1)
        await _bulk(stack, site=stack.site("A"), device=a1, n=1, outcome="FAILURE",
                    prefix="backfill", recorded=E.T0 - timedelta(days=200), ingested=NOW)
        after = _risk(await P.predictive(stack, "h-owner"), a1)
        rows = await _rows_of(stack, a1)
        rate, _ = weighted_failure_rate(rows, now=datetime.now(timezone.utc))
        assert after["sample_count"] == before["sample_count"] + 1
        assert after["factors"]["weighted_failure_rate"] == round(rate, 4)


# ---------------------------------------------------------------------------
# 7. Settlement reaches every dispatched proposal
# ---------------------------------------------------------------------------


class TestSettlementReachesEveryProposal:
    async def test_five_hundred_stuck_proposals_starve_nobody(self):
        """`list_by_status` returned the oldest 500; 500 that never hear
        back kept every newer proposal from settling. Keyset pages."""
        state, site_id = await _small_stack()
        t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
        async with state.sessionmaker() as session:
            for k in range(501):
                session.add(CCAgentProposal(
                    tenant_id="t1", agent_id="agent-one", actor="op-agent:agent-one@v1",
                    agent_version=1, site_id=site_id, device_agent_id="node-1",
                    action_type="SEL_CLEAR", params={}, rationale="r", evidence={},
                    disposition="requires_approval", authorization_basis="human_approval",
                    status="dispatched", dedupe_key=f"stuck-{k}", directive_id=f"stuck-{k}",
                    dispatched_at=t0, created_at=t0 + timedelta(seconds=k)))
            await session.commit()
        newest = await _dispatched(state, site_id, directive_id="live", key="live",
                                   at=t0, created=t0 + timedelta(days=1))
        async with state.sessionmaker() as session:
            session.add(CCOutcomeHistory(
                site_id=site_id, action_id="directive:live", action_type="SEL_CLEAR",
                device_agent_id="node-1", outcome="SUCCESS", actor="op-agent:agent-one@v1",
                ingested_at=t0 + timedelta(days=2)))
            await session.commit()
        assert await agent_runtime.settle_outcomes(state, "t1") == 1
        assert await _status(state, newest) == ("completed", "SUCCESS")

    async def test_a_keyless_proposal_takes_its_first_candidate_after_dispatch(self):
        state, site_id = await _small_stack()
        t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
        legacy = await _dispatched(state, site_id, directive_id="", key="legacy", at=t0)
        async with state.sessionmaker() as session:
            for key, result, at in (("before", "FAILURE", t0 - timedelta(seconds=5)),
                                    ("keyed", "FAILURE", t0 + timedelta(seconds=1)),
                                    ("after", "SUCCESS", t0 + timedelta(seconds=2))):
                session.add(CCOutcomeHistory(
                    site_id=site_id, action_id=("directive:x" if key == "keyed" else key),
                    action_type="SEL_CLEAR", device_agent_id="node-1", outcome=result,
                    actor="op-agent:agent-one@v1", ingested_at=at))
            await session.commit()
        await agent_runtime.settle_outcomes(state, "t1")
        assert await _status(state, legacy) == ("completed", "SUCCESS")


# ---------------------------------------------------------------------------
# 8. A stored proposal is never rewritten; a new one records exact evidence
# ---------------------------------------------------------------------------


class TestProposalTruth:
    async def test_stored_evidence_is_untouched_and_new_evidence_is_exact(self):
        stack, ids = await X.scenario()
        before = await X.stored_bytes(stack)
        stack.as_person()
        for path in ("/api/autonomy/", "/api/approvals/", "/api/outcomes/metrics",
                     f"/api/operational-agents/{ids['agent']}",
                     f"/api/operational-agents/{ids['agent']}/dry-run"):
            assert (await stack.get(path)).status_code == 200, path
        await _bulk(stack, site=stack.site("A"), device=stack.tagged("node-s3-a"), n=10_001)
        assert await X.stored_bytes(stack) == before
        # The dry-run's would-be creation record is the evaluator's own
        # composition: all-time and exact past 10,000 rows.
        dry = (await stack.get(f"/api/operational-agents/{ids['agent']}/dry-run")).json()
        sel = await _count(stack, action_type="SEL_CLEAR")
        evidences = [p["evidence"]["outcome_evidence"] for p in dry["would_propose"]
                     if p["action_type"] == "SEL_CLEAR" and p.get("evidence")]
        assert evidences and all(e["executions"] == sel for e in evidences)


# ---------------------------------------------------------------------------
# 9. Structure
# ---------------------------------------------------------------------------


class TestStructure:
    def _tree(self, rel: str):
        return ast.parse((SRC / rel).read_text())

    def test_the_removed_read_is_gone(self):
        offenders = [str(p.relative_to(SRC)) for p in SRC.rglob("*.py")
                     if "list_outcome_dicts" in p.read_text()]
        assert offenders == []

    def test_the_windowed_read_is_the_internal_paths_alone(self):
        from harkeniq_cc.db.repos import OutcomeHistoryRepo as Repo

        params = inspect.signature(Repo.list_device_outcome_dicts).parameters
        assert "scope" not in params
        callers = set()
        for path in SRC.rglob("*.py"):
            for fn in ast.walk(ast.parse(path.read_text())):
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for node in ast.walk(fn):
                        if isinstance(node, ast.Call) and getattr(
                                node.func, "attr", "") == "list_device_outcome_dicts":
                            callers.add((str(path.relative_to(SRC)), fn.name))
        assert callers == {("governance.py", "_compose_attention")}

    def test_every_outcome_read_selects_through_the_one_builder(self):
        repo = next(n for n in self._tree("db/repos.py").body
                    if isinstance(n, ast.ClassDef) and n.name == "OutcomeHistoryRepo")
        unbuilt = []
        for fn in repo.body:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = {getattr(n.func, "attr", getattr(n.func, "id", ""))
                     for n in ast.walk(fn) if isinstance(n, ast.Call)}
            if "select" in calls and fn.name not in ("_authorized", "count",
                                                    "list_device_outcome_dicts"):
                unbuilt.append(fn.name)
            if fn.name in ("tally", "device_stats", "first_by_action_ids",
                           "legacy_candidates"):
                assert "_authorized" in calls, fn.name
        assert unbuilt == []

    async def test_no_principal_read_has_a_limit_and_ordered_reads_end_in_id(self):
        state, site_id = await _small_stack()
        statements: list[str] = []

        @event.listens_for(state.engine.sync_engine, "before_cursor_execute")
        def _capture(conn, cursor, statement, *_a):
            if "cc_outcome_history" in statement:
                statements.append(" ".join(statement.split()))

        async with state.sessionmaker() as session:
            repo = OutcomeHistoryRepo(session)
            await repo.tally("t1")
            await repo.device_stats("t1", scope=None, now=NOW)
            await repo.first_by_action_ids("t1", ["directive:a"])
            await repo.legacy_candidates("t1", [("node-1", "SEL_CLEAR")])
            principal = list(statements)
            statements.clear()
            await repo.list_device_outcome_dicts("t1")
            internal = list(statements)
        assert principal and all(" LIMIT " not in s.upper() for s in principal)
        assert internal and " LIMIT " in internal[0].upper()
        # The keyed reads -- settlement's and the receipt's, and the legacy
        # candidates -- end in the immutable id.
        keyed = [s for s in principal
                 if "cc_outcome_history.action_id IN" in s or "action_type) IN" in s]
        assert len(keyed) == 2
        assert all(s.rstrip().endswith("cc_outcome_history.id") for s in keyed)
        # The decay sums are summed in Python's order where the engine can.
        from harkeniq_cc.db.outcome_sql import SQLITE_ORDERED_AGGREGATES

        stats = next(s for s in principal if SQLITE_DECAY_FUNCTION in s)
        ordered = "ORDER BY cc_outcome_history.recorded_at, cc_outcome_history.id)"
        assert stats.count(ordered) == (2 if SQLITE_ORDERED_AGGREGATES else 0)

    def test_settlement_pages_every_proposal_and_dispatch_is_untouched(self):
        runtime = self._tree("agent_runtime.py")

        def calls_in(name):
            fn = next(n for n in ast.walk(runtime)
                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
            return {getattr(n.func, "attr", "") for n in ast.walk(fn) if isinstance(n, ast.Call)}

        assert "page_by_status" in calls_in("settle_outcomes")
        assert "list_by_status" not in calls_in("settle_outcomes")
        assert "list_by_status" in calls_in("dispatch_decided")

    def test_the_learning_engine_keeps_no_cursor(self):
        assert not hasattr(IntelligenceEngine(), "_cursor")
        assert "rebuild" in inspect.getsource(IntelligenceEngine.run_cycle)

    def test_the_sqlite_weight_is_the_python_weight(self):
        """SQLite renders the weight by calling `predictive.decay_weight`
        on the stored strings: equal, to the bit, for any row -- past,
        future (the clamp), naive or aware."""
        rng = random.Random(41)
        now = datetime(2026, 10, 4, 12, 0, 0, 123456, tzinfo=timezone.utc)
        for _ in range(2000):
            offset = timedelta(seconds=rng.uniform(-86_400 * 3, 86_400 * 900))
            recorded = (now - offset).replace(microsecond=rng.randrange(1_000_000))
            stored = recorded.replace(tzinfo=None).isoformat(sep=" ")
            assert _sqlite_decay_weight(now.isoformat(sep=" "), stored) == \
                decay_weight(recorded, now), stored

    def test_postgresql_mirrors_the_python_pipeline(self):
        from sqlalchemy import bindparam, DateTime
        from sqlalchemy.dialects import postgresql

        sql = str(decay_weight_sql(
            bindparam("decay_now", NOW, type_=DateTime(timezone=True)),
            CCOutcomeHistory.recorded_at,
        ).compile(dialect=postgresql.dialect()))
        assert sql.startswith("power(0.5::float8, greatest(0.0::float8, extract(epoch from (")
        assert "::float8 / 86400.0::float8) " in sql
        assert sql.endswith(f"/ {float(DECAY_HALF_LIFE_DAYS)!r}::float8)")
        assert SQLITE_DECAY_FUNCTION == "harkeniq_decay_weight"
