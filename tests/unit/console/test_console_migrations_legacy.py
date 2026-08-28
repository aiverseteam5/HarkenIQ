"""The legacy-upgrade path must be EXECUTED by CI, not asserted to exist.

Two review passes converged here: test_alembic_migrations.py only checks
that migration files exist (this repo's own 'exit-gates-must-boot-the-
artifact' anti-pattern), and every other test builds schema via
create_all, which takes 0003's fresh-DB early return. So the one branch
production actually takes on upgrade — a database with real legacy rows —
had zero execution coverage. This runs alembic for real against a
hand-built pre-0003 schema and asserts the backfill's promises.
"""

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

CONSOLE_DIR = Path(__file__).resolve().parents[3] / "services" / "console"


def _run_alembic(db_path: Path, target: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", target],
        cwd=CONSOLE_DIR,
        env={
            "HARKEN_CONSOLE_DSN": f"sqlite+aiosqlite:///{db_path}",
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": "",
        },
        check=True,
        capture_output=True,
    )


@pytest.fixture
def legacy_db(tmp_path):
    """A database stamped at 0001, hand-stripped to the pre-0003 shape,
    carrying one live grant and one revoked one."""
    db = tmp_path / "legacy.db"
    _run_alembic(db, "0001")
    conn = sqlite3.connect(db)
    conn.execute("DROP INDEX IF EXISTS uq_support_access_pending")
    conn.execute("DROP INDEX IF EXISTS ix_support_access_log_status")
    conn.execute("DROP INDEX IF EXISTS uq_tenant_services_endpoint_active")
    conn.execute("DROP INDEX IF EXISTS uq_tenant_services_active")
    conn.execute("DROP INDEX IF EXISTS ix_tenant_services_tenant")
    conn.execute("DROP TABLE IF EXISTS tenant_services")
    for col in ("status", "requested_by", "requested_at", "reason",
                "approved_by", "approved_at", "denied_by", "denied_at"):
        conn.execute(f"ALTER TABLE support_access_log DROP COLUMN {col}")
    conn.execute(
        "INSERT INTO support_access_log"
        " (id, tenant_id, enabled_by, enabled_at, expires_at)"
        " VALUES ('live1', 't1', 'eng-a', '2026-08-28 00:00:00',"
        "         '2099-01-01 00:00:00')"
    )
    conn.execute(
        "INSERT INTO support_access_log"
        " (id, tenant_id, enabled_by, enabled_at, expires_at, revoked_at)"
        " VALUES ('gone1', 't2', 'eng-b', '2026-08-01 00:00:00',"
        "         '2026-08-02 00:00:00', '2026-08-01 12:00:00')"
    )
    conn.commit()
    conn.close()
    return db


class TestLegacyUpgradePath:
    def test_live_grant_survives_and_binds_to_its_engineer(self, legacy_db):
        """The migration's whole promise: a grant that was live under the
        old rules keeps working after the deploy — as ITS holder's."""
        _run_alembic(legacy_db, "head")
        conn = sqlite3.connect(legacy_db)
        rows = dict(
            conn.execute(
                "SELECT id, status FROM support_access_log"
            ).fetchall()
        )
        assert rows == {"live1": "approved", "gone1": "revoked"}
        # requester binding: backfilled requested_by carries the holder
        by = dict(conn.execute(
            "SELECT id, requested_by FROM support_access_log").fetchall())
        assert by == {"live1": "eng-a", "gone1": "eng-b"}
        # tenant_services arrived with both partial unique indexes
        idx = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
            " AND tbl_name='tenant_services'")}
        assert {"uq_tenant_services_active",
                "uq_tenant_services_endpoint_active"} <= idx
        conn.close()

    def test_upgrade_is_rerunnable_without_clobbering(self, legacy_db):
        """A second head run must not re-backfill (a denied row must never
        resurrect as approved — data-migration finding)."""
        _run_alembic(legacy_db, "head")
        conn = sqlite3.connect(legacy_db)
        conn.execute(
            "UPDATE support_access_log SET status='denied' WHERE id='live1'"
        )
        conn.commit()
        conn.close()
        _run_alembic(legacy_db, "head")  # no-op: already at head
        conn = sqlite3.connect(legacy_db)
        status = conn.execute(
            "SELECT status FROM support_access_log WHERE id='live1'"
        ).fetchone()[0]
        assert status == "denied"
        conn.close()

    def test_fresh_database_converges_to_the_same_shape(self, tmp_path, legacy_db):
        """Migrated and fresh schemas must agree on NOT NULL (the
        schema-drift finding: requested_at was left nullable on upgrade)."""
        fresh = tmp_path / "fresh.db"
        _run_alembic(fresh, "head")
        _run_alembic(legacy_db, "head")

        def shape_map(db, table):
            conn = sqlite3.connect(db)
            # name -> (declared type incl. width, notnull) — widths matter:
            # the String(32)-vs-Keycloak-subject class shipped because
            # nothing compared migrated and fresh schemas.
            cols = {
                r[1]: (r[2], r[3])
                for r in conn.execute(f"PRAGMA table_info({table})")
            }
            conn.close()
            return cols

        for table in ("support_access_log", "tenant_services",
                      "console_audit_log", "api_keys", "licenses"):
            assert shape_map(fresh, table) == shape_map(legacy_db, table), (
                f"migrated and fresh schemas diverge on {table}"
            )
