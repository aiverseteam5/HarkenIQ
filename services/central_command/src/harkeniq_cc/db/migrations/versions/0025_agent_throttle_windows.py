"""A27.13: a bounded observation that a request was actually throttled.

A24.13 refuses an over-limit submission WITHOUT writing, so the traffic
a rate limit exists to bound cannot grow the table that bounds it. That
is still right -- and it left `throttled` structurally zero forever, so
A27.11's `throttled` state was unreachable in production.

This table is the counter that closes the gap without reopening the
amplification hole: one row per (tenant, agent, aligned window), so a
flood writes one row and increments it. Bounded by TIME, not traffic.

Additive, with NO backfill: a deployment upgrading to this has no record
of what it refused before the counter existed, and inventing one would
be the same manufactured certainty A27.4 refuses.

Revision ID: 0025
Revises: 0024
"""

from alembic import op
import sqlalchemy as sa

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

_TABLE = "cc_agent_throttle_windows"


def _has_table(bind) -> bool:
    return _TABLE in sa.inspect(bind).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    # Guarded and idempotent, the way every migration since 0010 is.
    if _has_table(bind):
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("agent_id", sa.String(length=32), nullable=False),
        sa.Column(
            "window_start", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "rejected", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("last_at", sa.DateTime(timezone=True), nullable=True),
        # The constraint is what lets two replicas race to open the same
        # window and have exactly one of them win -- the same guarantee
        # `cc_agent_read_windows` stands on.
        sa.UniqueConstraint(
            "tenant_id", "agent_id", "window_start",
            name="uq_agent_throttle_window",
        ),
    )
    op.create_index(
        "ix_cc_agent_throttle_windows_tenant_id", _TABLE, ["tenant_id"]
    )
    op.create_index(
        "ix_cc_agent_throttle_windows_agent_id", _TABLE, ["agent_id"]
    )
    op.create_index(
        "ix_cc_agent_throttle_windows_window_start", _TABLE, ["window_start"]
    )
    # Deliberately NO backfill. See the module docstring.


def downgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind):
        return
    op.drop_table(_TABLE)
