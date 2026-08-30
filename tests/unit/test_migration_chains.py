"""The alembic chains actually run, on fresh AND on existing databases.

The existing migration test checks that files exist. That would not have
caught the real failure mode of this chain: 0001 is a `create_all` from
CURRENT models, so a fresh database is born with every table the models
declare, and any later migration that issues a plain CREATE TABLE breaks
the first time someone deploys from scratch. Every additive migration
carries an inspector guard for exactly that reason, and this test is
what keeps the guard honest.

Both paths are exercised:
  fresh   0001 create_all, then every later revision, to head
  legacy  a database stamped at the previous revision with the new
          objects removed, then upgraded (the production path)
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).parents[2]

SERVICES = {
    "cc": (REPO / "services/central_command", "HARKEN_CC_DSN", "0009"),
    "sm": (REPO / "services/site_manager", "HARKEN_SM_DSN", "0007"),
}


def _alembic(service: str, db_path: Path, *args: str) -> None:
    cwd, env_var, _ = SERVICES[service]
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=cwd,
        env={
            **_clean_env(),
            env_var: f"sqlite+aiosqlite:///{db_path}",
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} failed for {service}:\n"
        f"{result.stdout}\n{result.stderr}"
    )


def _clean_env() -> dict:
    import os

    env = dict(os.environ)
    # PYTHONPATH from the test runner would shadow each service's own
    # prepend_sys_path and silently migrate the wrong metadata.
    env.pop("PYTHONPATH", None)
    return env


def _tables(db_path: Path) -> set[str]:
    con = sqlite3.connect(db_path)
    try:
        return {
            r[0] for r in con.execute(
                "select name from sqlite_master where type='table'"
            )
        }
    finally:
        con.close()


def _columns(db_path: Path, table: str) -> set[str]:
    con = sqlite3.connect(db_path)
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    finally:
        con.close()


def _version(db_path: Path) -> str:
    con = sqlite3.connect(db_path)
    try:
        return con.execute("select version_num from alembic_version").fetchone()[0]
    finally:
        con.close()


class TestFreshDatabase:
    def test_cc_chain_reaches_head(self, tmp_path):
        db = tmp_path / "cc.db"
        _alembic("cc", db, "upgrade", "head")
        assert _version(db) == SERVICES["cc"][2]
        assert {
            "cc_operational_agents", "cc_agent_scopes",
            "cc_agent_capabilities", "cc_agent_proposals",
            "cc_approval_records",
        } <= _tables(db)
        assert "actor" in _columns(db, "cc_outcome_history")
        assert "principal_ref" in _columns(db, "cc_approval_group_members")

    def test_sm_chain_reaches_head(self, tmp_path):
        db = tmp_path / "sm.db"
        _alembic("sm", db, "upgrade", "head")
        assert _version(db) == SERVICES["sm"][2]
        assert {"actor", "authorization_basis", "proposal_id"} <= _columns(
            db, "sm_directives"
        )
        assert "actor" in _columns(db, "sm_action_outcomes")


class TestExistingDatabase:
    """The production path: a database that predates this slice."""

    def test_cc_upgrade_from_0008(self, tmp_path):
        """E0.1's ledger lands on a database that predates it."""
        db = tmp_path / "cc.db"
        _alembic("cc", db, "upgrade", "head")
        con = sqlite3.connect(db)
        con.execute("drop table cc_approval_records")
        con.execute(
            "alter table cc_approval_group_members drop column principal_ref"
        )
        con.execute("update alembic_version set version_num='0008'")
        con.commit()
        con.close()

        _alembic("cc", db, "upgrade", "head")
        assert _version(db) == "0009"
        assert "cc_approval_records" in _tables(db)
        assert "principal_ref" in _columns(db, "cc_approval_group_members")

    def test_cc_upgrade_from_0007(self, tmp_path):
        db = tmp_path / "cc.db"
        _alembic("cc", db, "upgrade", "head")
        con = sqlite3.connect(db)
        for table in (
            "cc_agent_proposals", "cc_agent_capabilities",
            "cc_agent_scopes", "cc_operational_agents",
        ):
            con.execute(f"drop table {table}")
        # sqlite cannot drop a column on older versions; rebuild the one
        # table whose column A0 adds.
        con.execute("alter table cc_outcome_history rename to _old")
        con.execute(
            "create table cc_outcome_history (id text primary key, "
            "site_id text, action_id text, action_type text, "
            "device_agent_id text, vendor text, model text, outcome text, "
            "fault_resolved boolean, recorded_at datetime, "
            "ingested_at datetime)"
        )
        con.execute("drop table _old")
        con.execute("update alembic_version set version_num='0007'")
        con.commit()
        con.close()

        _alembic("cc", db, "upgrade", "head")
        # Reaches HEAD from a legacy stamp, not merely the next revision:
        # every guarded migration between here and head must be a no-op
        # on the objects that already exist.
        assert _version(db) == SERVICES["cc"][2]
        assert "cc_operational_agents" in _tables(db)
        assert "actor" in _columns(db, "cc_outcome_history")

    def test_sm_upgrade_from_0006(self, tmp_path):
        db = tmp_path / "sm.db"
        _alembic("sm", db, "upgrade", "head")
        con = sqlite3.connect(db)
        con.execute("alter table sm_directives drop column actor")
        con.execute("alter table sm_directives drop column authorization_basis")
        con.execute("alter table sm_directives drop column proposal_id")
        con.execute("alter table sm_action_outcomes drop column actor")
        con.execute("update alembic_version set version_num='0006'")
        con.commit()
        con.close()

        _alembic("sm", db, "upgrade", "head")
        assert _version(db) == "0007"
        assert {"actor", "authorization_basis", "proposal_id"} <= _columns(
            db, "sm_directives"
        )
