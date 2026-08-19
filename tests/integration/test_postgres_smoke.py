"""Alembic + hypertable smoke against a real TimescaleDB (R2a deploy).

Gated on ``HARKEN_TEST_PG_DSN`` (e.g. from deploy/site_manager compose:
``postgresql+asyncpg://harkeniq:harkeniq@localhost:5432/harkeniq_sm``).
Not part of the exit gate; skipped when the env var is unset.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa

from harkeniq_sm.db.base import make_engine

DSN = os.environ.get("HARKEN_TEST_PG_DSN", "")
SM_DIR = Path(__file__).parents[2] / "services" / "site_manager"

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not DSN, reason="HARKEN_TEST_PG_DSN not set"),
]

EXPECTED_TABLES = {
    "sites", "racks", "devices", "fault_domains", "domain_memberships",
    "verdict_reports", "heartbeats", "agent_status",
    "device_subsystem_state", "incidents", "actions", "audit_log",
}


def _alembic(*args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "HARKEN_SM_DSN": DSN}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=SM_DIR, env=env, capture_output=True, text=True,
    )


async def test_upgrade_head_creates_schema():
    result = _alembic("upgrade", "head")
    assert result.returncode == 0, result.stderr

    engine = make_engine(DSN)
    try:
        async with engine.connect() as conn:
            tables = set(
                (await conn.execute(sa.text(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                ))).scalars()
            )
            assert EXPECTED_TABLES <= tables, EXPECTED_TABLES - tables

            has_timescale = (await conn.execute(sa.text(
                "SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'"
            ))).scalar()
            if has_timescale:
                hypertables = set(
                    (await conn.execute(sa.text(
                        "SELECT hypertable_name FROM timescaledb_information.hypertables"
                    ))).scalars()
                )
                assert {"verdict_reports", "heartbeats"} <= hypertables

            # Idempotent re-run.
            assert _alembic("upgrade", "head").returncode == 0
    finally:
        await engine.dispose()
