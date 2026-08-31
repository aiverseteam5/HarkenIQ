"""cc_fleet_cache.capabilities — the node's own capability declaration.

The Capability Registry's fleet-side storage. Carried verbatim from the
Site Manager, which carries it verbatim from the node, which is the only
authoritative source: nothing at this layer declares a capability, it
only reflects one.

Nullable with no backfill, and that is load-bearing rather than lazy.
NULL means "this device has not declared", which is a different claim
from a declaration whose effective set is empty. Backfilling an empty
set would tell ``/api/capabilities`` that every device cached before this
migration can do nothing — and the Operational Agent's zero-reach
refusal would then strip every bound action class from every agent on
every fleet that upgraded Central Command before its agents. Rows fill
in on their own as agents re-register and sites are polled.

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-31
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

JSONVariant = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    # 0001 is a create_all from CURRENT models, so fresh databases are born
    # with the column and only existing ones need the ALTER (same guard as
    # 0002, 0005 and 0013).
    inspector = sa.inspect(op.get_bind())
    columns = {c["name"] for c in inspector.get_columns("cc_fleet_cache")}
    if "capabilities" in columns:
        return
    op.add_column(
        "cc_fleet_cache", sa.Column("capabilities", JSONVariant, nullable=True)
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {c["name"] for c in inspector.get_columns("cc_fleet_cache")}
    if "capabilities" in columns:
        op.drop_column("cc_fleet_cache", "capabilities")
