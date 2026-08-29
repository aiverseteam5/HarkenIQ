"""S5: cc_safety_state — live autonomy safety state at Central Command.

Suppression and error-budget drop-back are the two mechanisms that
withdraw autonomy without a human. Both lived only inside the Site
Manager, behind its site-token break-glass API, so a demotion that had
already happened was invisible to the tenant operator and to any future
agent. This table is where the fleet snapshot's safety state lands so
`/api/autonomy` can fold it into each action class's disposition.

One row per site, replaced on each poll. A site that reported nothing is
stored with reported=False and rendered as UNKNOWN, never as safe.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-29
"""

from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 is a create_all from CURRENT models, so a fresh database is born
    # with this table — only pre-S5 databases need the CREATE. Idempotence
    # is mandatory for every additive migration in this chain.
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("cc_safety_state"):
        return
    op.create_table(
        "cc_safety_state",
        sa.Column("site_id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("reported", sa.Boolean(), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sm_stop_switch", sa.Boolean(), nullable=False),
        sa.Column("suppressions", sa.JSON(), nullable=True),
        sa.Column("error_budgets", sa.JSON(), nullable=True),
        sa.Column("site_budgets", sa.JSON(), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_cc_safety_tenant", "cc_safety_state", ["tenant_id"])


def downgrade() -> None:
    op.drop_table("cc_safety_state")
