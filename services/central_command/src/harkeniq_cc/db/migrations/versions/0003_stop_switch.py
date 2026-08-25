"""QA-022 (R7-P3): cc_stop_switch table — persisted fleet-wide stop switch.

The stop switch was an in-process dict that vanished on CC restart and
never reached any SM or lease. It is now a per-tenant row, pushed to
Site Managers via PushPolicy.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-25
"""

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 is a create_all from CURRENT models, so a fresh database is
    # born with this table — only pre-R7 databases need the CREATE.
    # Idempotence is mandatory for every additive migration in this chain.
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("cc_stop_switch"):
        return
    op.create_table(
        "cc_stop_switch",
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("changed_by", sa.String(255), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("cc_stop_switch")
