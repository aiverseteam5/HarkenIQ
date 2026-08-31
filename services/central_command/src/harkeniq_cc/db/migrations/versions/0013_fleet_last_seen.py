"""cc_fleet_cache.last_seen_at — when the SITE last saw the agent.

The Site Manager has always sent ``FleetDevice.last_seen_unix`` and
``SMClient`` has always dictified it, but the fleet poller dropped it and
the cache had nowhere to put it, so ``/api/fleet/{id}`` served
``snapshot_at`` — CC's own cache-refresh time — under the name
``last_seen_at``. A silent agent therefore looked fresh on every poll.

Nullable on purpose: rows cached before this migration have no honest
value, and backfilling them from ``snapshot_at`` would restate the same
lie in a new column.

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-31
"""

from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 is a create_all from CURRENT models, so fresh databases are born
    # with the column and only existing ones need the ALTER (same guard as
    # 0002 and 0005).
    inspector = sa.inspect(op.get_bind())
    columns = {c["name"] for c in inspector.get_columns("cc_fleet_cache")}
    if "last_seen_at" in columns:
        return
    op.add_column(
        "cc_fleet_cache",
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("cc_fleet_cache", "last_seen_at")
