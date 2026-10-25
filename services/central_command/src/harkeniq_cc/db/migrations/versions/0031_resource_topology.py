"""TX-2 (A32): resource relationships, topology and the rack mapping.

  cc_resource_relationships.lifecycle_state   NOT NULL, default 'active':
      an edge is retired when its source stops asserting it -- a former
      placement, kept rather than rewritten or deleted (A32.2)
  cc_resource_relationships.retired_at        nullable timestamptz
  uq_resource_active_primary_parent           replaces
      uq_resource_primary_parent: at most one ACTIVE primary containment
      parent per resource (A32.3)
  uq_resource_tenant_identity                 cc_resources (id, tenant_id),
      the target of the two composite foreign keys below
  fk_resource_relationship_from_tenant,       (from|to_resource_id, tenant_id)
  fk_resource_relationship_to_tenant          -> cc_resources (id, tenant_id):
      an edge between two tenants cannot be written (A32.3)
  cc_site_topology                            one row per site: the racks its
      Site Manager reports, read by the catalogue's mirror and nothing else
      (A32.4)

Additive. NO backfill: every existing edge was a current edge, so `active`
is exactly what it already was, and no topology row or rack resource is
invented -- the poller writes the topology and the mirror the racks, at
runtime, so there is still one writer of each.

SQLite cannot add a constraint to an existing table with ALTER, so the
tenant identity and the composite foreign keys are added here on PostgreSQL
only; a fresh database of either engine has them from 0001's create_all,
which builds the CURRENT models.

Guarded and idempotent, as every migration since 0010: a fresh database
already has every object, and a re-run after the gate's alembic rewinds
finds them present. The index swap and the constraints are transactional
on PostgreSQL; the catalogue's tables are one row per site, rack and device,
so nothing here needs a CONCURRENTLY build.

The downgrade deletes RETIRED edges first -- TX-1 had none, and the index it
restores admits one primary edge per child whatever its state -- then
restores TX-1's index and drops what this revision added.

Revision ID: 0031
Revises: 0030
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None

_RESOURCES = "cc_resources"
_RELATIONSHIPS = "cc_resource_relationships"
_TOPOLOGY = "cc_site_topology"

JSONVariant = sa.JSON().with_variant(JSONB(), "postgresql")

_OLD_PRIMARY = "uq_resource_primary_parent"
_ACTIVE_PRIMARY = "uq_resource_active_primary_parent"
_ACTIVE_PREDICATE = "is_primary AND lifecycle_state = 'active'"
_TENANT_IDENTITY = "uq_resource_tenant_identity"
#: name -> the edge column whose tenant it pins
_TENANT_FKS = {
    "fk_resource_relationship_from_tenant": "from_resource_id",
    "fk_resource_relationship_to_tenant": "to_resource_id",
}


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _indexes(bind, table: str) -> set[str]:
    return {ix["name"] for ix in sa.inspect(bind).get_indexes(table)}


def _unique_constraints(bind, table: str) -> set[str]:
    return {
        uc["name"] for uc in sa.inspect(bind).get_unique_constraints(table)
        if uc.get("name")
    }


def _foreign_keys(bind, table: str) -> set[str]:
    return {
        fk["name"] for fk in sa.inspect(bind).get_foreign_keys(table)
        if fk.get("name")
    }


def upgrade() -> None:
    bind = op.get_bind()
    present = _tables(bind)
    postgres = bind.dialect.name == "postgresql"

    if _RELATIONSHIPS in present:
        columns = _columns(bind, _RELATIONSHIPS)
        if "lifecycle_state" not in columns:
            op.add_column(_RELATIONSHIPS, sa.Column(
                "lifecycle_state", sa.String(length=16),
                nullable=False, server_default="active",
            ))
        if "retired_at" not in columns:
            op.add_column(_RELATIONSHIPS, sa.Column(
                "retired_at", sa.DateTime(timezone=True), nullable=True,
            ))
        indexes = _indexes(bind, _RELATIONSHIPS)
        # The new guarantee first, then the old one out: there is no moment
        # without "one primary parent". Every row is active at this point,
        # so the new index admits exactly what the old one did.
        if _ACTIVE_PRIMARY not in indexes:
            op.create_index(
                _ACTIVE_PRIMARY, _RELATIONSHIPS, ["to_resource_id"],
                unique=True,
                postgresql_where=sa.text(_ACTIVE_PREDICATE),
                sqlite_where=sa.text(_ACTIVE_PREDICATE),
            )
        if _OLD_PRIMARY in indexes:
            op.drop_index(_OLD_PRIMARY, table_name=_RELATIONSHIPS)

    if postgres and _RESOURCES in present and _RELATIONSHIPS in present:
        if _TENANT_IDENTITY not in _unique_constraints(bind, _RESOURCES):
            op.create_unique_constraint(
                _TENANT_IDENTITY, _RESOURCES, ["id", "tenant_id"],
            )
        existing = _foreign_keys(bind, _RELATIONSHIPS)
        for name, column in _TENANT_FKS.items():
            if name in existing:
                continue
            # No HarkenIQ writer can produce a cross-tenant edge, so one is
            # corruption to investigate -- refused loudly, never deleted to
            # make the constraint fit.
            crossing = bind.execute(sa.text(
                f"SELECT count(*) FROM {_RELATIONSHIPS} e "
                f"JOIN {_RESOURCES} r ON r.id = e.{column} "
                "WHERE r.tenant_id <> e.tenant_id"
            )).scalar()
            if crossing:
                raise RuntimeError(
                    f"refusing to add {name}: {crossing} edge(s) join resources "
                    "of different tenants; no HarkenIQ writer produces one, so "
                    "this is corruption to investigate, not to migrate around"
                )
            op.create_foreign_key(
                name, _RELATIONSHIPS, _RESOURCES,
                [column, "tenant_id"], ["id", "tenant_id"],
            )

    if _TOPOLOGY not in present:
        op.create_table(
            _TOPOLOGY,
            sa.Column("site_id", sa.String(length=32), primary_key=True),
            sa.Column("tenant_id", sa.String(length=64), nullable=False),
            sa.Column("reported", sa.Boolean(), nullable=False),
            sa.Column("racks", JSONVariant, nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_cc_site_topology_tenant", _TOPOLOGY, ["tenant_id"])
    # Deliberately NO backfill. See the module docstring.


def downgrade() -> None:
    bind = op.get_bind()
    present = _tables(bind)
    postgres = bind.dialect.name == "postgresql"

    if _TOPOLOGY in present:
        op.drop_table(_TOPOLOGY)

    if postgres and _RELATIONSHIPS in present:
        existing = _foreign_keys(bind, _RELATIONSHIPS)
        for name in _TENANT_FKS:
            if name in existing:
                op.drop_constraint(name, _RELATIONSHIPS, type_="foreignkey")
    if postgres and _RESOURCES in present:
        if _TENANT_IDENTITY in _unique_constraints(bind, _RESOURCES):
            op.drop_constraint(_TENANT_IDENTITY, _RESOURCES, type_="unique")

    if _RELATIONSHIPS in present:
        columns = _columns(bind, _RELATIONSHIPS)
        if "lifecycle_state" in columns:
            # TX-1 has no retired edge, and its index would count one.
            bind.execute(sa.text(
                f"DELETE FROM {_RELATIONSHIPS} WHERE lifecycle_state <> 'active'"
            ))
        indexes = _indexes(bind, _RELATIONSHIPS)
        if _OLD_PRIMARY not in indexes:
            op.create_index(
                _OLD_PRIMARY, _RELATIONSHIPS, ["to_resource_id"], unique=True,
                postgresql_where=sa.text("is_primary"),
                sqlite_where=sa.text("is_primary"),
            )
        # Before the column it names: SQLite will not drop a column an index
        # still reads.
        if _ACTIVE_PRIMARY in indexes:
            op.drop_index(_ACTIVE_PRIMARY, table_name=_RELATIONSHIPS)
        for column in ("retired_at", "lifecycle_state"):
            if column in columns:
                op.drop_column(_RELATIONSHIPS, column)
