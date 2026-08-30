"""A1: attribution and authorization basis on the execution path.

Three columns, one defect fix behind them.

  sm_directives.actor          who this directive runs for
  sm_directives.authorization_basis  human_approval | autonomous_grant
  sm_directives.proposal_id    the CC proposal it settles
  sm_action_outcomes.actor     attribution on the evidence path

`authorization_basis` is load-bearing, not decoration. A directive delivered
with `autonomous_grant` carries no human decision, so the node may not
treat an authorization-shaped lease refusal ("propose", i.e. the S5
error-budget drop-back) as already satisfied. Before this column every
directive looked pre-approved to the node, which would have let an
agent keep acting through a drop-back that exists precisely to stop it.

`actor` on sm_action_outcomes is the second half of the attribution
chain: it rides FleetOutcome up to Central Command so an execution is
still attributable once it has become evidence.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-30
"""

from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 is a create_all from CURRENT models, so a fresh database is
    # born with these columns; only pre-A1 databases need the ALTER.
    inspector = sa.inspect(op.get_bind())
    directive_cols = {c["name"] for c in inspector.get_columns("sm_directives")}
    for name, col in (
        ("actor", sa.Column("actor", sa.String(255), server_default="")),
        ("authorization_basis",
         sa.Column("authorization_basis", sa.String(32), server_default="")),
        ("proposal_id",
         sa.Column("proposal_id", sa.String(64), server_default="")),
    ):
        if name not in directive_cols:
            op.add_column("sm_directives", col)
    outcome_cols = {
        c["name"] for c in inspector.get_columns("sm_action_outcomes")
    }
    if "actor" not in outcome_cols:
        op.add_column(
            "sm_action_outcomes",
            sa.Column("actor", sa.String(255), server_default=""),
        )


def downgrade() -> None:
    op.drop_column("sm_action_outcomes", "actor")
    op.drop_column("sm_directives", "proposal_id")
    op.drop_column("sm_directives", "authorization_basis")
    op.drop_column("sm_directives", "actor")
