"""Initial Console schema (R4-0).

Creates all Console tables from the declarative metadata. Covers R2b
tables: tenants, users, API keys, subscriptions, invoices, support
tickets, audit logs, feature toggles, releases, impersonation logs,
approval groups, and licensing.

Revision ID: 0001
Revises:
Create Date: 2026-08-24
"""

from alembic import op

from harkeniq_console.db.models import Base

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
