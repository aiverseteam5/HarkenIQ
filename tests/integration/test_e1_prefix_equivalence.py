"""E1.1 guardrail A3: the subtree prefix match is cross-database identical.

The materialized path was chosen over a recursive CTE precisely so
PostgreSQL (production) and sqlite (the unit suite) cannot diverge. That
is a claim about two database engines, so it is proved against both and
not asserted in prose.

Two engine behaviours could break it:

* **LIKE case sensitivity.** sqlite's LIKE is ASCII-case-insensitive,
  PostgreSQL's is case-sensitive. Ids are lowercase hex, so no path can
  differ only by case and the two engines cannot disagree.
* **LIKE wildcards.** `%` and `_` are wildcards in both. Neither can
  occur in a hex id, and `autoescape=True` neutralizes them regardless.

Gated on ``HARKEN_TEST_CC_PG_DSN``; skipped when unset.
"""

from __future__ import annotations

import os
import uuid

import pytest

from harkeniq_cc.db.base import create_all, make_engine, make_sessionmaker
from harkeniq_cc.db.models import CCOrgUnit
from harkeniq_cc.db.repos import OrgUnitRepo

DSN = os.environ.get("HARKEN_TEST_CC_PG_DSN", "")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not DSN, reason="HARKEN_TEST_CC_PG_DSN not set"),
]


#: A shape built to break a naive prefix match:
#: `c7` and `c70` are siblings whose ids extend one another, so a match
#: without the trailing delimiter would pull `c70` into `c7`'s subtree.
SHAPE = [
    # (key, parent key)
    ("root", None),
    ("west", "root"),
    ("east", "root"),
    ("c7", "west"),
    ("c70", "west"),
    ("hall", "c7"),
]


async def _build(sessionmaker, tenant: str, ids: dict[str, str]) -> dict[str, CCOrgUnit]:
    """Build SHAPE with ids chosen so `c70` literally extends `c7`.

    Real ids are always 32 hex characters, and two ids of equal width
    cannot be strict prefixes of one another -- so on today's data the
    sibling trap cannot arise at all. That is a property of the id
    generator, not of the query, and the trailing delimiter is what
    makes the query safe *without depending on it*: if a future import
    ever admitted a shorter or variable-width id, the prefix match would
    still be correct. These deliberately short ids are that future.
    """
    made: dict[str, CCOrgUnit] = {}
    async with sessionmaker() as session:
        for key, parent_key in SHAPE:
            parent = made.get(parent_key) if parent_key else None
            path = (parent.path if parent else "/") + ids[key] + "/"
            unit = CCOrgUnit(
                id=ids[key],
                tenant_id=tenant,
                parent_id=parent.id if parent else None,
                unit_type="region",
                name=key,
                path=path,
                depth=(parent.depth + 1) if parent else 1,
            )
            session.add(unit)
            await session.flush()
            made[key] = unit
        await session.commit()
    return made


async def _subtree_names(sessionmaker, tenant: str, path: str) -> list[str]:
    async with sessionmaker() as session:
        rows = await OrgUnitRepo(session).list_subtree(tenant, path)
        return sorted(r.name for r in rows)


@pytest.mark.asyncio
async def test_the_same_query_returns_the_same_subtree_on_both_engines():
    tenant = f"eq-{uuid.uuid4().hex[:8]}"

    pg_engine = make_engine(DSN)
    await create_all(pg_engine)
    pg_sessions = make_sessionmaker(pg_engine)

    lite_engine = make_engine("sqlite+aiosqlite:///:memory:")
    await create_all(lite_engine)
    lite_sessions = make_sessionmaker(lite_engine)

    try:
        # IDENTICAL rows on both engines, or the comparison is vacuous.
        ids = {
            "root": uuid.uuid4().hex,
            "west": uuid.uuid4().hex,
            "east": uuid.uuid4().hex,
            # The trap, possible only because these two ids differ in
            # width. Real ids never do -- see _build's docstring.
            "c7": "c7",
            "c70": "c70",
            "hall": uuid.uuid4().hex,
        }
        pg_units = await _build(pg_sessions, tenant, ids)
        await _build(lite_sessions, tenant, ids)

        for key in ("root", "west", "c7", "c70", "east"):
            path = pg_units[key].path
            pg = await _subtree_names(pg_sessions, tenant, path)
            lite = await _subtree_names(lite_sessions, tenant, path)
            assert pg == lite, f"engines disagreed on the subtree of {key}"

        # And the answer is the CORRECT one, not merely a matching one:
        # Cluster 7 contains itself and its hall, and never its sibling
        # whose id extends its own.
        c7 = await _subtree_names(pg_sessions, tenant, pg_units["c7"].path)
        assert c7 == ["c7", "hall"]
        assert "c70" not in c7

        # The trap is real: without the trailing delimiter it would match.
        assert pg_units["c70"].path.startswith(pg_units["c7"].path.rstrip("/"))
    finally:
        async with pg_sessions() as session:
            from sqlalchemy import delete

            await session.execute(
                delete(CCOrgUnit).where(CCOrgUnit.tenant_id == tenant)
            )
            await session.commit()
        await pg_engine.dispose()
        await lite_engine.dispose()


@pytest.mark.asyncio
async def test_like_wildcards_cannot_be_smuggled_into_a_path():
    """A path containing `%` would otherwise select the whole tenant.

    Ids are hex so this cannot arise through the API; the test forces a
    poisoned row in to prove `autoescape` holds the line anyway.
    """
    tenant = f"eq-{uuid.uuid4().hex[:8]}"
    engine = make_engine(DSN)
    await create_all(engine)
    sessions = make_sessionmaker(engine)
    try:
        async with sessions() as session:
            for name, path in (
                ("real", "/aaaa/"),
                ("other", "/bbbb/"),
                ("poison", "/%/"),
            ):
                session.add(
                    CCOrgUnit(
                        id=uuid.uuid4().hex, tenant_id=tenant, parent_id=None,
                        unit_type="region", name=name, path=path, depth=1,
                    )
                )
            await session.commit()

        matched = await _subtree_names(sessions, tenant, "/%/")
        assert matched == ["poison"], "a `%` path escaped and matched siblings"
    finally:
        async with sessions() as session:
            from sqlalchemy import delete

            await session.execute(
                delete(CCOrgUnit).where(CCOrgUnit.tenant_id == tenant)
            )
            await session.commit()
        await engine.dispose()
