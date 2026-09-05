"""A6-1 (A24): the external ingress ledger.

  cc_agent_submissions       <- one row per external submission, accepted
                                or refused, with the replay key that makes
                                a retry safe
  cc_agent_ingress_attempts  <- one row per external ATTEMPT, which is what
                                a rate control can actually meter
  cc_agent_proposals.operation_key
                             <- the same physical operation, independent of
                                which agent proposed it (A24.12)

PURELY ADDITIVE. Two new tables and one nullable column; no semantics
changed on any existing governance table. In particular `dedupe_key` on
`cc_agent_proposals` is left exactly as it is: A24.6's logical-duplicate
guarantee is an admission lock, not a retroactive constraint, and design
section 28 records why (the column defaults to "", its key shape changed
at A5, and a migration that fails on historical rows to surface a
historical bug is the wrong trade against a customer's upgrade).

NO BACKFILL, anywhere. There is no historical submission or attempt to
invent, and `operation_key` stays NULL on every existing proposal: a
historical proposal predates the concept, and computing one now would
assert an equivalence between old rows that nobody ever checked. NULL
means "not comparable", and the collision rule below skips it.

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
_ATTEMPTS = "cc_agent_ingress_attempts"


def _has_table(bind) -> bool:
    return sa.inspect(bind).has_table(_TABLE)


def upgrade() -> None:
    bind = op.get_bind()
    _upgrade_attempts(bind)
    _upgrade_operation_key(bind)
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


def _upgrade_attempts(bind) -> None:
    if sa.inspect(bind).has_table(_ATTEMPTS):
        return
    op.create_table(
        _ATTEMPTS,
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("agent_id", sa.String(32), nullable=False),
        sa.Column("outcome", sa.String(24), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_cc_agent_ingress_attempts_tenant_id", _ATTEMPTS, ["tenant_id"]
    )
    op.create_index(
        "ix_cc_agent_ingress_attempts_agent_id", _ATTEMPTS, ["agent_id"]
    )
    op.create_index(
        "ix_cc_agent_ingress_attempts_created_at", _ATTEMPTS, ["created_at"]
    )
    op.create_index(
        "ix_ingress_attempts_window", _ATTEMPTS,
        ["tenant_id", "agent_id", "created_at"],
    )


def _upgrade_operation_key(bind) -> None:
    cols = {c["name"] for c in sa.inspect(bind).get_columns("cc_agent_proposals")}
    if "operation_key" in cols:
        return
    op.add_column(
        "cc_agent_proposals",
        sa.Column("operation_key", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_cc_agent_proposals_operation_key",
        "cc_agent_proposals", ["operation_key"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    # Drops only what this revision created. No other table is touched, so
    # a downgrade cannot lose a proposal, an approval or an audit row --
    # though it DOES discard the external submission and attempt ledgers,
    # which are this revision's own records and exist nowhere else.
    if "operation_key" in {
        c["name"] for c in inspector.get_columns("cc_agent_proposals")
    }:
        op.drop_index(
            "ix_cc_agent_proposals_operation_key",
            table_name="cc_agent_proposals",
        )
        op.drop_column("cc_agent_proposals", "operation_key")
    if inspector.has_table(_ATTEMPTS):
        op.drop_table(_ATTEMPTS)
    if inspector.has_table(_TABLE):
        op.drop_table(_TABLE)
