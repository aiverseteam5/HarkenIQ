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
    # 0001 is a create_all from CURRENT models — fresh databases already
    # have the column; only pre-R6 databases need the ALTER (same live
    # finding as the SM 0002).
    inspector = sa.inspect(op.get_bind())
    columns = {c["name"] for c in inspector.get_columns("cc_fleet_cache")}
    if "device_class" in columns:
        return
    op.add_column(
        "cc_fleet_cache",
        sa.Column(
            "device_class", sa.String(32), nullable=False,
            server_default="server",
        ),
    )


def downgrade() -> None:
    op.drop_column("cc_fleet_cache", "device_class")
