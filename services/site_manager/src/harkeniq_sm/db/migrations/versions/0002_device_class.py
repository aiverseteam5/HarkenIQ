"""R6-P7: devices.device_class ("server" | "switch").

Additive column with a server default so every pre-R6 row (and every
registration from a pre-R6 agent, which sends no device_class) keeps its
existing meaning.

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
    # 0001 is a create_all from CURRENT models, so a fresh database is born
    # with this column already present — only pre-R6 databases need the
    # ALTER. Idempotence is mandatory for every additive migration in this
    # chain (found live: fresh full-stack boot crash-looped on the
    # duplicate column).
    inspector = sa.inspect(op.get_bind())
    columns = {c["name"] for c in inspector.get_columns("devices")}
    if "device_class" in columns:
        return
    op.add_column(
        "devices",
        sa.Column(
            "device_class", sa.String(32), nullable=False,
            server_default="server",
        ),
    )


def downgrade() -> None:
    op.drop_column("devices", "device_class")
