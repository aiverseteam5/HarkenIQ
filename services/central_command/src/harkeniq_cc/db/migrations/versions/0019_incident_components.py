"""A5: the component identity an incident actually names (spec A22.4).

  cc_incidents.components   [{"component", "severity", "skill_name", "at"}]

A verdict's sensor id is ``"<subsystem>:<component>"``. The Site Manager
has always split off the subsystem and DISCARDED the remainder, so
Central Command held no component identity for any device -- no drive
bay, no port name. That is why A4 could make IDENTIFY_LED,
INTERFACE_ENABLE and INTERFACE_DISABLE addressable while the evaluator
could supply none of their required parameters: every proposal carried
``params={"reason": ...}`` and was refused at the node.

NULLABLE WITH NO BACKFILL, and that is the whole design. NULL means this
Site Manager has not reported components, which is UNKNOWN. Writing ``[]``
would assert that nothing is affected -- a fact nobody checked -- and an
agent reading it would conclude the incident names no component and
refuse for the wrong reason. A17.4's rule, third application: unknown is
neither present nor absent.

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-01
"""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

_JSON = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    # 0001 is a create_all from CURRENT models, so a fresh database is born
    # with this column and only an existing one needs the add.
    if "cc_incidents" not in set(inspector.get_table_names()):
        return
    columns = {c["name"] for c in inspector.get_columns("cc_incidents")}
    if "components" not in columns:
        op.add_column(
            "cc_incidents", sa.Column("components", _JSON, nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "cc_incidents" not in set(inspector.get_table_names()):
        return
    columns = {c["name"] for c in inspector.get_columns("cc_incidents")}
    if "components" in columns:
        op.drop_column("cc_incidents", "components")
