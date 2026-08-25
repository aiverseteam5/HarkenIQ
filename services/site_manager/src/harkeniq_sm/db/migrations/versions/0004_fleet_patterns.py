"""QA-033 (R-C1): sm_fleet_patterns — CC-pushed fleet knowledge.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-25
"""

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 is a create_all from CURRENT models, so a fresh database is
    # born with this table — only older databases need the CREATE.
    # Idempotence is mandatory for every additive migration in this chain.
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("sm_fleet_patterns"):
        return
    op.create_table(
        "sm_fleet_patterns",
        sa.Column("pattern_id", sa.String(64), primary_key=True),
        sa.Column("pattern_type", sa.String(64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("affected_scope", sa.JSON(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("detected_at", sa.String(64), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("sm_fleet_patterns")
