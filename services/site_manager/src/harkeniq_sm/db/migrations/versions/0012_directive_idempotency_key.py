"""PB-0 (A30.42/A30.43): one proposal, at most one directive.

  sm_directives.idempotency_key        nullable; `proposal:<proposal_id>`
                                       for a proposal-driven directive
  ux_sm_directives_idempotency_key     UNIQUE (idempotency_key)

`DispatchAction` inserted every request unconditionally, so a lost
acknowledgement, a second Central Command replica or two approvers
finishing together queued the same proposal twice. The key is written for
NEW directives only. Historical rows stay NULL -- duplicates the old code
made (A30.42 R3 shows they can exist) are never rewritten, and NULLs never
collide in a unique index, so this migration cannot fail on existing data.

On PostgreSQL the unique index is built CONCURRENTLY in alembic's
autocommit block, so the Site Manager keeps queueing directives while it
builds; an INVALID index left by an interrupted build is dropped and
rebuilt. Guarded and idempotent (see 0003): 0001 is a create_all from
CURRENT models, so a fresh database is born with both.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-11
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

_TABLE = "sm_directives"
_COLUMN = "idempotency_key"
_INDEX = "ux_sm_directives_idempotency_key"


def _index_state(bind):
    """None if absent, else whether the index is valid."""
    if bind.dialect.name == "postgresql":
        row = bind.execute(sa.text(
            "SELECT i.indisvalid FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = :name"
        ), {"name": _INDEX}).first()
        return None if row is None else bool(row[0])
    names = {ix["name"] for ix in sa.inspect(bind).get_indexes(_TABLE)}
    return True if _INDEX in names else None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {c["name"] for c in sa.inspect(bind).get_columns(_TABLE)}
    if _COLUMN not in columns:
        op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(128), nullable=True))
    postgres = bind.dialect.name == "postgresql"
    state = _index_state(bind)

    def work() -> None:
        if state is False:                        # an interrupted build
            op.drop_index(
                _INDEX, table_name=_TABLE,
                postgresql_concurrently=postgres, if_exists=True,
            )
        if state is not True:
            op.create_index(
                _INDEX, _TABLE, [_COLUMN], unique=True,
                postgresql_concurrently=postgres,
            )

    if postgres:
        with op.get_context().autocommit_block():
            work()
    else:
        work()


def downgrade() -> None:
    bind = op.get_bind()
    postgres = bind.dialect.name == "postgresql"
    if _index_state(bind) is not None:
        if postgres:
            with op.get_context().autocommit_block():
                op.drop_index(
                    _INDEX, table_name=_TABLE,
                    postgresql_concurrently=True, if_exists=True,
                )
        else:
            op.drop_index(_INDEX, table_name=_TABLE)
    columns = {c["name"] for c in sa.inspect(bind).get_columns(_TABLE)}
    if _COLUMN in columns:
        op.drop_column(_TABLE, _COLUMN)
