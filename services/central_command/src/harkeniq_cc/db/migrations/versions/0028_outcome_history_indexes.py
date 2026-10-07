"""A30.41: the indexes exact outcome reads stand on. Indexes only.

  ix_outcome_history_action_id    (action_id)
      settlement's keyed lookup and the receipt's -- a sequential scan
      before (225 ms -> 4 ms at 1.1M rows on the production image)
  ix_outcome_history_site         (site_id)
      site-scoped tallies and device statistics
  ix_outcome_history_device_time  (device_agent_id, recorded_at, id)
                                  INCLUDE (site_id, outcome)
      the ordered per-device decay sums, index-only and already in
      aggregate order (4.2 s -> 0.69 s, exact, over 1.1M rows); replaces
      ix_outcome_history_device, which is its prefix
  ix_outcome_history_actor        (actor varchar_pattern_ops)
      campaign wave settlement's actor read, and -- with no code change --
      the D2 budget count's `actor LIKE 'op-agent:<id>@v%'` prefix, which a
      plain btree cannot serve under a non-C collation

No column, no table, no backfill: what changes is how the rows are read,
never what they say.

On PostgreSQL every index is built CONCURRENTLY, so a large outcome table
keeps accepting the fleet poller's writes while it builds. CONCURRENTLY
cannot run inside a transaction, so the work runs in alembic's autocommit
block. An interrupted concurrent build leaves an INVALID index under the
name, which a name check alone would skip forever, so an invalid one is
dropped and rebuilt. Guarded and idempotent, the way every migration since
0010 is: 0001 is a create_all from CURRENT models, so a fresh database
already has these indexes and none of the old one.

Revision ID: 0028
Revises: 0027
"""

from alembic import op
import sqlalchemy as sa

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None

_TABLE = "cc_outcome_history"
_OLD = "ix_outcome_history_device"

#: name -> (columns, postgresql options)
_NEW = {
    "ix_outcome_history_action_id": (["action_id"], {}),
    "ix_outcome_history_site": (["site_id"], {}),
    "ix_outcome_history_device_time": (
        ["device_agent_id", "recorded_at", "id"],
        {"postgresql_include": ["site_id", "outcome"]},
    ),
    "ix_outcome_history_actor": (
        ["actor"], {"postgresql_ops": {"actor": "varchar_pattern_ops"}},
    ),
}


def _indexes(bind) -> dict[str, bool]:
    """{index name: valid} on the outcome table."""
    if bind.dialect.name == "postgresql":
        rows = bind.execute(sa.text(
            "SELECT c.relname, i.indisvalid FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid "
            "JOIN pg_class t ON t.oid = i.indrelid "
            "WHERE t.relname = :table"
        ), {"table": _TABLE}).all()
        return {name: bool(valid) for name, valid in rows}
    return {ix["name"]: True for ix in sa.inspect(bind).get_indexes(_TABLE)}


def _create(name: str, postgres: bool) -> None:
    columns, options = _NEW[name]
    op.create_index(
        name, _TABLE, columns, postgresql_concurrently=postgres, **options,
    )


def _drop(name: str, postgres: bool) -> None:
    op.drop_index(
        name, table_name=_TABLE, postgresql_concurrently=postgres,
        if_exists=True,
    )


def _apply(work) -> None:
    bind = op.get_bind()
    postgres = bind.dialect.name == "postgresql"
    existing = _indexes(bind)
    if postgres:
        with op.get_context().autocommit_block():
            work(existing, True)
    else:
        work(existing, False)


def upgrade() -> None:
    def work(existing: dict[str, bool], postgres: bool) -> None:
        for name in _NEW:
            if name in existing and not existing[name]:
                _drop(name, postgres)          # an interrupted build
                existing.pop(name)
            if name not in existing:
                _create(name, postgres)
        if _OLD in existing:
            _drop(_OLD, postgres)

    _apply(work)


def downgrade() -> None:
    def work(existing: dict[str, bool], postgres: bool) -> None:
        if _OLD not in existing:
            op.create_index(
                _OLD, _TABLE, ["device_agent_id"],
                postgresql_concurrently=postgres,
            )
        for name in _NEW:
            if name in existing:
                _drop(name, postgres)

    _apply(work)
