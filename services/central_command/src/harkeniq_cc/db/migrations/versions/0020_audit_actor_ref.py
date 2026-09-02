"""A23-2: a stable actor identity on the audit log (spec A23.7).

  cc_audit_log.actor_ref   nullable, String(128)
  ix_cc_audit_log_tenant_actor_ref (tenant_id, actor_ref)

`actor` was written in three forms -- Keycloak subject, email, and
"email or subject" -- so the A22.10 migration census compared display
strings to subject-keyed grants and reported granted people as
ungranted. `actor_ref` carries the canonical stable reference for every
NEW row, written through the one `actor_of()` helper.

OUTSIDE THE HASH-CHAIN PAYLOAD, like `site_id` (E1.2): `AuditRepo.
_chain_payload` hashes ts, actor, action, subject, tenant_id and detail,
and only those. Adding this column changes no entry hash, and every
chain written before it still verifies.

NULLABLE WITH NO BACKFILL. A historical row's actor cannot be resolved
to a subject without the identity provider, and rewriting `actor` would
change its hash. NULL means "recorded before A23-2, or unresolvable";
readers understand both forms and never pretend a row was migrated.

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-02
"""

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

_INDEX = "ix_cc_audit_log_tenant_actor_ref"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "cc_audit_log" not in set(inspector.get_table_names()):
        return
    columns = {c["name"] for c in inspector.get_columns("cc_audit_log")}
    if "actor_ref" not in columns:
        op.add_column(
            "cc_audit_log",
            sa.Column("actor_ref", sa.String(length=128), nullable=True),
        )
    indexes = {i["name"] for i in inspector.get_indexes("cc_audit_log")}
    if _INDEX not in indexes:
        op.create_index(_INDEX, "cc_audit_log", ["tenant_id", "actor_ref"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "cc_audit_log" not in set(inspector.get_table_names()):
        return
    indexes = {i["name"] for i in inspector.get_indexes("cc_audit_log")}
    if _INDEX in indexes:
        op.drop_index(_INDEX, table_name="cc_audit_log")
    columns = {c["name"] for c in inspector.get_columns("cc_audit_log")}
    if "actor_ref" in columns:
        op.drop_column("cc_audit_log", "actor_ref")
