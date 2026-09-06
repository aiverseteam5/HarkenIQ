"""A27 (A6-3): first-class proposal provenance.

`origin` reached `admit_proposal()` and was written ONLY into the
audit-entry detail JSON, so no approver could tell whether HarkenIQ's own
loop reasoned a proposal or an external runtime asked for it without
querying a hash-chained store by subject id.

Additive and nullable, with **NO BACKFILL** (A27.4). A proposal created
before this column existed has no authoritative provenance; it reads NULL
and projects as `unknown`. Defaulting it to `evaluator` would assert a
fact nobody checked -- the error A19.9 refused when a naive rule would
have shouted DRIFT at every pre-A2 agent.

Revision ID: 0024
Revises: 0023
"""

from alembic import op
import sqlalchemy as sa

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

_TABLE = "cc_agent_proposals"
_COLUMN = "provenance_type"


def _has_column(bind) -> bool:
    return _COLUMN in {
        c["name"] for c in sa.inspect(bind).get_columns(_TABLE)
    }


def upgrade() -> None:
    bind = op.get_bind()
    # Guarded and idempotent, the way every migration since 0010 is: a
    # re-run must be a no-op rather than a failure.
    if _has_column(bind):
        return
    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, sa.String(length=32), nullable=True),
    )
    # Deliberately NO UPDATE. See A27.4.


def downgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind):
        return
    op.drop_column(_TABLE, _COLUMN)
