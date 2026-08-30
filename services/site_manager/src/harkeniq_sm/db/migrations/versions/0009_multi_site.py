"""E1.3: one Site Manager, many sites -- on the WRITE path too.

E0.2 made every Central Command-facing read resolve to one authoritative
site. Every write, and all twenty of this service's own endpoints, still
resolved a single name from the config file -- so two sites on one Site
Manager would have put every device from both into one site row, and the
E0.2 reads would then have scoped perfectly to a set that was already
wrong.

  site_enrollment_tokens        site-bound, revocable, hash-only.
                                The site is what the SM KNOWS, never
                                what an agent claims (ratified D1).
  sm_stop_switches              per site, and one Site Manager-wide
                                emergency row. Persisted -- the old
                                switch was an in-memory boolean that a
                                restart silently cleared (ratified D2).
  agent_identities.site_id      an identity is issued FOR a site
  sm_candidate_skills.site_id   which site's diagnosis produced it
  audit_log.site_id             OUTSIDE the hash payload, exactly as
                                CC's E1.2 column: adding a column the
                                payload does not name leaves every
                                existing chain verifiable

Backfill is unambiguous by construction: a Site Manager that predates
E1.3 has exactly one site, so every row attaches to the only site there
is. An upgrading single-site deployment sees no behaviour change, and
agents keep registering without a credential because one site is not
ambiguous.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-30
"""

from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 is a create_all from CURRENT models, so a fresh database is born
    # with these objects; only pre-E1.3 databases need the DDL.
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("site_enrollment_tokens"):
        op.create_table(
            "site_enrollment_tokens",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("site_id", sa.String(32), nullable=False),
            # Unique: one secret can never resolve to two sites, which is
            # the ambiguity this table exists to remove.
            sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
            sa.Column("label", sa.String(255), server_default=""),
            sa.Column("issued_by", sa.String(255), server_default=""),
            sa.Column("issued_at", sa.DateTime(timezone=True)),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked_by", sa.String(255), server_default=""),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("use_count", sa.Integer(), server_default="0"),
        )
        op.create_index(
            "ix_site_enrollment_tokens_site_id",
            "site_enrollment_tokens", ["site_id"],
        )
        op.create_index(
            "ix_site_enrollment_tokens_token_hash",
            "site_enrollment_tokens", ["token_hash"],
        )

    if not inspector.has_table("sm_stop_switches"):
        op.create_table(
            "sm_stop_switches",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("scope", sa.String(16), server_default="site"),
            sa.Column("site_id", sa.String(32), nullable=True),
            sa.Column("active", sa.Boolean(), server_default=sa.false()),
            sa.Column("activated_by", sa.String(255), server_default=""),
            sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("deactivated_by", sa.String(255), server_default=""),
            sa.Column(
                "deactivated_at", sa.DateTime(timezone=True), nullable=True
            ),
            sa.Column("reason", sa.String(512), server_default=""),
            sa.UniqueConstraint(
                "scope", "site_id", name="uq_stop_switch_scope_site"
            ),
        )
        op.create_index(
            "ix_sm_stop_switches_site_id", "sm_stop_switches", ["site_id"]
        )

    # sqlite cannot ADD COLUMN carrying a foreign key (it needs a table
    # rebuild), so the constraint is declared on PostgreSQL -- where
    # production runs -- and the column is plain on the sqlite used by
    # tests. The models carry the FK either way, so the ORM is identical.
    postgres = bind.dialect.name == "postgresql"

    def add_site_column(table: str, fk: bool = True) -> None:
        columns = {c["name"] for c in inspector.get_columns(table)}
        if "site_id" in columns:
            return
        args = [sa.ForeignKey("sites.id")] if (fk and postgres) else []
        op.add_column(table, sa.Column("site_id", sa.String(32), *args,
                                       nullable=True))
        op.create_index(f"ix_{table}_site_id", table, ["site_id"])

    add_site_column("agent_identities")
    add_site_column("sm_candidate_skills")
    # audit_log.site_id is metadata, not a relationship: an audit entry
    # must survive its site being deleted, so no foreign key.
    add_site_column("audit_log", fk=False)

    _backfill(bind)


def _backfill(bind) -> None:
    """Attach existing rows to the only site there is.

    Re-runnable: only rows with a NULL site are touched, and a Site
    Manager with zero or several sites is left alone rather than guessed
    at. A pre-E1.3 deployment always has exactly one.
    """
    sites = bind.execute(
        sa.text("SELECT id FROM sites WHERE status = 'active'")
    ).fetchall()
    if len(sites) != 1:
        return
    site_id = sites[0][0]

    for table in ("agent_identities", "sm_candidate_skills", "audit_log"):
        bind.execute(
            sa.text(f"UPDATE {table} SET site_id = :s WHERE site_id IS NULL"),
            {"s": site_id},
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table in ("audit_log", "sm_candidate_skills", "agent_identities"):
        columns = {c["name"] for c in inspector.get_columns(table)}
        if "site_id" in columns:
            op.drop_index(f"ix_{table}_site_id", table)
            op.drop_column(table, "site_id")
    if inspector.has_table("sm_stop_switches"):
        op.drop_table("sm_stop_switches")
    if inspector.has_table("site_enrollment_tokens"):
        op.drop_table("site_enrollment_tokens")
