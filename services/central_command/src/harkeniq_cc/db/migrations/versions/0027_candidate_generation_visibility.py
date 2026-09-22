"""A30.29: generation provenance on candidate skills.

  cc_candidate_skills.generation_visibility
      the projection boundary a candidate's YAML was generated from, as
      the Site Manager recorded it -- {"scope": "site"|"tenant",
      "site_id": <cc site id>|null, "projection_version": 1}

A candidate's YAML is model-generated from a prompt that carried the
fleet patterns pushed to the Site Manager. Since A30.28 a pushed pattern
is one site's bounded projection; before it, it was the whole tenant's.
Once written, the YAML does not say which it saw -- so the writer records
it beside the text, and the reader compares the record against the
reader's CURRENT canonical reach.

NULLABLE with NO backfill and NO default. A row from before this
migration, or from a Site Manager that predates the marker, reads
UNKNOWN: withheld from every scoped reader, kept for a tenant-wide one.
Backfilling `tenant` would assert something nobody recorded; backfilling
the row's own `site_id` would assert the opposite of what a pre-A30.28
push delivered. Neither is provable, so neither is done.

The incident explanation needs no column: it is a JSON document at both
services and carries its marker INSIDE it, written in the same assignment
as the generated text.

Revision ID: 0027
Revises: 0026
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None

JSONVariant = sa.JSON().with_variant(JSONB(), "postgresql")

_TABLE = "cc_candidate_skills"
_COLUMN = "generation_visibility"


def _existing(bind) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(_TABLE)}


def upgrade() -> None:
    # Guarded and idempotent, the way every migration since 0010 is: 0001
    # is a create_all from CURRENT models, so a fresh database already
    # has the column.
    if _COLUMN in _existing(op.get_bind()):
        return
    op.add_column(_TABLE, sa.Column(_COLUMN, JSONVariant, nullable=True))


def downgrade() -> None:
    if _COLUMN in _existing(op.get_bind()):
        op.drop_column(_TABLE, _COLUMN)
