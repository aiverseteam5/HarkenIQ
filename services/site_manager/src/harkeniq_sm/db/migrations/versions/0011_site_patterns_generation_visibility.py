"""A30.29: per-site pattern store + generation provenance on candidates.

  sm_site_fleet_patterns             a fleet pattern as pushed to ONE site,
                                     keyed (site_id, pattern_id), with the
                                     projection marker as pushed
  sm_candidate_skills
    .generation_visibility           the projection boundary a candidate's
                                     YAML was generated from

`sm_fleet_patterns` was keyed by pattern id alone, so on a multi-site Site
Manager the last push won and site A's next diagnosis was generated from
site B's projection. The old table is NOT rewritten, NOT migrated into the
new one and NOT dropped: its rows carry no site and no marker, and
inventing either would be a fabricated backfill. The runtime reads them
once at boot as unmarked (tenant-unbounded) evidence, and each site's own
rows supersede them as Central Command re-pushes.

`generation_visibility` is NULLABLE with NO backfill and NO default. A
candidate written before this migration reads UNKNOWN, which Central
Command withholds from every scoped reader; a tenant-wide reader keeps
the stored YAML. Unknown is not "site-safe", and it is not "tenant" either
-- it is a fact the writer never recorded, and nothing here pretends
otherwise.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-22
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

JSONVariant = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    # 0001 is a create_all from CURRENT models, so a fresh database is
    # born with both objects; only an existing database needs them.
    # Idempotence is mandatory in this chain (see 0003).
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("sm_site_fleet_patterns"):
        op.create_table(
            "sm_site_fleet_patterns",
            sa.Column("site_id", sa.String(32), sa.ForeignKey("sites.id"), primary_key=True),
            sa.Column("pattern_id", sa.String(64), primary_key=True),
            sa.Column("pattern_type", sa.String(64), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("affected_scope", JSONVariant, nullable=True),
            sa.Column("confidence", sa.Float(), nullable=False),
            sa.Column("evidence", JSONVariant, nullable=True),
            sa.Column("detected_at", sa.String(64), nullable=False),
            sa.Column("visibility", JSONVariant, nullable=True),
            sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        )
    columns = {c["name"] for c in inspector.get_columns("sm_candidate_skills")}
    if "generation_visibility" not in columns:
        op.add_column(
            "sm_candidate_skills",
            sa.Column("generation_visibility", JSONVariant, nullable=True),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {c["name"] for c in inspector.get_columns("sm_candidate_skills")}
    if "generation_visibility" in columns:
        op.drop_column("sm_candidate_skills", "generation_visibility")
    if inspector.has_table("sm_site_fleet_patterns"):
        op.drop_table("sm_site_fleet_patterns")
