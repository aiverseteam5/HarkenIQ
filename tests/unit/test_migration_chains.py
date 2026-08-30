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
    "cc": (REPO / "services/central_command", "HARKEN_CC_DSN", "0010"),
    "sm": (REPO / "services/site_manager", "HARKEN_SM_DSN", "0008"),
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
        # E0.2: the authoritative site identity and per-site budgets.
        assert {"cc_site_id", "status", "bound_at"} <= _columns(db, "sites")
        assert "site_id" in _columns(db, "sm_error_budgets")


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
        assert _version(db) == SERVICES["cc"][2]
        assert "cc_approval_records" in _tables(db)
        assert "principal_ref" in _columns(db, "cc_approval_group_members")

    def test_cc_upgrade_from_0009_backfills_one_root_per_tenant(self, tmp_path):
        """E1.1 lands on a database with sites and no tree.

        The backfill is what makes "every site has one canonical
        organizational path" true for existing installs rather than only
        for new ones, so it is asserted here and not merely written.
        """
        db = tmp_path / "cc.db"
        _alembic("cc", db, "upgrade", "head")
        con = sqlite3.connect(db)
        con.execute("drop table cc_org_units")
        # sqlite refuses to drop a column that carries a foreign key, so
        # the pre-E1.1 `cc_sites` is reconstructed rather than altered.
        # `legacy_alter_table` keeps the rename from rewriting the other
        # tables that reference cc_sites.
        con.execute("pragma legacy_alter_table=ON")
        cols = [
            r[1] for r in con.execute("pragma table_info(cc_sites)").fetchall()
            if r[1] != "org_unit_id"
        ]
        con.execute("drop index if exists ix_cc_sites_org_unit_id")
        con.execute(
            "create table cc_sites_legacy as select "
            + ", ".join(cols)
            + " from cc_sites"
        )
        con.execute("drop table cc_sites")
        con.execute("alter table cc_sites_legacy rename to cc_sites")
        for tenant, name in (("t-a", "site-1"), ("t-a", "site-2"), ("t-b", "site-3")):
            con.execute(
                "insert into cc_sites (id, tenant_id, site_name, sm_endpoint, "
                "license_fingerprint, status) values (?, ?, ?, '', '', 'active')",
                (name + tenant, tenant, name),
            )
        con.execute("update alembic_version set version_num='0009'")
        con.commit()
        con.close()

        _alembic("cc", db, "upgrade", "head")
        assert _version(db) == SERVICES["cc"][2]
        assert "cc_org_units" in _tables(db)
        assert "org_unit_id" in _columns(db, "cc_sites")

        con = sqlite3.connect(db)
        roots = con.execute(
            "select tenant_id, id, path, depth from cc_org_units "
            "where parent_id is null order by tenant_id"
        ).fetchall()
        # One root each, not one shared root: tenants never share a tree.
        assert [r[0] for r in roots] == ["t-a", "t-b"]
        for tenant_id, unit_id, path, depth in roots:
            assert path == f"/{unit_id}/"
            assert depth == 1

        rows = con.execute(
            "select tenant_id, org_unit_id from cc_sites order by site_name"
        ).fetchall()
        con.close()
        # Every site attached, and attached within its OWN tenant's root.
        by_tenant = {t: u for t, u in roots and [(r[0], r[1]) for r in roots]}
        assert all(unit is not None for _, unit in rows)
        assert all(unit == by_tenant[tenant] for tenant, unit in rows)

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

    def test_sm_upgrade_from_0007_preserves_drop_backs(self, tmp_path):
        """E0.2 rekeys sm_error_budgets. A withdrawal that is lost would
        restore autonomy nobody reviewed, so the backfill must carry it."""
        db = tmp_path / "sm.db"
        _alembic("sm", db, "upgrade", "head")
        con = sqlite3.connect(db)
        # Rebuild the pre-E0.2 shape with one site and one dropped-back row.
        con.execute("drop table sm_error_budgets")
        con.execute(
            "create table sm_error_budgets (action_type text primary key, "
            "success_count integer, failure_count integer, total_count integer, "
            "min_success_rate real, dropped_back boolean, "
            "dropped_back_at datetime, updated_at datetime)"
        )
        con.execute(
            "insert into sm_error_budgets values "
            "('SEL_CLEAR', 1, 19, 20, 0.95, 1, null, null)"
        )
        con.execute(
            "insert into sites (id, name, status, created_at) "
            "values ('s1', 'alpha', 'active', '2026-08-30 00:00:00')"
        )
        con.execute("update alembic_version set version_num='0007'")
        con.commit()
        con.close()

        _alembic("sm", db, "upgrade", "head")
        con = sqlite3.connect(db)
        try:
            rows = con.execute(
                "select site_id, action_type, dropped_back from sm_error_budgets"
            ).fetchall()
        finally:
            con.close()
        assert rows == [("s1", "SEL_CLEAR", 1)], (
            "the drop-back was not carried onto the site"
        )

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
        # Reaches HEAD from a legacy stamp: every guarded migration in
        # between must be a no-op on objects that already exist.
        assert _version(db) == SERVICES["sm"][2]
        assert {"actor", "authorization_basis", "proposal_id"} <= _columns(
            db, "sm_directives"
        )
