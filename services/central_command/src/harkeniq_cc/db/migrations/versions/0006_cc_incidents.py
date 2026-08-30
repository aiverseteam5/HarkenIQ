"""S4: cc_incidents — real incidents with their diagnosis at Central Command.

Before this, CC synthesised "incidents" from critical-health devices while
the Site Manager's real consolidated incidents (and the LLM explanation
attached to them) never left the site. The tenant surface could show that
something was wrong but never why.

Correlation hierarchy is preserved: one parent with children, as the Site
Manager consolidated it. Resolution follows D3 — absence from a snapshot
infers resolution; no resolution reason is stored.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-29
"""

from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 is a create_all from CURRENT models, so a fresh database is born
    # with this table — only pre-S4 databases need the CREATE. Idempotence
    # is mandatory for every additive migration in this chain.
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("cc_incidents"):
        return
    op.create_table(
        "cc_incidents",
        sa.Column("incident_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("site_id", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("device_agent_id", sa.String(64), nullable=False),
        sa.Column("subsystem", sa.String(32), nullable=False),
        sa.Column("parent_incident_id", sa.String(64), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("inferred", sa.Boolean(), nullable=False),
        sa.Column("correlation_meta", sa.JSON(), nullable=True),
        sa.Column("explanation", sa.JSON(), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_cc_incidents_tenant_status", "cc_incidents", ["tenant_id", "status"],
    )
    op.create_index(
        "ix_cc_incidents_device", "cc_incidents", ["tenant_id", "device_agent_id"],
    )


def downgrade() -> None:
    op.drop_table("cc_incidents")
