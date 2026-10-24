"""TX-1 (A31): the Resource Catalogue foundation.

  cc_resources               one described thing in the estate: an opaque,
                             immutable id; a closed kind; a namespaced type;
                             lifecycle; provenance; and the legacy link to
                             the canonical site or device it mirrors
  cc_resource_relationships  typed directional edges, with primary
                             containment as CONTAINS + is_primary and a
                             partial unique index making "at most one
                             primary containment parent" a guarantee
  cc_resource_external_ids   (source_system, namespace, external_id) ->
                             resource_id, unique per tenant

Additive: no existing table, column or index changes. NO backfill: the
mirror (`harkeniq_cc.resource_mirror`) populates the catalogue at runtime,
so there is one writer and one implementation of what a mirrored resource
is. Nothing here is read by any authorization, execution, safety, dispatch
or correlation path.

Guarded and idempotent, as every migration since 0010: 0001 is a
create_all from CURRENT models, so a fresh database already has the
tables, and a re-run after the gate's alembic rewinds finds them present.

Revision ID: 0030
Revises: 0029
"""

from alembic import op
import sqlalchemy as sa

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

_RESOURCES = "cc_resources"
_RELATIONSHIPS = "cc_resource_relationships"
_EXTERNAL_IDS = "cc_resource_external_ids"


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    present = _tables(bind)

    if _RESOURCES not in present:
        op.create_table(
            _RESOURCES,
            sa.Column("id", sa.String(length=32), primary_key=True),
            sa.Column("tenant_id", sa.String(length=64), nullable=False),
            sa.Column("resource_kind", sa.String(length=32), nullable=False),
            sa.Column("resource_type", sa.String(length=128), nullable=False),
            sa.Column("display_name", sa.String(length=255), nullable=False),
            sa.Column("lifecycle_state", sa.String(length=16), nullable=False),
            sa.Column("source", sa.String(length=64), nullable=False),
            sa.Column("legacy_kind", sa.String(length=16), nullable=True),
            sa.Column("legacy_ref", sa.String(length=255), nullable=True),
            sa.Column("legacy_site_id", sa.String(length=32), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint(
                "tenant_id", "legacy_kind", "legacy_ref",
                name="uq_resource_legacy_link",
            ),
        )
        op.create_index("ix_cc_resources_tenant_id", _RESOURCES, ["tenant_id"])
        op.create_index(
            "ix_cc_resources_legacy_site_id", _RESOURCES, ["legacy_site_id"]
        )
        op.create_index(
            "ix_resources_tenant_kind", _RESOURCES, ["tenant_id", "resource_kind"]
        )

    if _RELATIONSHIPS not in present:
        op.create_table(
            _RELATIONSHIPS,
            sa.Column("id", sa.String(length=32), primary_key=True),
            sa.Column("tenant_id", sa.String(length=64), nullable=False),
            sa.Column(
                "from_resource_id", sa.String(length=32),
                sa.ForeignKey("cc_resources.id"), nullable=False,
            ),
            sa.Column(
                "to_resource_id", sa.String(length=32),
                sa.ForeignKey("cc_resources.id"), nullable=False,
            ),
            sa.Column("relationship_type", sa.String(length=32), nullable=False),
            sa.Column("is_primary", sa.Boolean(), nullable=False),
            sa.Column("source", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "from_resource_id", "to_resource_id", "relationship_type",
                name="uq_resource_relationship",
            ),
        )
        op.create_index(
            "ix_cc_resource_relationships_tenant_id", _RELATIONSHIPS, ["tenant_id"]
        )
        op.create_index(
            "ix_cc_resource_relationships_from_resource_id",
            _RELATIONSHIPS, ["from_resource_id"],
        )
        op.create_index(
            "ix_cc_resource_relationships_to_resource_id",
            _RELATIONSHIPS, ["to_resource_id"],
        )
        # At most one primary containment parent per resource: a PARTIAL
        # unique index, identical on both engines.
        op.create_index(
            "uq_resource_primary_parent", _RELATIONSHIPS, ["to_resource_id"],
            unique=True,
            postgresql_where=sa.text("is_primary"),
            sqlite_where=sa.text("is_primary"),
        )

    if _EXTERNAL_IDS not in present:
        op.create_table(
            _EXTERNAL_IDS,
            sa.Column("id", sa.String(length=32), primary_key=True),
            sa.Column("tenant_id", sa.String(length=64), nullable=False),
            sa.Column(
                "resource_id", sa.String(length=32),
                sa.ForeignKey("cc_resources.id"), nullable=False,
            ),
            sa.Column("source_system", sa.String(length=64), nullable=False),
            sa.Column("namespace", sa.String(length=128), nullable=False),
            sa.Column("external_id", sa.String(length=512), nullable=False),
            sa.Column("source", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "tenant_id", "source_system", "namespace", "external_id",
                name="uq_resource_external_id",
            ),
        )
        op.create_index(
            "ix_cc_resource_external_ids_tenant_id", _EXTERNAL_IDS, ["tenant_id"]
        )
        op.create_index(
            "ix_cc_resource_external_ids_resource_id",
            _EXTERNAL_IDS, ["resource_id"],
        )
    # Deliberately NO backfill. See the module docstring.


def downgrade() -> None:
    bind = op.get_bind()
    present = _tables(bind)
    # Children before the table they reference.
    for table in (_EXTERNAL_IDS, _RELATIONSHIPS, _RESOURCES):
        if table in present:
            op.drop_table(table)
