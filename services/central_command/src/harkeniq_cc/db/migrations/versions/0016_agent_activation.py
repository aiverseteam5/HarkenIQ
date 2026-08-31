"""A2: the Operational Agent becomes a complete governed product.

  cc_agent_preflights        IMMUTABLE activation readiness results,
                             bound to the configuration version they
                             describe. A re-run is a new row; the old
                             one is superseded, never updated, so what
                             a person actually approved stays explicable.
  cc_agent_skill_installs    per-DEVICE skill delivery ledger whose
                             composite key prevents a double install.
                             Per device because SiteSkillInstall carried
                             only a site id and fanned out to every
                             device on it -- a scope escape dressed as a
                             convenience.

  cc_operational_agents gains:
    execution_budget /       D2: counts actions EXECUTED under this
    budget_period            agent's attribution, as S5 budgets count
                             actions. Exhaustion stops unattended
                             execution only; the agent keeps observing,
                             proposing, and doing what a human approves.
    paused_reason            per-agent safety. Can only tighten.
    activation_acknowledged* D1/D3: a named human accepted this
                             configuration's warnings, version-bound so
                             an edit invalidates it.
    activation_subject_ref   the approval subject raised when activation
                             would confer unattended execution.
    activated_version        the configuration that was actually
                             switched on.

No new permission, no new approval table, no second capability model.
Activation approval rides the existing E0.1 ledger under a new
subject_type, which needs no schema change.

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-31
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

JSONVariant = sa.JSON().with_variant(JSONB(), "postgresql")

_AGENT_COLUMNS = (
    ("execution_budget", sa.Integer(), "0"),
    ("budget_period", sa.String(16), "daily"),
    ("paused_reason", sa.String(512), ""),
    ("activation_acknowledged_by", sa.String(255), ""),
    ("activation_acknowledged_version", sa.Integer(), "0"),
    ("activation_subject_ref", sa.String(64), ""),
    ("activated_version", sa.Integer(), "0"),
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())

    # 0001 is a create_all from CURRENT models, so a fresh database is
    # born with these and only an existing one needs the ALTER (the same
    # guard 0002, 0005, 0013 and 0014 use).
    columns = {c["name"] for c in inspector.get_columns("cc_operational_agents")}
    for name, type_, default in _AGENT_COLUMNS:
        if name not in columns:
            op.add_column(
                "cc_operational_agents",
                sa.Column(name, type_, nullable=False, server_default=default),
            )
    if "activation_acknowledged_at" not in columns:
        op.add_column(
            "cc_operational_agents",
            sa.Column(
                "activation_acknowledged_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )

    if "cc_agent_preflights" not in tables:
        op.create_table(
            "cc_agent_preflights",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column(
                "agent_id", sa.String(32),
                sa.ForeignKey("cc_operational_agents.id"),
                nullable=False, index=True,
            ),
            sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
            sa.Column("configuration_version", sa.Integer(), nullable=False,
                      server_default="1"),
            sa.Column("overall", sa.String(16), nullable=False,
                      server_default="unknown"),
            sa.Column("can_activate", sa.Boolean(), nullable=False,
                      server_default=sa.false()),
            sa.Column("requires_acknowledgement", sa.Boolean(), nullable=False,
                      server_default=sa.false()),
            sa.Column("requires_activation_approval", sa.Boolean(),
                      nullable=False, server_default=sa.false()),
            sa.Column("result", JSONVariant, nullable=True),
            sa.Column("produced_by", sa.String(255), nullable=False,
                      server_default=""),
            sa.Column("produced_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index(
            "ix_cc_agent_preflights_agent", "cc_agent_preflights",
            ["agent_id", "configuration_version"],
        )

    if "cc_agent_skill_installs" not in tables:
        op.create_table(
            "cc_agent_skill_installs",
            sa.Column("agent_id", sa.String(32), primary_key=True),
            sa.Column("agent_version", sa.Integer(), primary_key=True),
            sa.Column("skill_id", sa.String(255), primary_key=True),
            sa.Column("device_agent_id", sa.String(255), primary_key=True),
            sa.Column("site_id", sa.String(32), nullable=False, server_default=""),
            sa.Column("skill_version", sa.String(32), nullable=False,
                      server_default=""),
            sa.Column("status", sa.String(16), nullable=False,
                      server_default="queued"),
            sa.Column("detail", sa.String(512), nullable=False, server_default=""),
            sa.Column("installed_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    for name in ("cc_agent_skill_installs", "cc_agent_preflights"):
        if name in tables:
            op.drop_table(name)
    columns = {c["name"] for c in inspector.get_columns("cc_operational_agents")}
    for name, _, _ in _AGENT_COLUMNS:
        if name in columns:
            op.drop_column("cc_operational_agents", name)
    if "activation_acknowledged_at" in columns:
        op.drop_column("cc_operational_agents", "activation_acknowledged_at")
