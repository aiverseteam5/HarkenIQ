"""TX-3 (A33): external identity, provenance, aliases and tombstones.

  cc_resources.{source_system, source_namespace,      where a description
                first_observed_at, last_observed_at}  came from, and when
                                                      its source observed it
                                                      (A33.4), all nullable
  cc_resource_relationships.{the same four}           the same, for an edge
  cc_resource_external_ids.{first_observed_at,        the same, for an
                            last_observed_at}         identifier binding
  cc_resource_aliases                                 what a resource is
      CALLED, kept apart from what it IS (A33.3): never unique across
      resources, one row per (resource, type, normalized value, source)
  fk_resource_external_id_tenant,                     (resource_id, tenant_id)
  fk_resource_alias_tenant                            -> cc_resources (id,
      tenant_id): a binding or an alias cannot cross tenants (A33.2)

Additive. NO backfill: a row written before TX-3 carries NULL provenance --
"not recorded" -- until the mirror's next pass records it, so there is still
one writer of every catalogue fact, and no alias is invented here.

SQLite cannot add a constraint to an existing table with ALTER, so the
composite foreign keys are added on PostgreSQL only -- 0031's pattern; a
fresh database of either engine has them from 0001's create_all, which
builds the CURRENT models. A row that already crosses tenants refuses the
migration loudly: no HarkenIQ writer produces one, so it is corruption to
investigate, never something to delete so that a key fits.

Guarded and idempotent, as every migration since 0010. The downgrade drops
exactly what this revision added.

Revision ID: 0032
Revises: 0031
"""

from alembic import op
import sqlalchemy as sa

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None

_RESOURCES = "cc_resources"
_RELATIONSHIPS = "cc_resource_relationships"
_EXTERNAL_IDS = "cc_resource_external_ids"
_ALIASES = "cc_resource_aliases"

_OBSERVED = ("first_observed_at", "last_observed_at")
_ORIGIN = ("source_system", "source_namespace")
#: table -> the provenance columns this revision adds to it
_PROVENANCE = {
    _RESOURCES: _ORIGIN + _OBSERVED,
    _RELATIONSHIPS: _ORIGIN + _OBSERVED,
    _EXTERNAL_IDS: _OBSERVED,
}
#: name -> the table whose (resource_id, tenant_id) it pins
_TENANT_FKS = {
    "fk_resource_external_id_tenant": _EXTERNAL_IDS,
    "fk_resource_alias_tenant": _ALIASES,
}


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _columns(bind, table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(table)}


def _foreign_keys(bind, table: str) -> set[str]:
    return {
        fk["name"] for fk in sa.inspect(bind).get_foreign_keys(table)
        if fk.get("name")
    }


def _column(name: str) -> sa.Column:
    if name == "source_system":
        return sa.Column(name, sa.String(length=64), nullable=True)
    if name == "source_namespace":
        return sa.Column(name, sa.String(length=128), nullable=True)
    return sa.Column(name, sa.DateTime(timezone=True), nullable=True)


def upgrade() -> None:
    bind = op.get_bind()
    present = _tables(bind)
    postgres = bind.dialect.name == "postgresql"

    for table, names in _PROVENANCE.items():
        if table not in present:
            continue
        columns = _columns(bind, table)
        for name in names:
            if name not in columns:
                op.add_column(table, _column(name))

    if _ALIASES not in present and _RESOURCES in present:
        op.create_table(
            _ALIASES,
            sa.Column("id", sa.String(length=32), primary_key=True),
            sa.Column("tenant_id", sa.String(length=64), nullable=False),
            sa.Column(
                "resource_id", sa.String(length=32),
                sa.ForeignKey("cc_resources.id"), nullable=False,
            ),
            sa.Column("alias_type", sa.String(length=32), nullable=False),
            sa.Column("value", sa.String(length=255), nullable=False),
            sa.Column("normalized_value", sa.String(length=255), nullable=False),
            sa.Column("source_system", sa.String(length=64), nullable=False),
            sa.Column("source_namespace", sa.String(length=128), nullable=False),
            sa.Column("source", sa.String(length=64), nullable=False),
            sa.Column(
                "lifecycle_state", sa.String(length=16),
                nullable=False, server_default="active",
            ),
            sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "resource_id", "alias_type", "normalized_value",
                "source_system", "source_namespace",
                name="uq_resource_alias",
            ),
        )
        op.create_index("ix_cc_resource_aliases_tenant_id", _ALIASES, ["tenant_id"])
        op.create_index("ix_cc_resource_aliases_resource_id", _ALIASES, ["resource_id"])
        op.create_index(
            "ix_resource_alias_lookup", _ALIASES,
            ["tenant_id", "alias_type", "normalized_value"],
        )
        present.add(_ALIASES)

    if postgres and _RESOURCES in present:
        for name, table in _TENANT_FKS.items():
            if table not in present or name in _foreign_keys(bind, table):
                continue
            crossing = bind.execute(sa.text(
                f"SELECT count(*) FROM {table} x "
                f"JOIN {_RESOURCES} r ON r.id = x.resource_id "
                "WHERE r.tenant_id <> x.tenant_id"
            )).scalar()
            if crossing:
                raise RuntimeError(
                    f"refusing to add {name}: {crossing} row(s) of {table} name a "
                    "resource of another tenant; no HarkenIQ writer produces one, "
                    "so this is corruption to investigate, not to migrate around"
                )
            op.create_foreign_key(
                name, table, _RESOURCES,
                ["resource_id", "tenant_id"], ["id", "tenant_id"],
            )
    # Deliberately NO backfill. See the module docstring.


def downgrade() -> None:
    bind = op.get_bind()
    present = _tables(bind)
    postgres = bind.dialect.name == "postgresql"

    if _ALIASES in present:
        op.drop_table(_ALIASES)
    if postgres and _EXTERNAL_IDS in present:
        if "fk_resource_external_id_tenant" in _foreign_keys(bind, _EXTERNAL_IDS):
            op.drop_constraint(
                "fk_resource_external_id_tenant", _EXTERNAL_IDS, type_="foreignkey"
            )
    for table, names in _PROVENANCE.items():
        if table not in present:
            continue
        columns = _columns(bind, table)
        for name in names:
            if name in columns:
                op.drop_column(table, name)
