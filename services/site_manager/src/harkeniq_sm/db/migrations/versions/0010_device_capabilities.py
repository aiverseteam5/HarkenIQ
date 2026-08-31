"""Capability Registry: the node's capability declaration, persisted.

  devices.capabilities    what this device's executor can ACTUALLY do,
                          as the node itself declared it at registration

Deliberately NULLABLE with no backfill and no default. A device that has
not declared is UNKNOWN, and unknown is not the same claim as "no
capabilities": inventing an empty set here would tell Central Command
that every pre-Registry device can do nothing, which would strip bound
action classes from every fleet the moment it upgraded its Site Manager
ahead of its agents. The rows fill themselves in as agents re-register,
which they do on every report loop (QA-041).

Nothing about execution changes. The node's allow list remains the final
execution authority; this column is what lets the platform SAY what that
authority will permit before an operator finds out by watching an
approved action get refused.

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-31
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

JSONVariant = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {c["name"] for c in inspector.get_columns("devices")}
    if "capabilities" not in columns:
        op.add_column(
            "devices", sa.Column("capabilities", JSONVariant, nullable=True)
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {c["name"] for c in inspector.get_columns("devices")}
    if "capabilities" in columns:
        op.drop_column("devices", "capabilities")
