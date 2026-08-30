"""E1.1: the tenant's own organizational tree, and one path per site.

`cc_org_units` is a generic tree -- the customer names its own levels --
with a materialized `path` of the form ``/id/id/`` so a subtree is one
prefix match that behaves identically on PostgreSQL and on the sqlite
the unit tests use. The trailing delimiter is load-bearing: without it
`/u1/u7/` would prefix-match the sibling `/u1/u70/`, and at E1.2 a scope
over Cluster 7 would silently cover Cluster 70.

`cc_sites.org_unit_id` gives every site exactly one canonical
containment path.

The backfill creates one root unit per tenant, `unit_type` =
"organization", named for the tenant, and attaches every existing site
to it. After it runs, every site has a path and nothing reads it: this
migration has no behavioural effect at all. Authorization scope is a
separate model that arrives at E1.2.

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-30
"""

import uuid
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 is a create_all from CURRENT models, so a fresh database is born
    # with this table and column; only pre-E1 databases need the DDL.
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("cc_org_units"):
        op.create_table(
            "cc_org_units",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column(
                "parent_id",
                sa.String(32),
                sa.ForeignKey("cc_org_units.id"),
                nullable=True,
            ),
            sa.Column("unit_type", sa.String(32), server_default="organization"),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("path", sa.String(512), nullable=False),
            sa.Column("depth", sa.Integer(), server_default="1"),
            sa.Column("sort_order", sa.Integer(), server_default="0"),
            sa.Column("created_by", sa.String(255), server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.Column("updated_by", sa.String(255), server_default=""),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint(
                "tenant_id", "parent_id", "name", name="uq_org_unit_sibling"
            ),
        )
        op.create_index("ix_cc_org_units_tenant_id", "cc_org_units", ["tenant_id"])
        op.create_index("ix_cc_org_units_parent_id", "cc_org_units", ["parent_id"])
        op.create_index("ix_cc_org_units_path", "cc_org_units", ["path"])
        op.create_index(
            "ix_org_units_tenant_path", "cc_org_units", ["tenant_id", "path"]
        )

    site_columns = {c["name"] for c in inspector.get_columns("cc_sites")}
    if "org_unit_id" not in site_columns:
        # sqlite cannot ADD COLUMN with a foreign key -- it would need a
        # table rebuild -- so the constraint is declared on PostgreSQL,
        # which is where production runs, and the column is plain on the
        # sqlite used by tests. The model carries the FK either way, so
        # the ORM relationship is identical on both.
        fk = (
            [sa.ForeignKey("cc_org_units.id")]
            if bind.dialect.name == "postgresql"
            else []
        )
        op.add_column(
            "cc_sites",
            sa.Column("org_unit_id", sa.String(32), *fk, nullable=True),
        )
        op.create_index("ix_cc_sites_org_unit_id", "cc_sites", ["org_unit_id"])

    _backfill(bind)


def _backfill(bind) -> None:
    """One root unit per tenant; every unattached site hangs from it.

    Re-runnable: a tenant that already has a root is left alone, and only
    sites with a null `org_unit_id` are touched. A migration that could
    double-root a tenant on a retry would be worse than no backfill.
    """
    now = datetime.now(timezone.utc)
    tenants = [
        row[0]
        for row in bind.execute(
            sa.text(
                "SELECT DISTINCT tenant_id FROM cc_sites "
                "WHERE org_unit_id IS NULL AND tenant_id IS NOT NULL"
            )
        ).fetchall()
    ]

    for tenant_id in tenants:
        existing = bind.execute(
            sa.text(
                "SELECT id FROM cc_org_units "
                "WHERE tenant_id = :t AND parent_id IS NULL "
                "ORDER BY created_at LIMIT 1"
            ),
            {"t": tenant_id},
        ).fetchone()

        if existing:
            root_id = existing[0]
        else:
            root_id = uuid.uuid4().hex
            bind.execute(
                sa.text(
                    "INSERT INTO cc_org_units "
                    "(id, tenant_id, parent_id, unit_type, name, path, depth, "
                    " sort_order, created_by, created_at, updated_by, updated_at) "
                    "VALUES (:id, :t, NULL, 'organization', :name, :path, 1, 0, "
                    " 'migration:0010', :now, 'migration:0010', :now)"
                ),
                {
                    "id": root_id,
                    "t": tenant_id,
                    "name": tenant_id,
                    "path": f"/{root_id}/",
                    "now": now,
                },
            )

        bind.execute(
            sa.text(
                "UPDATE cc_sites SET org_unit_id = :u "
                "WHERE tenant_id = :t AND org_unit_id IS NULL"
            ),
            {"u": root_id, "t": tenant_id},
        )


def downgrade() -> None:
    """Total: nothing outside E1.1 reads either object."""
    inspector = sa.inspect(op.get_bind())
    site_columns = {c["name"] for c in inspector.get_columns("cc_sites")}
    if "org_unit_id" in site_columns:
        op.drop_index("ix_cc_sites_org_unit_id", "cc_sites")
        op.drop_column("cc_sites", "org_unit_id")
    if inspector.has_table("cc_org_units"):
        op.drop_table("cc_org_units")
