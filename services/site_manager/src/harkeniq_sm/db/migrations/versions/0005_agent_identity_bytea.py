"""QA-040: agent_identities key/certificate columns become BYTEA.

The R3a model mapped ``bytes`` onto Text columns. sqlite (every unit
test) stores bytes into Text silently; postgres/asyncpg raises DataError,
so RegisterAgent crashed on the real compose stack and NO agent identity
was ever persisted — the columns can only hold binary content (the
certificate is canonical JSON + a raw Ed25519 signature, not UTF-8).

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-26
"""

from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_COLUMNS = ("public_key_pem", "certificate")


def upgrade() -> None:
    bind = op.get_bind()
    # sqlite is dynamically typed: bytes round-trip through a Text column
    # unchanged, and 0001's create_all from current models already declares
    # BLOB on fresh databases. Only postgres needs the ALTER.
    if bind.dialect.name != "postgresql":
        return
    inspector = sa.inspect(bind)
    cols = {c["name"]: c["type"] for c in inspector.get_columns("agent_identities")}
    for name in _COLUMNS:
        # Idempotence is mandatory for this chain (see 0003): skip columns
        # already binary — fresh DBs born from create_all land here too.
        if isinstance(cols.get(name), sa.LargeBinary):
            continue
        # No rows can exist with text data on postgres (every insert
        # failed), but convert defensively rather than assuming empty.
        op.alter_column(
            "agent_identities",
            name,
            type_=sa.LargeBinary(),
            postgresql_using=f"convert_to({name}, 'UTF8')",
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for name in _COLUMNS:
        # 'escape', not convert_from: the certificate contains a raw Ed25519
        # signature that is not valid UTF-8. The pre-0005 schema could never
        # actually hold data on postgres (that is the bug), so any downgrade
        # representation is best-effort by construction.
        op.alter_column(
            "agent_identities",
            name,
            type_=sa.Text(),
            postgresql_using=f"encode({name}, 'escape')",
        )
