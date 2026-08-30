"""E0.2: authoritative CC site identity, and per-site error budgets.

Two problems, one slice.

**Site identity was never persisted.** `RegisterSite` received Central
Command's `site_id` and logged it. `GetFleetSnapshot` then queried the
Site Manager's own `sites.id` primary keys with it -- different id
spaces, never a match -- and fell through to "list all devices". Four
further reads (incidents, pending actions, outcomes, candidate skills)
were never site-scoped at all, and the outcome and candidate watermarks
meant one site's poll CONSUMED another site's rows.

  sites.cc_site_id   Central Command's identity, unique, never
                     overwritten by a registration
  sites.status       active | retired; a site belongs to exactly one
                     ACTIVE Site Manager
  sites.bound_at     when the binding was made

**Error budgets had no site.** `sm_error_budgets` was keyed by
`action_type` alone, so on a Site Manager serving several sites a
failure pattern at one site would withdraw autonomy at every other.
Autonomy is earned on evidence, and one site's evidence is not
another's. The key becomes `(site_id, action_type)`.

Backfill is deliberately conservative. Existing rows are attributed to
the Site Manager's single site, which is the only shape that has ever
existed in practice. If somehow several sites are present the rows are
COPIED to each, because losing a drop-back would restore autonomy nobody
reviewed -- the unsafe direction.

Binding itself is not backfilled: Central Command assigns the id, so it
re-registers on its next poll and binds authoritatively. Guessing here
would be exactly the kind of adoption heuristic this slice exists to
remove.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-30
"""

from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    site_cols = {c["name"] for c in inspector.get_columns("sites")}
    if "cc_site_id" not in site_cols:
        op.add_column("sites", sa.Column("cc_site_id", sa.String(32), nullable=True))
        op.create_index(
            "uq_sites_cc_site_id", "sites", ["cc_site_id"], unique=True,
        )
    if "status" not in site_cols:
        op.add_column(
            "sites", sa.Column("status", sa.String(16), server_default="active"),
        )
    if "bound_at" not in site_cols:
        op.add_column(
            "sites", sa.Column("bound_at", sa.DateTime(timezone=True), nullable=True),
        )

    budget_cols = {c["name"] for c in inspector.get_columns("sm_error_budgets")}
    if "site_id" in budget_cols:
        return

    site_ids = [
        row[0] for row in bind.execute(sa.text("SELECT id FROM sites")).fetchall()
    ]
    existing = bind.execute(
        sa.text(
            "SELECT action_type, success_count, failure_count, total_count, "
            "min_success_rate, dropped_back, dropped_back_at, updated_at "
            "FROM sm_error_budgets"
        )
    ).fetchall()

    # A composite primary key change: rebuild the table. batch_alter_table
    # copies on sqlite and issues real DDL on PostgreSQL.
    op.drop_table("sm_error_budgets")
    op.create_table(
        "sm_error_budgets",
        sa.Column(
            "site_id", sa.String(32), sa.ForeignKey("sites.id"), primary_key=True,
        ),
        sa.Column("action_type", sa.String(64), primary_key=True),
        sa.Column("success_count", sa.Integer(), server_default="0"),
        sa.Column("failure_count", sa.Integer(), server_default="0"),
        sa.Column("total_count", sa.Integer(), server_default="0"),
        sa.Column("min_success_rate", sa.Float(), server_default="0.95"),
        sa.Column("dropped_back", sa.Boolean(), server_default=sa.false()),
        sa.Column("dropped_back_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    if existing and site_ids:
        table = sa.table(
            "sm_error_budgets",
            sa.column("site_id"), sa.column("action_type"),
            sa.column("success_count"), sa.column("failure_count"),
            sa.column("total_count"), sa.column("min_success_rate"),
            sa.column("dropped_back"), sa.column("dropped_back_at"),
            sa.column("updated_at"),
        )
        op.bulk_insert(table, [
            {
                "site_id": site_id,
                "action_type": row[0],
                "success_count": row[1],
                "failure_count": row[2],
                "total_count": row[3],
                "min_success_rate": row[4],
                "dropped_back": row[5],
                "dropped_back_at": row[6],
                "updated_at": row[7],
            }
            for site_id in site_ids
            for row in existing
        ])


def downgrade() -> None:
    op.drop_table("sm_error_budgets")
    op.create_table(
        "sm_error_budgets",
        sa.Column("action_type", sa.String(64), primary_key=True),
        sa.Column("success_count", sa.Integer(), server_default="0"),
        sa.Column("failure_count", sa.Integer(), server_default="0"),
        sa.Column("total_count", sa.Integer(), server_default="0"),
        sa.Column("min_success_rate", sa.Float(), server_default="0.95"),
        sa.Column("dropped_back", sa.Boolean(), server_default=sa.false()),
        sa.Column("dropped_back_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    op.drop_index("uq_sites_cc_site_id", "sites")
    op.drop_column("sites", "bound_at")
    op.drop_column("sites", "status")
    op.drop_column("sites", "cc_site_id")
