"""Widen identity/actor columns to String(128).

Gate-caught defect class (2026-08-28): columns storing WHO acted were
String(32), but a Keycloak subject is 36 chars and two of them
(licenses.issued_by, users.invited_by) receive emails. sqlite does not
enforce VARCHAR lengths, so the whole unit suite stayed green; postgres
raises StringDataRightTruncationError the moment a real subject arrives —
which the compose gate proved at tenant_services.registered_by. This
migration fixes the LEGACY databases for every merged table with a
writer-confirmed identity value; fresh databases get String(128) from the
models via 0001's create_all, and widening an already-128 column is a
no-op, so this is idempotent by construction.

String(128) is the repo's identity-width precedent (users.keycloak_user_id).
Enum-like String(32) columns (status/plan/scope/...) and 32-hex id/FK
columns are deliberately untouched — they are correct at 32.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-28
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

#: (table, column) pairs with a writer-confirmed identity value.
IDENTITY_COLUMNS = [
    ("console_audit_log", "actor_id"),
    ("delinquency_log", "actor_id"),
    ("users", "invited_by"),
    ("custom_roles", "created_by"),
    ("api_keys", "created_by"),
    ("support_tickets", "created_by"),
    ("support_tickets", "assigned_to"),
    ("ticket_messages", "author_id"),
    ("ticket_state_changes", "changed_by"),
    ("feature_flags", "updated_by"),
    ("platform_settings", "updated_by"),
    ("licenses", "issued_by"),
    ("licenses", "revoked_by"),
    ("credit_notes", "issued_by"),
    ("impersonation_log", "admin_user_id"),
]


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    tables = set(insp.get_table_names())
    for table, column in IDENTITY_COLUMNS:
        if table not in tables:
            continue
        with op.batch_alter_table(table) as batch:
            batch.alter_column(
                column, existing_type=sa.String(32), type_=sa.String(128),
            )


def downgrade() -> None:
    # Narrowing would truncate real subjects already stored; a rollback of
    # the WIDTH is never required for the old code to run (it reads and
    # writes shorter values fine). Deliberate no-op.
    pass
