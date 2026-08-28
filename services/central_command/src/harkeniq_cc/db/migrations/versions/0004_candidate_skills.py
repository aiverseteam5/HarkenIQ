"""QA-033 feedback half: cc_candidate_skills — SM candidate-skill intake.

Candidate skills generated at Site Managers ride FleetSnapshot up to CC;
the intelligence loop links them to detected fleet patterns and tracks
the R-C1 learning cycle through to promotion recommendation.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-26
"""

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 is a create_all from CURRENT models, so a fresh database is
    # born with this table — only pre-QA-033 databases need the CREATE.
    # Idempotence is mandatory for every additive migration in this chain.
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("cc_candidate_skills"):
        return
    op.create_table(
        "cc_candidate_skills",
        sa.Column("skill_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), primary_key=True),
        sa.Column("site_id", sa.String(32), nullable=False),
        sa.Column("yaml_text", sa.Text(), nullable=False),
        sa.Column("source_device", sa.String(64), nullable=False),
        sa.Column("source_component", sa.String(128), nullable=False),
        sa.Column("validation_state", sa.String(16), nullable=False),
        sa.Column("warnings", sa.JSON(), nullable=True),
        sa.Column("dry_run_matches", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("cycle_id", sa.String(64), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_candidate_skills_tenant_status",
        "cc_candidate_skills",
        ["tenant_id", "status"],
    )


def downgrade() -> None:
    op.drop_table("cc_candidate_skills")
