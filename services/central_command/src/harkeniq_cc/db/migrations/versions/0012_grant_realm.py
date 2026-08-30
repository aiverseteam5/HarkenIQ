"""E1.4: a scope grant is a (realm, subject) fact.

`cc_scope_grants.principal_ref` holds a Keycloak subject, and Keycloak
subjects are REALM-SCOPED: the same id means nothing across realms, and
the same person has a different id in each. Keyed on the subject alone,
moving a tenant onto its own realm silently orphaned every grant -- and
under strict enforcement that locked the tenant out completely,
including the administrator who would have re-granted.

  cc_scope_grants.realm    which realm this subject belongs to

Backfilled to the Central Command's configured realm, which is where
every existing grant was made, so an upgrade changes nothing. A grant
whose realm is not the one this Central Command serves no longer
authorizes -- a grant made in another realm authorizing here would be a
cross-realm authorization bug.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-30
"""

import os

from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {c["name"] for c in inspector.get_columns("cc_scope_grants")}
    if "realm" not in columns:
        op.add_column(
            "cc_scope_grants",
            sa.Column("realm", sa.String(128), server_default=""),
        )
        op.create_index(
            "ix_scope_grants_realm", "cc_scope_grants", ["tenant_id", "realm"]
        )

    # Existing grants were made under whatever realm this Central Command
    # was configured for. Recording that is what makes a later realm
    # change visible instead of silent.
    realm = os.environ.get("HARKEN_CC_KEYCLOAK_REALM", "")
    if realm:
        bind.execute(
            sa.text(
                "UPDATE cc_scope_grants SET realm = :r "
                "WHERE realm IS NULL OR realm = ''"
            ),
            {"r": realm},
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {c["name"] for c in inspector.get_columns("cc_scope_grants")}
    if "realm" in columns:
        op.drop_index("ix_scope_grants_realm", "cc_scope_grants")
        op.drop_column("cc_scope_grants", "realm")
