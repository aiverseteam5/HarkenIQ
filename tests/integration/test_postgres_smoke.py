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


async def test_agent_identity_register_roundtrip():
    """QA-040 regression: RegisterAgent crashed on postgres because the R3a
    model mapped bytes onto Text columns (asyncpg DataError). sqlite accepted
    the bytes silently, so only a real postgres exercises the failing path:
    register (BYTEA insert incl. the non-UTF-8 certificate) -> signature
    verify -> peer-key bundle read-back.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from harkeniq.autonomy.identity import AgentIdentity
    from harkeniq_sm.agent_identity import AgentIdentityService
    from harkeniq_sm.db.base import make_sessionmaker

    assert _alembic("upgrade", "head").returncode == 0
    engine = make_engine(DSN)
    try:
        service = AgentIdentityService(
            make_sessionmaker(engine), Ed25519PrivateKey.generate()
        )
        agent = AgentIdentity.generate()
        sm_pub, certificate = await service.register_agent(
            agent_id=agent.agent_id,
            public_key_pem=agent.public_key_pem,
            site_name="pg-smoke",
        )
        assert sm_pub.startswith(b"-----BEGIN PUBLIC KEY-----")
        assert certificate  # canonical JSON + raw Ed25519 signature (not UTF-8)

        message = b"pg-smoke lease check"
        signature = agent.sign(message)
        assert await service.verify_agent_signature(agent.agent_id, message, signature)

        peer_keys, bundle_sig = await service.get_peer_keys()
        assert peer_keys[agent.agent_id] == agent.public_key_pem
        assert bundle_sig

        # Re-registration with the same key is an upsert, not a crash.
        await service.register_agent(
            agent_id=agent.agent_id,
            public_key_pem=agent.public_key_pem,
            site_name="pg-smoke",
        )
    finally:
        await engine.dispose()
