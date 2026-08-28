"""QA-021 (R7-P3): sm_settings KV table — SM identity keypair persistence.

The lease-signing Ed25519 keypair must survive SM restarts (agents pin
the SM public key at registration); it lives in a small key-value table.

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
    if inspector.has_table("sm_settings"):
        return
    op.create_table(
        "sm_settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("sm_settings")
