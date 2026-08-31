"""S6: governed capability orchestration across an estate.

  cc_campaigns             one action class, one scoped run, versioned
  cc_campaign_targets      every device considered, INCLUDING the excluded
                           ones with their reason -- the artifact that
                           makes "never discovers incapability after
                           dispatch" auditable rather than asserted
  cc_campaign_sites        per-site branch state; the site is the
                           isolation unit, as it is for identity (E0.2),
                           correlation and error budgets (E1.3)
  cc_campaign_scopes       the target SELECTION, kept out of the action
                           params it would otherwise ship to nodes as an
                           execution payload
  cc_campaign_plans        IMMUTABLE per-site wave plans as the Site
                           Manager computed them: membership and a domain
                           COUNT, never domain identities
  cc_campaign_waves        the unit of approval and of execution, and
                           where APPROVED / EXECUTABLE / EXECUTED are
                           kept apart
  cc_campaign_dispatches   durable ledger whose COMPOSITE PRIMARY KEY is
                           the idempotency guarantee: a restart, replay
                           or redelivery cannot execute a device twice in
                           a wave, because the second row cannot exist

No device wave plan is stored here, deliberately. Fault domains live at
the Site Manager and Central Command must never invent or approximate
blast radius (S6 architectural invariant), so this schema carries site
ordering and stops there.

`cc_approval_records` is untouched: campaign waves ride the existing
E0.1 ledger under a new `subject_type` value, which needs no schema
change and creates no second approval model.

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-31
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

JSONVariant = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())

    if "cc_campaigns" not in tables:
        op.create_table(
            "cc_campaigns",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("description", sa.String(1024), nullable=False, server_default=""),
            sa.Column("action_type", sa.String(64), nullable=False),
            sa.Column("params", JSONVariant, nullable=True),
            sa.Column("status", sa.String(24), nullable=False, server_default="draft"),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("site_concurrency", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("max_wave_size", sa.Integer(), nullable=False, server_default="5"),
            sa.Column("created_by", sa.String(255), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("preflight_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("acknowledged_by", sa.String(255), nullable=False, server_default=""),
            sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("acknowledged_version", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("halt_reason", sa.String(1024), nullable=False, server_default=""),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index(
            "ix_cc_campaigns_tenant_status", "cc_campaigns", ["tenant_id", "status"]
        )

    if "cc_campaign_targets" not in tables:
        op.create_table(
            "cc_campaign_targets",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column(
                "campaign_id", sa.String(32),
                sa.ForeignKey("cc_campaigns.id"), nullable=False, index=True,
            ),
            sa.Column("site_id", sa.String(32), nullable=False, index=True),
            sa.Column("device_agent_id", sa.String(255), nullable=False),
            sa.Column("device_name", sa.String(255), nullable=False, server_default=""),
            sa.Column("device_class", sa.String(32), nullable=False, server_default="server"),
            sa.Column("applicability", sa.String(32), nullable=False, server_default="eligible"),
            sa.Column("reason", sa.String(512), nullable=False, server_default=""),
            sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("revalidation", sa.String(32), nullable=False, server_default=""),
            sa.Column("revalidation_reason", sa.String(512), nullable=False, server_default=""),
            sa.Column("outcome", sa.String(32), nullable=False, server_default=""),
            sa.Column("error", sa.String(512), nullable=False, server_default=""),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "campaign_id", "device_agent_id", name="uq_campaign_target"
            ),
        )
        op.create_index(
            "ix_cc_campaign_targets_site", "cc_campaign_targets",
            ["campaign_id", "site_id"],
        )

    if "cc_campaign_sites" not in tables:
        op.create_table(
            "cc_campaign_sites",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column(
                "campaign_id", sa.String(32),
                sa.ForeignKey("cc_campaigns.id"), nullable=False, index=True,
            ),
            sa.Column("site_id", sa.String(32), nullable=False, index=True),
            sa.Column("site_name", sa.String(255), nullable=False, server_default=""),
            sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("order_index", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("current_wave", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("wave_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("halt_reason", sa.String(1024), nullable=False, server_default=""),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("campaign_id", "site_id", name="uq_campaign_site"),
        )

    if "cc_campaign_dispatches" not in tables:
        op.create_table(
            "cc_campaign_dispatches",
            sa.Column("campaign_id", sa.String(32), primary_key=True),
            sa.Column("campaign_version", sa.Integer(), primary_key=True),
            sa.Column("site_id", sa.String(32), primary_key=True),
            sa.Column("device_agent_id", sa.String(255), primary_key=True),
            sa.Column("wave_index", sa.Integer(), primary_key=True),
            sa.Column("plan_hash", sa.String(64), primary_key=True),
            sa.Column("directive_id", sa.String(64), nullable=False, server_default=""),
            sa.Column("actor", sa.String(255), nullable=False, server_default=""),
            sa.Column("authorization", sa.String(32), nullable=False, server_default=""),
            sa.Column("decided_by", sa.String(255), nullable=False, server_default=""),
            sa.Column("accepted", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("detail", sa.String(512), nullable=False, server_default=""),
            sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=False),
        )


    if "cc_campaign_scopes" not in tables:
        op.create_table(
            "cc_campaign_scopes",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column(
                "campaign_id", sa.String(32),
                sa.ForeignKey("cc_campaigns.id"), nullable=False, index=True,
            ),
            sa.Column("scope_type", sa.String(32), nullable=False),
            sa.Column("scope_ref", sa.String(128), nullable=False),
            sa.UniqueConstraint(
                "campaign_id", "scope_type", "scope_ref", name="uq_campaign_scope"
            ),
        )

    if "cc_campaign_plans" not in tables:
        op.create_table(
            "cc_campaign_plans",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column(
                "campaign_id", sa.String(32),
                sa.ForeignKey("cc_campaigns.id"), nullable=False, index=True,
            ),
            sa.Column("campaign_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("site_id", sa.String(32), nullable=False, index=True),
            sa.Column("plan_hash", sa.String(64), nullable=False),
            sa.Column("waves", JSONVariant, nullable=True),
            sa.Column("unplannable", JSONVariant, nullable=True),
            sa.Column("separation_rule", sa.String(255), nullable=False, server_default=""),
            sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint(
                "campaign_id", "campaign_version", "site_id", "plan_hash",
                name="uq_campaign_plan",
            ),
        )

    if "cc_campaign_waves" not in tables:
        op.create_table(
            "cc_campaign_waves",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column(
                "campaign_id", sa.String(32),
                sa.ForeignKey("cc_campaigns.id"), nullable=False, index=True,
            ),
            sa.Column("campaign_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("site_id", sa.String(32), nullable=False, index=True),
            sa.Column("wave_index", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("plan_hash", sa.String(64), nullable=False),
            sa.Column("device_agent_ids", JSONVariant, nullable=True),
            sa.Column("domain_span", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("subject_ref", sa.String(64), nullable=False, server_default=""),
            sa.Column("status", sa.String(24), nullable=False, server_default="pending_approval"),
            sa.Column("void_reason", sa.String(512), nullable=False, server_default=""),
            sa.Column("decided_by", sa.String(255), nullable=False, server_default=""),
            sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint(
                "campaign_id", "campaign_version", "site_id", "wave_index",
                "plan_hash", name="uq_campaign_wave",
            ),
        )
        op.create_index(
            "ix_cc_campaign_waves_status", "cc_campaign_waves",
            ["campaign_id", "status"],
        )
        op.create_index(
            "ix_cc_campaign_waves_subject", "cc_campaign_waves", ["subject_ref"]
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    for name in (
        "cc_campaign_waves",
        "cc_campaign_plans",
        "cc_campaign_scopes",
        "cc_campaign_dispatches",
        "cc_campaign_sites",
        "cc_campaign_targets",
        "cc_campaigns",
    ):
        if name in tables:
            op.drop_table(name)
