"""PB-0 (A30.42/A30.43): the dispatch claim, and the A30.42 indexes.

  cc_agent_proposals.dispatch_claimed_at   nullable timestamptz -- the
      per-proposal claim one conditional UPDATE takes, and commits, before
      Central Command calls the Site Manager, so one approved proposal is
      sent by at most one dispatcher at a time
  ix_agent_proposals_tenant_status_created     (tenant_id, status, created_at, id)
      the keyset dispatch selection over EVERY approved proposal, which
      replaced the oldest-500 read
  ix_agent_proposals_tenant_agent_created      (tenant_id, agent_id, created_at)
  ix_agent_proposals_tenant_agent_basis_dispatched
                                               (tenant_id, agent_id,
                                                authorization_basis, dispatched_at)
      PB-1's admission and unattended counts -- ratified with this
      migration, shipped with it

Additive, nullable, NO backfill: a proposal decided before PB-0 was never
claimed, and writing a timestamp onto it would invent a dispatch attempt
nobody made. Historical rows are never rewritten.

On PostgreSQL every index is built CONCURRENTLY in alembic's autocommit
block, so the proposal table keeps accepting writes while it builds; an
INVALID index left by an interrupted build is dropped and rebuilt. Guarded
and idempotent, as every migration since 0010: 0001 is a create_all from
CURRENT models, so a fresh database already has the column and the indexes.

Revision ID: 0029
Revises: 0028
"""

from alembic import op
import sqlalchemy as sa

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None

_TABLE = "cc_agent_proposals"
_COLUMN = "dispatch_claimed_at"

#: name -> columns
_NEW = {
    "ix_agent_proposals_tenant_status_created": [
        "tenant_id", "status", "created_at", "id",
    ],
    "ix_agent_proposals_tenant_agent_created": [
        "tenant_id", "agent_id", "created_at",
    ],
    "ix_agent_proposals_tenant_agent_basis_dispatched": [
        "tenant_id", "agent_id", "authorization_basis", "dispatched_at",
    ],
}


def _indexes(bind) -> dict[str, bool]:
    """{index name: valid} on the proposal table."""
    if bind.dialect.name == "postgresql":
        rows = bind.execute(sa.text(
            "SELECT c.relname, i.indisvalid FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid "
            "JOIN pg_class t ON t.oid = i.indrelid "
            "WHERE t.relname = :table"
        ), {"table": _TABLE}).all()
        return {name: bool(valid) for name, valid in rows}
    return {ix["name"]: True for ix in sa.inspect(bind).get_indexes(_TABLE)}


def _columns(bind) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(_TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if _COLUMN not in _columns(bind):
        op.add_column(
            _TABLE, sa.Column(_COLUMN, sa.DateTime(timezone=True), nullable=True),
        )
    postgres = bind.dialect.name == "postgresql"
    existing = _indexes(bind)

    def work() -> None:
        for name, columns in _NEW.items():
            if name in existing and not existing[name]:
                op.drop_index(                     # an interrupted build
                    name, table_name=_TABLE,
                    postgresql_concurrently=postgres, if_exists=True,
                )
                existing.pop(name)
            if name not in existing:
                op.create_index(
                    name, _TABLE, columns, postgresql_concurrently=postgres,
                )

    if postgres:
        with op.get_context().autocommit_block():
            work()
    else:
        work()


def downgrade() -> None:
    bind = op.get_bind()
    postgres = bind.dialect.name == "postgresql"
    existing = _indexes(bind)

    def work() -> None:
        for name in _NEW:
            if name in existing:
                op.drop_index(
                    name, table_name=_TABLE,
                    postgresql_concurrently=postgres, if_exists=True,
                )

    if postgres:
        with op.get_context().autocommit_block():
            work()
    else:
        work()
    if _COLUMN in _columns(bind):
        op.drop_column(_TABLE, _COLUMN)
