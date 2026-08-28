"""Tenant service placement registry.

The Console proxied every infrastructure surface to one global
``config.cc_url``, so every tenant saw the same Central Command. L1-L3 are
single-tenant by the constitution and tenancy lives only at L4, so the
vendor Console has to know which stack belongs to which tenant. This table
is that mapping, and resolution against it is fail-closed.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-28
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    # 0001 is `Base.metadata.create_all()` against LIVE metadata, so a fresh
    # database already has every table the models currently declare —
    # including this one — while a database stamped at 0001 before this
    # commit does not. Until 0001 is frozen to an explicit table list, every
    # migration after it has to be idempotent or fresh installs break.
    if _has_table("tenant_services"):
        return

    op.create_table(
        "tenant_services",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(32), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("service_kind", sa.String(32), nullable=False),
        sa.Column("endpoint_url", sa.String(512), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("registered_by", sa.String(32), nullable=False, server_default=""),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_tenant_services_tenant", "tenant_services", ["tenant_id"],
    )
    # One ACTIVE placement per kind per tenant; disabled rows stay as
    # history, so re-registering a moved tenant is not blocked.
    op.create_index(
        "uq_tenant_services_active",
        "tenant_services",
        ["tenant_id", "service_kind"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    if not _has_table("tenant_services"):
        return
    op.drop_index("uq_tenant_services_active", table_name="tenant_services")
    op.drop_index("ix_tenant_services_tenant", table_name="tenant_services")
    op.drop_table("tenant_services")
