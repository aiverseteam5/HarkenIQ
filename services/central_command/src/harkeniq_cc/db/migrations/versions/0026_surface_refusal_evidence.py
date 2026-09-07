"""A29.16 (A6-4A): bounded, attributable refusal evidence.

An authenticated off-plane refusal was durably CHARGED -- it moved
`reads` -- and nothing recorded who was refused or why. The only refusal
signal was a process-local counter, and `/metrics` is unauthenticated, so
a tenant or agent id may never be a label there (A25.11). That is exactly
the attribution an operator needs to find the misconfigured runtime.

The evidence therefore rides the row that already exists. No new table, no
row per refusal: storage stays bounded by (tenant, agent, window), so a
flood costs one row per minute and then 429s -- the property A24.13 and
A27.13 both refused to trade away.

Additive and nullable-safe, with NO backfill. A window that closed before
this migration recorded no refusals, and defaulting its counters to
anything but zero would assert a fact nobody measured (A27.4).

Revision ID: 0026
Revises: 0025
"""

from alembic import op
import sqlalchemy as sa

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

_TABLE = "cc_agent_read_windows"

#: The CLOSED reason vocabulary, one column each. A bounded set in the
#: schema cannot be widened by a caller, and no path, query, body or error
#: text is ever stored.
_COLUMNS = (
    ("surface_refused", sa.Integer(), "0"),
    ("refused_surface_not_allowed", sa.Integer(), "0"),
    ("refused_job_not_bound", sa.Integer(), "0"),
)


def _existing(bind) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(_TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    have = _existing(bind)
    # Guarded and idempotent, the way every migration since 0010 is.
    for name, type_, default in _COLUMNS:
        if name in have:
            continue
        op.add_column(
            _TABLE,
            sa.Column(name, type_, nullable=False, server_default=default),
        )
    if "last_surface_refused_at" not in have:
        op.add_column(
            _TABLE,
            sa.Column(
                "last_surface_refused_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )
    # Deliberately NO backfill and NO new index: every read of this
    # evidence is already keyed by (tenant_id, agent_id, window_start),
    # which the unique constraint already serves.


def downgrade() -> None:
    bind = op.get_bind()
    have = _existing(bind)
    for name in ("last_surface_refused_at", *[c[0] for c in _COLUMNS]):
        if name in have:
            op.drop_column(_TABLE, name)
