"""Initial Central Command schema (R4-0).

Creates all CC tables from the declarative metadata. Covers R2b baseline
tables (sites, fleet cache, approvals, audit, policies, budgets) plus
R3b-3 learning tables (outcome history, fleet patterns).

Revision ID: 0001
Revises:
Create Date: 2026-08-24
"""

from alembic import op

from harkeniq_cc.db.models import Base

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
