"""A30.29 on a REAL PostgreSQL: generation provenance, both services.

The sqlite proofs carry the invariant. This file asks what sqlite cannot
answer honestly:

* the marker is JSONB at both services. JSONB re-orders keys and normalises
  types, and `parse` refuses any key outside the three it names -- so the
  round trip has to be seen on the engine production runs;
* `sm_site_fleet_patterns` has a COMPOSITE primary key. Two sites, one
  pattern, two rows; one site pushed twice, one row -- under a real
  constraint, not sqlite's affinity;
* the upgrades run against databases HOLDING ROWS. CC `0026 -> 0027` and
  SM `0010 -> 0011` with a pre-marker candidate and a legacy pattern row
  present: nothing backfilled, nothing copied, nothing dropped;
* the read policy over persisted grants on the real engine.

Each CC run owns a tenant and an id suffix (the database is shared and
migrated, not created); the SM database is small and owned by this file.

Gated on ``HARKEN_TEST_CC_PG_DSN`` (CC) and ``HARKEN_TEST_SM_PG_DSN`` (SM).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from harkeniq.generation_provenance import KEY, parse, site_visibility
from harkeniq_cc.db.base import make_engine, make_sessionmaker
from harkeniq_cc.db.models import CCCandidateSkill
from harkeniq_cc.db.repos import CandidateSkillRepo
from tests.unit.cc import s3_estate as E
from tests.unit.cc import s4_estate as S
from tests.unit.cc.test_a30_29_generated_content_isolation import (
    PHRASE, SECRET, _assert_visible, _assert_withheld_everywhere, _reads, _seed,
)

CC_DSN = os.environ.get("HARKEN_TEST_CC_PG_DSN", "")
SM_DSN = os.environ.get("HARKEN_TEST_SM_PG_DSN", "")
REPO = Path(__file__).parents[2]

pytestmark = [pytest.mark.postgres]
needs_cc = pytest.mark.skipif(not CC_DSN, reason="HARKEN_TEST_CC_PG_DSN not set")
needs_sm = pytest.mark.skipif(not SM_DSN, reason="HARKEN_TEST_SM_PG_DSN not set")


def _alembic(service: str, dsn: str, *args: str) -> None:
    cwd, env_var = {
        "cc": (REPO / "services/central_command", "HARKEN_CC_DSN"),
        "sm": (REPO / "services/site_manager", "HARKEN_SM_DSN"),
    }[service]
    env = {k: v for k, v in os.environ.items() if not k.startswith("HARKEN_")}
    env[env_var] = dsn
    env["HARKEN_CC_TENANT_ID"] = "tenant-demo"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args], cwd=cwd, env=env,
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"


async def _cc_twin() -> E.Stack:
    tag = uuid.uuid4().hex[:8]
    return await S.build("X", engine=make_engine(CC_DSN), tenant=f"a3029-{tag}", tag=tag)


@needs_cc
class TestCentralCommandOnPostgres:
    async def test_the_historical_attack_and_the_covered_read(self):
        stack = await _cc_twin()
        try:
            hist_inc, hist_cand = await _seed(stack, tag="-hist")
            subject, _ = await E.persona(stack, "site_a")

            listed, detail, cands = await _reads(stack, subject, "site_admin", hist_inc)
            assert detail is not None
            _assert_withheld_everywhere(listed, detail, cands, hist_inc, hist_cand)

            # Now a marked artifact beside it: the same reader sees THAT one
            # (JSONB round trip of the marker) and still not the other.
            marker = site_visibility(stack.site("A")).to_dict()
            new_inc, new_cand = await _seed(stack, marker=marker, tag="-new")
            _l, detail, cands = await _reads(stack, subject, "site_admin", new_inc)
            _assert_visible(detail, cands, new_cand, marker)
            _l, detail, _c = await _reads(stack, subject, "site_admin", hist_inc)
            assert detail["diagnosis"]["generated"]["withheld"] is True

            _l, detail, cands = await _reads(stack, E.OWNER, "tenant_owner", hist_inc)
            assert SECRET in detail["diagnosis"]["generated"]["summary"]
        finally:
            await stack.state.engine.dispose()

    async def test_jsonb_normalisation_does_not_widen_the_marker(self):
        """A marker stored with an extra key on the engine comes back with
        it (JSONB keeps unknown keys) and is still refused whole."""
        stack = await _cc_twin()
        try:
            hostile = {**site_visibility(stack.site("A")).to_dict(), "sites": [stack.site("C")]}
            inc, cand = await _seed(stack, marker=hostile, tag="-wide")
            subject, _ = await E.persona(stack, "site_a")
            listed, detail, cands = await _reads(stack, subject, "site_admin", inc)
            assert detail is not None
            _assert_withheld_everywhere(listed, detail, cands, inc, cand)
            async with stack.state.engine.connect() as conn:
                stored = (await conn.execute(text(
                    "select generation_visibility from cc_candidate_skills "
                    "where skill_id = :s"), {"s": cand})).scalar_one()
            assert "sites" in stored and parse(stored) is None
        finally:
            await stack.state.engine.dispose()

    async def test_concurrent_cross_site_candidates_remain_complete_pairs(self):
        """Two real transactions may race on one tenant/skill id. The final
        row is one writer's complete protected-content/provenance unit, not
        YAML from one and the other writer's marker."""
        engine = make_engine(CC_DSN)
        db = make_sessionmaker(engine)
        tag = uuid.uuid4().hex[:8]
        tenant, skill = f"pair-{tag}", f"candidate-{tag}"
        marker_a = site_visibility(f"site-a-{tag}").to_dict()
        marker_b = site_visibility(f"site-b-{tag}").to_dict()

        async def write(site, yaml, warning, marker):
            async with db() as session:
                await CandidateSkillRepo(session).upsert(tenant, site, {
                    "skill_id": skill, "yaml_text": yaml,
                    "warnings_json": f'["{warning}"]',
                    "generation_visibility": marker,
                })
                await session.commit()

        try:
            # Seed so this test measures competing updates as well as the
            # transaction-scoped same-id serialization.
            await write(f"site-a-{tag}", "name: seed-a\n", "seed-a", marker_a)
            await asyncio.gather(
                write(f"site-a-{tag}", "name: write-a\n", "write-a", marker_a),
                write(f"site-b-{tag}", "name: write-b\n", "write-b", marker_b),
            )
            async with db() as session:
                row = await session.get(CCCandidateSkill, (skill, tenant))
                pair = (row.yaml_text, tuple(row.warnings or []), row.generation_visibility)
            assert pair in (
                ("name: write-a\n", ("write-a",), marker_a),
                ("name: write-b\n", ("write-b",), marker_b),
            )
        finally:
            async with engine.begin() as conn:
                await conn.execute(text(
                    "delete from cc_candidate_skills where tenant_id=:t and skill_id=:s"
                ), {"t": tenant, "s": skill})
            await engine.dispose()

    async def test_concurrent_unmarked_content_never_inherits_marked_provenance(self):
        """Whichever writer commits last, an unmarked replacement is NULL
        and a marked replacement carries its own marker."""
        engine = make_engine(CC_DSN)
        db = make_sessionmaker(engine)
        tag = uuid.uuid4().hex[:8]
        tenant, skill = f"unknown-{tag}", f"candidate-{tag}"
        marker = site_visibility(f"site-a-{tag}").to_dict()

        async def write(yaml, warning, incoming_marker):
            async with db() as session:
                await CandidateSkillRepo(session).upsert(tenant, f"site-a-{tag}", {
                    "skill_id": skill, "yaml_text": yaml,
                    "warnings_json": f'["{warning}"]',
                    "generation_visibility": incoming_marker,
                })
                await session.commit()

        try:
            await write("name: seed\n", "seed", marker)
            await asyncio.gather(
                write("name: marked\n", "marked", marker),
                write(f"description: {SECRET}\n", PHRASE, None),
            )
            async with db() as session:
                row = await session.get(CCCandidateSkill, (skill, tenant))
                if row.yaml_text == f"description: {SECRET}\n":
                    assert row.warnings == [PHRASE]
                    assert row.generation_visibility is None
                else:
                    assert row.yaml_text == "name: marked\n"
                    assert row.warnings == ["marked"]
                    assert row.generation_visibility == marker
        finally:
            async with engine.begin() as conn:
                await conn.execute(text(
                    "delete from cc_candidate_skills where tenant_id=:t and skill_id=:s"
                ), {"t": tenant, "s": skill})
            await engine.dispose()

    async def test_0026_to_0027_on_a_database_holding_rows(self):
        """The production upgrade path: a pre-marker candidate is present,
        the column arrives NULL for it, and nothing else moves."""
        engine = make_engine(CC_DSN)
        tag = uuid.uuid4().hex[:8]
        try:
            async with engine.begin() as conn:
                await conn.execute(text(
                    "insert into cc_candidate_skills (skill_id, tenant_id, site_id, yaml_text, "
                    "source_device, source_component, validation_state, dry_run_matches, "
                    "status, generated_at, received_at) values (:s, :t, 'site-x', "
                    "'name: old', 'node-1', 'fan:1', 'draft', 0, 'received', now(), now())"),
                    {"s": f"pre-{tag}", "t": f"t-{tag}"})
                before = (await conn.execute(text(
                    "select count(*) from cc_candidate_skills"))).scalar_one()
                await conn.execute(text(
                    "alter table cc_candidate_skills drop column generation_visibility"))
                await conn.execute(text("update alembic_version set version_num='0026'"))
            _alembic("cc", CC_DSN, "upgrade", "head")
            async with engine.connect() as conn:
                version = (await conn.execute(text(
                    "select version_num from alembic_version"))).scalar_one()
                after = (await conn.execute(text(
                    "select count(*) from cc_candidate_skills"))).scalar_one()
                nulls = (await conn.execute(text(
                    "select count(*) from cc_candidate_skills "
                    "where generation_visibility is null"))).scalar_one()
                typ = (await conn.execute(text(
                    "select data_type from information_schema.columns where "
                    "table_name='cc_candidate_skills' and column_name='generation_visibility'"
                ))).scalar_one()
            assert version == "0027"
            assert after == before and nulls == after, (before, after, nulls)
            assert typ == "jsonb"
            _alembic("cc", CC_DSN, "upgrade", "head")        # idempotent
        finally:
            async with engine.begin() as conn:
                await conn.execute(text(
                    "delete from cc_candidate_skills where skill_id = :s"), {"s": f"pre-{tag}"})
            await engine.dispose()


@needs_sm
class TestSiteManagerOnPostgres:
    async def test_the_composite_key_and_the_jsonb_marker(self):
        from harkeniq_sm.db.models import Site
        from harkeniq_sm.db.repos import SMSitePatternRepo

        from harkeniq_sm.db.base import make_engine as sm_engine, make_sessionmaker
        engine = sm_engine(SM_DSN)
        db = make_sessionmaker(engine)
        tag = uuid.uuid4().hex[:8]
        try:
            async with db() as session:
                a = Site(name=f"a-{tag}", cc_site_id=f"cc-a-{tag}")
                b = Site(name=f"b-{tag}", cc_site_id=f"cc-b-{tag}")
                session.add_all([a, b])
                await session.flush()
                repo = SMSitePatternRepo(session)
                ma = site_visibility(a.cc_site_id).to_dict()
                mb = site_visibility(b.cc_site_id).to_dict()
                await repo.upsert(a.id, {"pattern_id": f"P-{tag}", "description": "for A"}, ma)
                await repo.upsert(b.id, {"pattern_id": f"P-{tag}", "description": "for B"}, mb)
                await repo.upsert(a.id, {"pattern_id": f"P-{tag}", "description": "for A v2"}, ma)
                await session.commit()
                a_id, b_id = a.id, b.id
            async with engine.connect() as conn:
                rows = (await conn.execute(text(
                    "select site_id, description, visibility from sm_site_fleet_patterns "
                    "where pattern_id = :p order by description"), {"p": f"P-{tag}"})).all()
            assert [(r[0], r[1]) for r in rows] == [(a_id, "for A v2"), (b_id, "for B")]
            assert parse(rows[0][2]).site_id == f"cc-a-{tag}"
            assert parse(rows[1][2]).site_id == f"cc-b-{tag}"
        finally:
            async with engine.begin() as conn:
                await conn.execute(text(
                    "delete from sm_site_fleet_patterns where pattern_id = :p"), {"p": f"P-{tag}"})
                await conn.execute(text(
                    "delete from sites where name in (:a, :b)"), {"a": f"a-{tag}", "b": f"b-{tag}"})
            await engine.dispose()

    async def test_0010_to_0011_on_a_database_holding_rows(self):
        from harkeniq_sm.db.base import make_engine as sm_engine
        engine = sm_engine(SM_DSN)
        tag = uuid.uuid4().hex[:8]
        try:
            async with engine.begin() as conn:
                await conn.execute(text(
                    "insert into sm_fleet_patterns (pattern_id, pattern_type, description, "
                    "confidence, detected_at, received_at) values (:p, 'cross_site_batch', "
                    "'SEL_CLEAR fails at 65% across 3 sites (35/54)', 0.9, '1', now())"),
                    {"p": f"L-{tag}"})
                await conn.execute(text(
                    "insert into sm_candidate_skills (skill_id, yaml_text, source_device, "
                    "source_component, validation_state, dry_run_matches, generated_at, "
                    "reported_to_cc) values (:s, 'name: old', 'node-1', 'fan:1', 'DRAFT', 0, "
                    "now(), false)"), {"s": f"c-{tag}"})
                await conn.execute(text("drop table sm_site_fleet_patterns"))
                await conn.execute(text(
                    "alter table sm_candidate_skills drop column generation_visibility"))
                await conn.execute(text("update alembic_version set version_num='0010'"))
            _alembic("sm", SM_DSN, "upgrade", "head")
            async with engine.connect() as conn:
                version = (await conn.execute(text(
                    "select version_num from alembic_version"))).scalar_one()
                legacy = (await conn.execute(text(
                    "select description from sm_fleet_patterns where pattern_id = :p"),
                    {"p": f"L-{tag}"})).scalar_one()
                per_site = (await conn.execute(text(
                    "select count(*) from sm_site_fleet_patterns"))).scalar_one()
                marker = (await conn.execute(text(
                    "select generation_visibility from sm_candidate_skills where skill_id = :s"),
                    {"s": f"c-{tag}"})).scalar_one()
                pk = (await conn.execute(text(
                    "select string_agg(a.attname, ',' order by a.attnum) from pg_index i "
                    "join pg_attribute a on a.attrelid = i.indrelid and a.attnum = any(i.indkey) "
                    "where i.indrelid = 'sm_site_fleet_patterns'::regclass and i.indisprimary"
                ))).scalar_one()
            assert version == "0011"
            assert legacy == "SEL_CLEAR fails at 65% across 3 sites (35/54)"
            assert per_site == 0, "the legacy row is not copied under any site"
            assert marker is None
            assert set(pk.split(",")) == {"site_id", "pattern_id"}
            _alembic("sm", SM_DSN, "upgrade", "head")        # idempotent
        finally:
            async with engine.begin() as conn:
                await conn.execute(text(
                    "delete from sm_fleet_patterns where pattern_id = :p"), {"p": f"L-{tag}"})
                await conn.execute(text(
                    "delete from sm_candidate_skills where skill_id = :s"), {"s": f"c-{tag}"})
            await engine.dispose()
