"""A6-1 (A24): the external ingress ledger.

  cc_agent_submissions  <- one row per external submission, accepted or
                           refused, with the replay key that makes a
                           retry safe

PURELY ADDITIVE. One new table, no column added to and no semantics
changed on any existing governance table. In particular `dedupe_key` on
`cc_agent_proposals` is left exactly as it is: A24.6's logical-duplicate
guarantee is an admission lock, not a retroactive constraint, and design
section 28 records why (the column defaults to "", its key shape changed
at A5, and a migration that fails on historical rows to surface a
historical bug is the wrong trade against a customer's upgrade).

NO BACKFILL. There is no historical submission to invent. An absent row
means this agent has never submitted, which is the truth on every
database that upgrades into A6.

THE UNIQUE CONSTRAINT IS THE PRODUCT. Idempotency is not a convention
maintained by the route; it is the constraint below. Two concurrent
identical submissions race to insert, exactly one wins, and the loser
reads the winner's row. Creating this table without
`uq_agent_submission_key` would leave the route's replay handling as a
best-effort read-then-write, which is precisely the race it exists to
close.

Idempotence is mandatory for every additive migration in this chain.

Revision ID: 0022
Revises: 0021
Create Date: 2026-09-05
"""

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

_TABLE = "cc_agent_submissions"


def _has_table(bind) -> bool:
    return sa.inspect(bind).has_table(_TABLE)


def upgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind):
        return

    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        # A24.5: derived from the machine token, never from a body.
        sa.Column("agent_id", sa.String(32), nullable=False),
        sa.Column("agent_version", sa.Integer(), nullable=False,
                  server_default="1"),
        # 128 per the identity-width invariant for a principal-bearing
        # column: sqlite ignores VARCHAR length, so a narrow column
        # passes every unit test and truncates on PostgreSQL.
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False,
                  server_default=""),
        sa.Column("candidate_ref", sa.String(128), nullable=False,
                  server_default=""),
        # NULL means refused. Absent is not an unknown proposal; it is no
        # proposal.
        sa.Column("proposal_id", sa.String(32), nullable=True),
        sa.Column("code", sa.String(64), nullable=False, server_default=""),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "tenant_id", "agent_id", "idempotency_key",
            name="uq_agent_submission_key",
        ),
    )
    op.create_index(
        "ix_cc_agent_submissions_tenant_id", _TABLE, ["tenant_id"]
    )
    op.create_index(
        "ix_cc_agent_submissions_agent_id", _TABLE, ["agent_id"]
    )
    op.create_index(
        "ix_cc_agent_submissions_created_at", _TABLE, ["created_at"]
    )
    # The durable rate window (A24.8): counting committed rows is the
    # only per-identity control that is correct on a multi-replica CC.
    op.create_index(
        "ix_agent_submissions_rate", _TABLE,
        ["tenant_id", "agent_id", "created_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not _has_table(bind):
        return
    # Drops only what this revision created. No other table is touched,
    # so a downgrade cannot lose a proposal, an approval or an audit row.
    op.drop_table(_TABLE)
