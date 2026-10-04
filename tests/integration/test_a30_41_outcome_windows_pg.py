"""A30.41 on real PostgreSQL: what SQLite cannot show.

* the decay sums PostgreSQL computes match Python's per-row rate;
* reruns are identical even when the planner is pushed onto a PARALLEL plan
  (the sums are ordered aggregates, so worker timing cannot reorder them);
* rows inserted concurrently, and a row committed late with an EARLIER
  ingested_at, are counted -- the learning engine's old cursor skipped it;
* the boundaries hold on PostgreSQL's own types;
* the 0028 indexes serve the reads they were built for;
* 0027 -> 0028 lands on a database holding rows, CONCURRENTLY, rebuilds an
  index an interrupted build left INVALID, and is idempotent.

Gated on ``HARKEN_TEST_CC_PG_DSN``: an alembic-managed Central Command
database at head.
"""

from __future__ import annotations

import asyncio
import os
import random
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import insert, text

from harkeniq_cc import agent_runtime
from harkeniq_cc.config import CCConfig
from harkeniq_cc.db.base import make_engine, make_sessionmaker
from harkeniq_cc.db.models import CCAgentProposal, CCFleetCache, CCOutcomeHistory, CCSite
from harkeniq_cc.db.repos import OutcomeHistoryRepo
from harkeniq_cc.intelligence import IntelligenceEngine
from harkeniq_cc.predictive import weighted_failure_rate
from harkeniq_cc.runtime import AppState

CC_DSN = os.environ.get("HARKEN_TEST_CC_PG_DSN", "")
REPO = Path(__file__).parents[2]

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not CC_DSN, reason="HARKEN_TEST_CC_PG_DSN not set"),
]

NEW_INDEXES = {
    "ix_outcome_history_action_id", "ix_outcome_history_site",
    "ix_outcome_history_device_time", "ix_outcome_history_actor",
}
OLD_INDEX = "ix_outcome_history_device"


def _alembic(*args: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("HARKEN_")}
    env["HARKEN_CC_DSN"] = CC_DSN
    env["HARKEN_CC_TENANT_ID"] = "tenant-demo"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args], cwd=REPO / "services/central_command",
        env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


class _Stack:
    """A tenant of this run's own on the shared, migrated database."""

    def __init__(self):
        self.tag = uuid.uuid4().hex[:8]
        self.tenant = f"a3041-{self.tag}"
        self.engine = make_engine(CC_DSN)
        self.sessionmaker = make_sessionmaker(self.engine)
        self.state = AppState(config=CCConfig(tenant_id=self.tenant, insecure=True),
                              engine=self.engine, sessionmaker=self.sessionmaker)
        self.site_id = ""

    async def start(self, devices: int = 1) -> "_Stack":
        async with self.sessionmaker() as session:
            site = CCSite(id=f"s-{self.tag}", tenant_id=self.tenant,
                          site_name=f"dc-{self.tag}", sm_endpoint="sm:1", sm_token="tok")
            session.add(site)
            await session.flush()
            for d in range(devices):
                session.add(CCFleetCache(site_id=site.id, agent_id=self.dev(d),
                                         agent_name=f"n{d}", vendor="Dell", model="R750",
                                         observation="observed"))
            await session.commit()
            self.site_id = site.id
        return self

    def dev(self, d: int) -> str:
        return f"d{d}-{self.tag}"

    async def bulk(self, rows: list[dict]) -> None:
        async with self.sessionmaker() as session:
            for start in range(0, len(rows), 10_000):
                await session.execute(insert(CCOutcomeHistory), [
                    {"site_id": self.site_id, "action_type": "SEL_CLEAR", "vendor": "Dell",
                     "model": "R750", "actor": "a3041", "fault_resolved": False, **r}
                    for r in rows[start:start + 10_000]
                ])
            await session.commit()

    async def stop(self) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(text("delete from cc_agent_proposals where tenant_id = :t"),
                               {"t": self.tenant})
            await conn.execute(text("delete from cc_outcome_history where site_id = :s"),
                               {"s": self.site_id})
            await conn.execute(text("delete from cc_fleet_cache where site_id = :s"),
                               {"s": self.site_id})
            await conn.execute(text("delete from cc_sites where id = :s"), {"s": self.site_id})
        await self.engine.dispose()


def _row(stack, k, *, device=0, outcome="SUCCESS", recorded, ingested=None, key=""):
    return {"action_id": key or f"r{k}-{stack.tag}", "device_agent_id": stack.dev(device),
            "outcome": outcome, "recorded_at": recorded, "ingested_at": ingested or recorded}


async def test_the_decay_sums_match_python_on_postgres():
    """200 devices, random histories -- whole seconds and microseconds, ties,
    and future-dated rows the clamp must catch. PostgreSQL's rate equals
    Python's per-row rate to the fourth decimal for every device, and the
    two agree to 1e-12 (bit-identical where the libms agree)."""
    stack = await _Stack().start(devices=200)
    try:
        rng = random.Random(4141)
        now = datetime.now(timezone.utc)
        rows, by_device = [], {}
        for d in range(200):
            for k in range(rng.randint(1, 60)):
                recorded = now - timedelta(seconds=rng.randint(-120, 86_400 * 700),
                                           microseconds=rng.choice([0, rng.randrange(1_000_000)]))
                outcome = rng.choice(["SUCCESS"] * 6 + ["FAILURE", "ROLLBACK", "PARTIAL"])
                rows.append(_row(stack, f"{d}-{k}", device=d, outcome=outcome, recorded=recorded))
                by_device.setdefault(stack.dev(d), []).append(
                    {"outcome": outcome, "recorded_at": recorded})
        await stack.bulk(rows)
        async with stack.sessionmaker() as session:
            stats = await OutcomeHistoryRepo(session).device_stats(
                stack.tenant, scope=None, now=now)
        assert stats.total == len(rows)
        identical = 0
        for device, history in by_device.items():
            ordered = sorted(history, key=lambda r: r["recorded_at"])
            py_rate, _ = weighted_failure_rate(ordered, now=now)
            got = stats.devices[device]
            assert got.count == len(history)
            assert round(got.rate(), 4) == round(py_rate, 4), device
            assert abs(got.rate() - py_rate) <= 1e-12, device
            identical += got.rate() == py_rate
        assert identical >= 190, f"only {identical}/200 bit-identical"
    finally:
        await stack.stop()


async def test_reruns_are_identical_under_a_parallel_plan():
    stack = await _Stack().start(devices=50)
    try:
        now = datetime.now(timezone.utc)
        base = now - timedelta(days=30)
        await stack.bulk([
            _row(stack, k, device=k % 50, outcome="FAILURE" if k % 7 == 0 else "SUCCESS",
                 recorded=base + timedelta(seconds=k // 3))       # many ties
            for k in range(60_000)
        ])
        results = []
        for _ in range(4):
            async with stack.sessionmaker() as session:
                for setting in ("max_parallel_workers_per_gather = 4",
                                "parallel_setup_cost = 0", "parallel_tuple_cost = 0",
                                "min_parallel_table_scan_size = 0",
                                "min_parallel_index_scan_size = 0"):
                    await session.execute(text(f"SET LOCAL {setting}"))
                repo = OutcomeHistoryRepo(session)
                results.append((await repo.device_stats(stack.tenant, scope=None, now=now),
                                await repo.tally(stack.tenant)))
        assert all(r == results[0] for r in results[1:])
        assert results[0][0].total == 60_000
    finally:
        await stack.stop()


async def test_the_boundaries_hold_on_postgres():
    """10,001 rows: the newest outcome settles its proposal and the tally is
    exact; 50,001: the newest failure moves the device's rate."""
    stack = await _Stack().start(devices=1)
    try:
        old = datetime(2025, 1, 1, tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        await stack.bulk([_row(stack, k, recorded=old + timedelta(seconds=k))
                          for k in range(10_000)])
        await stack.bulk([_row(stack, "newest", outcome="FAILURE", recorded=now,
                               ingested=now + timedelta(hours=1), key=f"directive:n-{stack.tag}")])
        async with stack.sessionmaker() as session:
            session.add(CCAgentProposal(
                tenant_id=stack.tenant, agent_id="agent-one", actor="op-agent:agent-one@v1",
                agent_version=1, site_id=stack.site_id, device_agent_id=stack.dev(0),
                action_type="SEL_CLEAR", params={}, rationale="r", evidence={},
                disposition="requires_approval", authorization_basis="human_approval",
                status="dispatched", dedupe_key=f"k-{stack.tag}",
                directive_id=f"n-{stack.tag}", dispatched_at=now - timedelta(minutes=1)))
            await session.commit()
            tally = await OutcomeHistoryRepo(session).tally(stack.tenant)
        assert sum(r["count"] for r in tally) == 10_001
        assert sum(r["count"] for r in tally if r["outcome"] == "FAILURE") == 1
        assert await agent_runtime.settle_outcomes(stack.state, stack.tenant) == 1

        await stack.bulk([_row(stack, f"b{k}", recorded=old - timedelta(days=1, seconds=k))
                          for k in range(39_999)])
        async with stack.sessionmaker() as session:
            stats = await OutcomeHistoryRepo(session).device_stats(
                stack.tenant, scope=None, now=now)
        assert stats.total == 50_000
        before = stats.devices[stack.dev(0)].rate()
        await stack.bulk([_row(stack, "newest2", outcome="FAILURE",
                               recorded=now - timedelta(seconds=5))])
        async with stack.sessionmaker() as session:
            stats = await OutcomeHistoryRepo(session).device_stats(
                stack.tenant, scope=None, now=now)
        assert stats.total == 50_001
        assert stats.devices[stack.dev(0)].rate() > before
    finally:
        await stack.stop()


async def test_concurrent_inserts_and_a_late_commit_are_never_lost():
    stack = await _Stack().start(devices=4)
    try:
        t0 = datetime.now(timezone.utc) - timedelta(hours=2)
        await stack.bulk([_row(stack, k, device=k % 4, recorded=t0 + timedelta(seconds=k))
                          for k in range(100)])
        engine = IntelligenceEngine()

        async def cycle() -> int:
            async with stack.sessionmaker() as session:
                await engine.run_cycle(session, stack.tenant)
                await session.commit()
            return sum(m.total_count for m in engine.aggregator.get_metrics())

        assert await cycle() == 100
        # A late commit: transaction A takes an EARLY ingested_at and holds;
        # B commits a later one; a cycle runs between them.
        async with stack.engine.connect() as conn:
            tx = await conn.begin()
            await conn.execute(insert(CCOutcomeHistory).values(
                site_id=stack.site_id, action_id=f"late-{stack.tag}", action_type="SEL_CLEAR",
                device_agent_id=stack.dev(0), vendor="Dell", model="R750", outcome="FAILURE",
                actor="a3041", recorded_at=t0, ingested_at=t0))
            await stack.bulk([_row(stack, "b", recorded=datetime.now(timezone.utc))])
            assert await cycle() == 101      # B is visible, A is not yet
            await tx.commit()
        assert await cycle() == 102          # the old cursor would never see A

        # Eight writers at once while the tally is read repeatedly.
        async def writer(w: int) -> None:
            await stack.bulk([_row(stack, f"w{w}-{k}", device=w % 4,
                                   recorded=t0 + timedelta(seconds=k)) for k in range(250)])

        seen: list[int] = []

        async def reader() -> None:
            for _ in range(10):
                async with stack.sessionmaker() as session:
                    seen.append(sum(r["count"] for r in
                                    await OutcomeHistoryRepo(session).tally(stack.tenant)))
                await asyncio.sleep(0)

        await asyncio.gather(*(writer(w) for w in range(8)), reader())
        assert all(102 <= n <= 2102 for n in seen) and seen == sorted(seen)
        assert await cycle() == 2102
    finally:
        await stack.stop()


async def test_the_indexes_serve_the_reads():
    engine = make_engine(CC_DSN)
    try:
        async with engine.connect() as conn:
            valid = dict((await conn.execute(text(
                "SELECT c.relname, i.indisvalid FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_class t ON t.oid = i.indrelid "
                "WHERE t.relname = 'cc_outcome_history'"))).all())
            assert NEW_INDEXES <= set(valid) and all(valid[n] for n in NEW_INDEXES)
            assert OLD_INDEX not in valid
            ddl = (await conn.execute(text(
                "select indexdef from pg_indexes where indexname = 'ix_outcome_history_device_time'"
            ))).scalar_one()
            assert "INCLUDE (site_id, outcome)" in ddl
            actor_ddl = (await conn.execute(text(
                "select indexdef from pg_indexes where indexname = 'ix_outcome_history_actor'"
            ))).scalar_one()
            assert "varchar_pattern_ops" in actor_ddl

            async def plan(sql: str) -> str:
                await conn.execute(text("SET enable_seqscan = off"))
                rows = (await conn.execute(text(f"EXPLAIN {sql}"))).all()
                return "\n".join(r[0] for r in rows)

            assert "ix_outcome_history_action_id" in await plan(
                "SELECT * FROM cc_outcome_history WHERE action_id IN ('directive:a','directive:b') "
                "ORDER BY action_id, ingested_at, id")
            assert "ix_outcome_history_device_time" in await plan(
                "SELECT device_agent_id, count(*), sum(1.0 ORDER BY recorded_at, id) "
                "FROM cc_outcome_history GROUP BY device_agent_id ORDER BY device_agent_id")
            assert "ix_outcome_history_site" in await plan(
                "SELECT site_id, count(*) FROM cc_outcome_history WHERE site_id IN ('s1','s2') "
                "GROUP BY site_id")
            assert "ix_outcome_history_actor" in await plan(
                "SELECT count(*) FROM cc_outcome_history WHERE actor LIKE 'op-agent:abc@v%'")
            assert "ix_outcome_history_actor" in await plan(
                "SELECT * FROM cc_outcome_history WHERE actor = 'campaign:c@v1' "
                "ORDER BY ingested_at, id")
            await conn.rollback()
    finally:
        await engine.dispose()


async def test_0027_to_0028_on_a_database_holding_rows_and_an_invalid_index():
    """The production upgrade: rows present, the old index in place, and one
    new index left INVALID by an interrupted concurrent build. 0028 builds
    the rest CONCURRENTLY, rebuilds the invalid one, drops the old one,
    moves no row, and a re-run is a no-op."""
    stack = await _Stack().start(devices=1)
    engine = make_engine(CC_DSN)
    try:
        now = datetime.now(timezone.utc)
        await stack.bulk([_row(stack, k, recorded=now - timedelta(seconds=k)) for k in range(500)])
        async with engine.connect() as conn:
            for name in NEW_INDEXES - {"ix_outcome_history_site"}:
                await conn.execute(text(f"DROP INDEX IF EXISTS {name}"))
            await conn.execute(text(
                f"CREATE INDEX IF NOT EXISTS {OLD_INDEX} ON cc_outcome_history (device_agent_id)"))
            await conn.execute(text(
                "UPDATE pg_index SET indisvalid = false WHERE indexrelid = "
                "'ix_outcome_history_site'::regclass"))
            await conn.execute(text("UPDATE alembic_version SET version_num = '0027'"))
            await conn.commit()
            before = (await conn.execute(text(
                "select count(*), max(recorded_at) from cc_outcome_history where site_id = :s"),
                {"s": stack.site_id})).one()
        _alembic("upgrade", "head")
        async with engine.connect() as conn:
            version = (await conn.execute(text("select version_num from alembic_version"))).scalar_one()
            valid = dict((await conn.execute(text(
                "SELECT c.relname, i.indisvalid FROM pg_index i "
                "JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_class t ON t.oid = i.indrelid "
                "WHERE t.relname = 'cc_outcome_history'"))).all())
            after = (await conn.execute(text(
                "select count(*), max(recorded_at) from cc_outcome_history where site_id = :s"),
                {"s": stack.site_id})).one()
        assert version == "0028"
        assert NEW_INDEXES <= set(valid) and all(valid[n] for n in NEW_INDEXES), valid
        assert OLD_INDEX not in valid
        assert tuple(after) == tuple(before)
        _alembic("upgrade", "head")      # idempotent
    finally:
        _alembic("upgrade", "head")
        await engine.dispose()
        await stack.stop()
