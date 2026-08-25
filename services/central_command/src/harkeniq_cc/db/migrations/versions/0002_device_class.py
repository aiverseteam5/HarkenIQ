"""R6-P7: cc_fleet_cache.device_class ("server" | "switch").

Additive with a server default; pre-R6 snapshots and pre-R6 SMs (which
send no device_class) keep their existing meaning.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-25
"""

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cc_fleet_cache",
        sa.Column(
            "device_class", sa.String(32), nullable=False,
            server_default="server",
        ),
    )


def downgrade() -> None:
    op.drop_column("cc_fleet_cache", "device_class")
