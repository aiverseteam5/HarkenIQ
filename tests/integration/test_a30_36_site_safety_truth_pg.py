"""S3-E1-0 (A30.36) on a REAL PostgreSQL: the Site Manager's per-site truth.

The sqlite proof lives in tests/unit/sm/test_a30_36_site_safety_truth.py.
This asks what only the production engine can answer: the per-site
fault-domain read, the persisted halts, the per-site error budgets and the
node-path report -- action row, outcome and the device's `timestamptz`
touch -- under PostgreSQL's types and constraints, and that the whole
recorded stage sequence reads the same on both engines.

Each stack owns a tag suffixing every name it writes, because the database
is shared and migrated, not created. Gated on ``HARKEN_TEST_SM_PG_DSN``.
"""

from __future__ import annotations

import os
import uuid

import pytest

from tests.unit.sm import e1_0_estate as E

DSN = os.environ.get("HARKEN_TEST_SM_PG_DSN", "")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not DSN, reason="HARKEN_TEST_SM_PG_DSN not set"),
]


def _tag() -> str:
    return f"-{uuid.uuid4().hex[:8]}"


@pytest.mark.parametrize("topology", ["multi", "single"])
async def test_the_stages_read_the_same_on_postgres_as_on_sqlite(topology):
    on_sqlite = await E.run_stages(topology)
    on_postgres = await E.run_stages(topology, dsn=DSN, tag=_tag())
    for lite, pg in zip(on_sqlite, on_postgres):
        assert pg["stage"] == lite["stage"]
        assert pg["outcomes"] == lite["outcomes"], pg["stage"]
        assert pg["sm"] == lite["sm"], pg["stage"]
        assert pg["cc"] == lite["cc"], pg["stage"]


async def test_one_sites_change_never_moves_the_other_on_postgres():
    stack = await E.build(("alpha", "beta"), dsn=DSN, tag=_tag())
    try:
        await E.push_policy(stack)
        before = E.scrub(await E.snapshot(stack, "alpha"), stack.tag)
        await E.halt(stack, "beta")
        await E.suppress(stack, "beta")
        await E.report(stack, "b1", "SEL_CLEAR", n=3)
        await E.report(stack, "b2", "BMC_RESET", status="FAILED", success=False, n=5)
        assert E.scrub(await E.snapshot(stack, "alpha"), stack.tag) == before
        beta = await E.snapshot(stack, "beta")
        assert beta["sm_stop_switch"] is True
        assert [s["domain_id"] for s in beta["suppressions"]] == [stack.domain_id("beta")]
        assert beta["site_budgets"]["SEL_CLEAR"] == 0
        eb = {b["action_type"]: b for b in beta["error_budgets"]}
        assert eb["BMC_RESET"]["dropped_back"] is True
    finally:
        await E.halt(stack, "beta", active=False)
        await E.close(stack)


async def test_the_node_path_report_touches_a_zoned_timestamp_on_postgres():
    from harkeniq_sm.db.models import Device

    stack = await E.build(("alpha", "beta"), dsn=DSN, tag=_tag())
    try:
        assert await E.report(stack, "b1", "SEL_CLEAR") == ["accepted"]
        async with stack.state.sessionmaker() as session:
            device = await session.get(Device, stack.device_row("b1"))
        assert device.site_id == stack.site_ids["beta"]
        assert device.last_seen_at is not None
        assert device.last_seen_at.tzinfo is not None, "timestamptz came back without a zone"
    finally:
        await E.close(stack)
