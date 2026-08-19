"""Initial R2a schema.

Creates all Site Manager tables from the declarative metadata (single
source of truth: harkeniq_sm.db.models), then promotes the telemetry
tables to Timescale hypertables when the extension is available. On
plain PostgreSQL (or sqlite) the tables stay ordinary — one migration
path for all three deployment shapes.

Revision ID: 0001
Revises:
Create Date: 2026-08-19
"""

from alembic import op
import sqlalchemy as sa

from harkeniq_sm.db.models import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_HYPERTABLES = ("verdict_reports", "heartbeats")


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)

    if bind.dialect.name != "postgresql":
        return
    has_timescale = bind.execute(
        sa.text("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")
    ).scalar()
    if not has_timescale:
        return
    for table in _HYPERTABLES:
        bind.execute(
            sa.text(
                f"SELECT create_hypertable('{table}', 'time', "
                "if_not_exists => TRUE, migrate_data => TRUE)"
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
