"""A6-2 (A25.6): status-read pressure, counted in its own bucket.

  cc_agent_read_windows  <- one row per (tenant, agent, window), a counter

PURELY ADDITIVE. One new table. Nothing existing is altered, and in
particular `cc_agent_ingress_attempts` is untouched: A25.6 requires read
traffic to be accounted SEPARATELY from governed submission attempts, so
that abuse detection, tenant and per-agent quotas, capacity management and
entitlements can later tell a poll from an action.

WHY A COUNTER AND NOT ANOTHER LEDGER. The submission ledger records one
row per attempt, which is right for traffic that is rare and individually
meaningful. Polling is neither. A row per GET would make the meter the
largest writer in the system, so a read is counted in place: one row per
fixed window, incremented atomically.

THE UNIQUE CONSTRAINT IS THE MECHANISM, not decoration. Two replicas will
race to open the same window; the constraint is what makes exactly one of
them win, after which the loser takes the ordinary UPDATE path.

NO BACKFILL. There is no historical read to invent, and an absent window
correctly means no reads were served in it.

This is the one migration A6-2 needs. The exact-correlation work that
leads the slice required none: `cc_outcome_history.action_id` already
existed and was already populated.

Idempotence is mandatory for every additive migration in this chain.

Revision ID: 0023
Revises: 0022
Create Date: 2026-09-05
"""

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

_TABLE = "cc_agent_read_windows"


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table(_TABLE):
        return

    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        # A25.5: the agent the TOKEN names. Charging the bucket to the
        # authenticated principal is what stops a caller shifting its
        # cost onto the agent it names in a path.
        sa.Column("agent_id", sa.String(32), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reads", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint(
            "tenant_id", "agent_id", "window_start",
            name="uq_agent_read_window",
        ),
    )
    op.create_index(
        "ix_cc_agent_read_windows_tenant_id", _TABLE, ["tenant_id"]
    )
    op.create_index(
        "ix_cc_agent_read_windows_agent_id", _TABLE, ["agent_id"]
    )
    op.create_index(
        "ix_cc_agent_read_windows_window_start", _TABLE, ["window_start"]
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(_TABLE):
        return
    # Drops only what this revision created. A downgrade loses read
    # accounting for the current windows and nothing else -- no proposal,
    # no submission, no outcome and no audit row is touched.
    op.drop_table(_TABLE)
