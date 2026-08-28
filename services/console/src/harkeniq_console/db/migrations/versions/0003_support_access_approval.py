"""Support access becomes request -> approve, not self-grant.

The enable endpoint accepted platform_support, so support granted itself
entry into a customer tenant. Access is now requested by support and
approved by a platform_super_admin, and get_active() keys on the approved
status so a pending request grants nothing.

Existing rows migrate to status='approved': they were live grants under
the old rules, and silently voiding them would lock support out of an
in-flight incident at deploy time.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-28
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


NEW_COLUMNS = (
    ("status", lambda: sa.Column(
        "status", sa.String(16), nullable=False, server_default="requested")),
    ("requested_by", lambda: sa.Column(
        "requested_by", sa.String(32), nullable=False, server_default="")),
    ("requested_at", lambda: sa.Column(
        "requested_at", sa.DateTime(timezone=True), nullable=True)),
    ("reason", lambda: sa.Column("reason", sa.Text(), nullable=True)),
    ("approved_by", lambda: sa.Column("approved_by", sa.String(32), nullable=True)),
    ("approved_at", lambda: sa.Column(
        "approved_at", sa.DateTime(timezone=True), nullable=True)),
    ("denied_by", lambda: sa.Column("denied_by", sa.String(32), nullable=True)),
    ("denied_at", lambda: sa.Column(
        "denied_at", sa.DateTime(timezone=True), nullable=True)),
)


def _existing_columns() -> set:
    insp = sa.inspect(op.get_bind())
    return {c["name"] for c in insp.get_columns("support_access_log")}


def upgrade() -> None:
    # See 0002: 0001 create_all()s live metadata, so a fresh database already
    # has these columns while an older one does not. Add only what is missing.
    present = _existing_columns()
    added_any = False
    for name, make in NEW_COLUMNS:
        if name not in present:
            op.add_column("support_access_log", make())
            added_any = True

    if not added_any:
        # Fresh database: the schema is already current and there are no
        # legacy rows to reinterpret.
        return

    # Rows that predate the approval flow were granted under the old rules.
    # Treat them as approved by whoever enabled them, so a live grant keeps
    # working across the deploy instead of vanishing mid-incident.
    op.execute(
        """
        UPDATE support_access_log
           SET status = CASE WHEN revoked_at IS NULL THEN 'approved' ELSE 'revoked' END,
               requested_by = enabled_by,
               requested_at = enabled_at,
               approved_by = enabled_by,
               approved_at = enabled_at
        """
    )

    op.create_index(
        "ix_support_access_log_status", "support_access_log", ["status"],
    )

    # expires_at is nullable now: a pending request has no clock until it
    # is approved.
    with op.batch_alter_table("support_access_log") as batch:
        batch.alter_column(
            "expires_at", existing_type=sa.DateTime(timezone=True), nullable=True,
        )


def downgrade() -> None:
    present = _existing_columns()
    if "status" in present:
        op.drop_index(
            "ix_support_access_log_status", table_name="support_access_log",
        )
    for col in (
        "denied_at", "denied_by", "approved_at", "approved_by",
        "reason", "requested_at", "requested_by", "status",
    ):
        if col in present:
            op.drop_column("support_access_log", col)
