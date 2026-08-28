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
    # Keyed on the status column specifically, not "any column added": a
    # partially-applied schema must never re-run the backfill and clobber
    # real approval-flow rows (a denied row must not resurrect as a live
    # grant). Review finding, data-migration pass.
    status_added = "status" not in present
    for name, make in NEW_COLUMNS:
        if name not in present:
            op.add_column("support_access_log", make())

    if not status_added:
        # Fresh database (or already migrated): the state model is present
        # and there are no legacy rows to reinterpret.
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
         WHERE status = 'requested' AND requested_by = ''
        """
    )

    op.create_index(
        "ix_support_access_log_status", "support_access_log", ["status"],
    )
    # One pending request per engineer per tenant — the DB invariant the
    # application's read-then-insert check only hoped for (red team).
    op.create_index(
        "uq_support_access_pending",
        "support_access_log",
        ["tenant_id", "requested_by"],
        unique=True,
        postgresql_where=sa.text("status = 'requested'"),
        sqlite_where=sa.text("status = 'requested'"),
    )

    # expires_at is nullable now: a pending request has no clock until it
    # is approved. requested_at is tightened to match the model (every
    # legacy row was just backfilled from enabled_at), so migrated and
    # fresh databases converge on the same shape.
    with op.batch_alter_table("support_access_log") as batch:
        batch.alter_column(
            "expires_at", existing_type=sa.DateTime(timezone=True), nullable=True,
        )
        batch.alter_column(
            "requested_at", existing_type=sa.DateTime(timezone=True),
            nullable=False,
        )


def downgrade() -> None:
    """Lossy by necessity: approval/denial provenance cannot survive a
    schema without those columns. Rows the pre-0003 code cannot represent
    (no clock yet) are removed, and expires_at is restored to NOT NULL so
    the rolled-back model's contract holds."""
    present = _existing_columns()
    if "status" in present:
        op.execute(
            "DELETE FROM support_access_log WHERE expires_at IS NULL"
        )
        op.drop_index(
            "uq_support_access_pending", table_name="support_access_log",
        )
        op.drop_index(
            "ix_support_access_log_status", table_name="support_access_log",
        )
    for col in (
        "denied_at", "denied_by", "approved_at", "approved_by",
        "reason", "requested_at", "requested_by", "status",
    ):
        if col in present:
            op.drop_column("support_access_log", col)
    with op.batch_alter_table("support_access_log") as batch:
        batch.alter_column(
            "expires_at", existing_type=sa.DateTime(timezone=True),
            nullable=False,
        )
