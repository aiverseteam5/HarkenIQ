"""A0+A1: the Operational Agent — bundle, scope, capabilities, proposals.

The product noun finally becomes an object. An Operational Agent is a
declarative bundle over capabilities that already exist: identity,
explicit scope, capability bindings, and a policy that can only ever be
a subset of what the tenant itself is permitted.

Four tables plus one column:

  cc_operational_agents   the named, versioned, tenant-owned bundle
  cc_agent_scopes         explicit scope rows; no rows means no reach
  cc_agent_capabilities   references to governed capabilities that exist
  cc_agent_proposals      labelled, evidence-carrying proposals
  cc_outcome_history.actor  attribution on the evidence path

`actor` on the outcome history is what closes the loop: without it an
execution can be attributed to an agent right up to the moment it
becomes evidence, and then goes anonymous — so "what did my agent
actually do, and did it work" would have no answer.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-30
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

JSONVariant = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    # 0001 is a create_all from CURRENT models, so a fresh database is born
    # with these tables and this column; only pre-A0 databases need the
    # DDL. Idempotence is mandatory for every additive migration in this
    # chain (same pattern as 0002 and 0007).
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("cc_operational_agents"):
        _create_agent_tables()
    columns = {c["name"] for c in inspector.get_columns("cc_outcome_history")}
    if "actor" not in columns:
        op.add_column(
            "cc_outcome_history",
            sa.Column("actor", sa.String(255), server_default=""),
        )


def _create_agent_tables() -> None:
    op.create_table(
        "cc_operational_agents",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.String(512), server_default=""),
        sa.Column("status", sa.String(16), server_default="draft"),
        sa.Column("version", sa.Integer(), server_default="1"),
        sa.Column("autonomy_ceiling", sa.Integer(), server_default="0"),
        sa.Column(
            "require_approval_always", sa.Boolean(), server_default=sa.true()
        ),
        sa.Column("max_proposals_per_day", sa.Integer(), server_default="25"),
        sa.Column("created_by", sa.String(255), server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_by", sa.String(255), server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.Column("activated_by", sa.String(255), server_default=""),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "name", name="uq_op_agent_tenant_name"),
    )
    op.create_index(
        "ix_cc_operational_agents_tenant_id", "cc_operational_agents", ["tenant_id"]
    )

    op.create_table(
        "cc_agent_scopes",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "agent_id",
            sa.String(32),
            sa.ForeignKey("cc_operational_agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("scope_type", sa.String(16), nullable=False),
        sa.Column("scope_ref", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "agent_id", "scope_type", "scope_ref", name="uq_agent_scope"
        ),
    )
    op.create_index("ix_cc_agent_scopes_agent_id", "cc_agent_scopes", ["agent_id"])
    op.create_index("ix_cc_agent_scopes_tenant_id", "cc_agent_scopes", ["tenant_id"])

    op.create_table(
        "cc_agent_capabilities",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "agent_id",
            sa.String(32),
            sa.ForeignKey("cc_operational_agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("capability_ref", sa.String(128), nullable=False),
        sa.Column("config", JSONVariant, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "agent_id", "kind", "capability_ref", name="uq_agent_capability"
        ),
    )
    op.create_index(
        "ix_cc_agent_capabilities_agent_id", "cc_agent_capabilities", ["agent_id"]
    )
    op.create_index(
        "ix_cc_agent_capabilities_tenant_id", "cc_agent_capabilities", ["tenant_id"]
    )

    op.create_table(
        "cc_agent_proposals",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("agent_id", sa.String(32), nullable=False),
        sa.Column("actor", sa.String(255), server_default=""),
        sa.Column("agent_version", sa.Integer(), server_default="1"),
        sa.Column("site_id", sa.String(32), server_default=""),
        sa.Column("device_agent_id", sa.String(255), server_default=""),
        sa.Column("action_type", sa.String(64), nullable=False),
        sa.Column("params", JSONVariant, nullable=True),
        sa.Column("rationale", sa.Text(), server_default=""),
        sa.Column("evidence", JSONVariant, nullable=True),
        sa.Column("disposition", sa.String(32), server_default=""),
        sa.Column("disposition_reason", sa.Text(), server_default=""),
        sa.Column("blocking_conditions", JSONVariant, nullable=True),
        sa.Column("authorization_basis", sa.String(32), server_default=""),
        sa.Column("status", sa.String(24), server_default="proposed"),
        sa.Column("decided_by", sa.String(255), server_default=""),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dedupe_key", sa.String(255), server_default=""),
        sa.Column("directive_id", sa.String(64), server_default=""),
        sa.Column("dispatch_reason", sa.String(512), server_default=""),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(32), server_default=""),
        sa.Column("outcome_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_cc_agent_proposals_tenant_id", "cc_agent_proposals", ["tenant_id"]
    )
    op.create_index(
        "ix_cc_agent_proposals_agent_id", "cc_agent_proposals", ["agent_id"]
    )
    op.create_index(
        "ix_cc_agent_proposals_site_id", "cc_agent_proposals", ["site_id"]
    )
    op.create_index(
        "ix_cc_agent_proposals_dedupe_key", "cc_agent_proposals", ["dedupe_key"]
    )
    op.create_index(
        "ix_cc_agent_proposals_created_at", "cc_agent_proposals", ["created_at"]
    )
    op.create_index(
        "ix_agent_proposals_tenant_status",
        "cc_agent_proposals",
        ["tenant_id", "status"],
    )


def downgrade() -> None:
    op.drop_column("cc_outcome_history", "actor")
    op.drop_index("ix_agent_proposals_tenant_status", "cc_agent_proposals")
    op.drop_index("ix_cc_agent_proposals_created_at", "cc_agent_proposals")
    op.drop_index("ix_cc_agent_proposals_dedupe_key", "cc_agent_proposals")
    op.drop_index("ix_cc_agent_proposals_site_id", "cc_agent_proposals")
    op.drop_index("ix_cc_agent_proposals_agent_id", "cc_agent_proposals")
    op.drop_index("ix_cc_agent_proposals_tenant_id", "cc_agent_proposals")
    op.drop_table("cc_agent_proposals")
    op.drop_index("ix_cc_agent_capabilities_tenant_id", "cc_agent_capabilities")
    op.drop_index("ix_cc_agent_capabilities_agent_id", "cc_agent_capabilities")
    op.drop_table("cc_agent_capabilities")
    op.drop_index("ix_cc_agent_scopes_tenant_id", "cc_agent_scopes")
    op.drop_index("ix_cc_agent_scopes_agent_id", "cc_agent_scopes")
    op.drop_table("cc_agent_scopes")
    op.drop_index("ix_cc_operational_agents_tenant_id", "cc_operational_agents")
    op.drop_table("cc_operational_agents")
