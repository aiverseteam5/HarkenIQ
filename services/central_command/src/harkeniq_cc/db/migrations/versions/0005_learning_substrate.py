"""S3: durable learning substrate — cc_learning_cycles + cc_learned_signals.

The R-C1 loop already ran end to end (outcome → pattern → distribution →
candidate → influence on the next diagnosis), but its LEDGER lived in
process memory: cycles vanished on restart and cc_candidate_skills.cycle_id
pointed at nothing afterwards. These two tables make the learning record
durable so it can be consumed by attention, diagnosis and, later, agents.

Two distinct concepts, deliberately not collapsed:
  * cc_learning_cycles  — the PROCESS (pattern → candidate → measured
    improvement → promotion recommendation);
  * cc_learned_signals  — the KNOWLEDGE that process produced, projected
    onto the scope its evidence supports.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-29
"""

from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 is a create_all from CURRENT models, so a fresh database is born
    # with these tables — only pre-S3 databases need the CREATE. Idempotence
    # is mandatory for every additive migration in this chain.
    inspector = sa.inspect(op.get_bind())

    if not inspector.has_table("cc_learning_cycles"):
        op.create_table(
            "cc_learning_cycles",
            sa.Column("cycle_id", sa.String(64), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column("pattern_id", sa.String(64), nullable=False),
            sa.Column("pattern_type", sa.String(32), nullable=False),
            sa.Column("skill_id", sa.String(128), nullable=True),
            sa.Column("sites_distributed", sa.Integer(), nullable=False),
            sa.Column("devices_applied", sa.Integer(), nullable=False),
            sa.Column("outcomes_before", sa.JSON(), nullable=True),
            sa.Column("outcomes_after", sa.JSON(), nullable=True),
            sa.Column("improvement_pct", sa.Float(), nullable=True),
            sa.Column("promotion_recommended", sa.Boolean(), nullable=False),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index(
            "ix_learning_cycles_tenant_status",
            "cc_learning_cycles",
            ["tenant_id", "status"],
        )

    if not inspector.has_table("cc_learned_signals"):
        op.create_table(
            "cc_learned_signals",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column("signal_key", sa.String(255), nullable=False),
            sa.Column("scope_type", sa.String(16), nullable=False),
            sa.Column("scope_ref", sa.String(128), nullable=False),
            sa.Column("action_type", sa.String(64), nullable=False),
            sa.Column("vendor", sa.String(64), nullable=False),
            sa.Column("model", sa.String(128), nullable=False),
            sa.Column("statement", sa.String(512), nullable=False),
            sa.Column("evidence", sa.JSON(), nullable=True),
            sa.Column("confidence", sa.Float(), nullable=False),
            sa.Column("source_pattern_id", sa.String(64), nullable=False),
            sa.Column("source_cycle_id", sa.String(64), nullable=True),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("observation_count", sa.Integer(), nullable=False),
            sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_confirmed_at", sa.DateTime(timezone=True), nullable=False),
        )
        # Unique on (tenant, signal_key): re-detection REFRESHES a signal
        # rather than accumulating duplicates of the same knowledge.
        op.create_index(
            "ix_learned_signals_tenant_key",
            "cc_learned_signals",
            ["tenant_id", "signal_key"],
            unique=True,
        )
        op.create_index(
            "ix_learned_signals_scope",
            "cc_learned_signals",
            ["tenant_id", "scope_type", "scope_ref"],
        )


def downgrade() -> None:
    op.drop_table("cc_learned_signals")
    op.drop_table("cc_learning_cycles")
