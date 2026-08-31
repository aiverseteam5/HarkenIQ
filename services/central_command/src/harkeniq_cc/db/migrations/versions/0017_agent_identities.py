"""A3: machine identity for Operational Agents (spec A20).

  cc_agent_identities   one Keycloak client-credentials service account
                        per LOGICAL agent, 1:1 with cc_operational_agents.

No secret column exists, deliberately (A20.5): Keycloak holds the secret
and Central Command shows one exactly once at issue and rotate. There is
no column it could be written into by accident.

`status` is authoritative on every request, which is what makes
revocation beat an otherwise-valid JWT immediately.

Additive, and NO BACKFILL: no agent has an identity today, so there is
nothing to infer. An agent without a row simply cannot authenticate,
which is the correct answer rather than a missing one.

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-31
"""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    # 0001 is a create_all from CURRENT models, so a fresh database is
    # born with this table and only an existing one needs the create
    # (the same guard 0002, 0005, 0013, 0014 and 0016 use).
    if "cc_agent_identities" in set(inspector.get_table_names()):
        return

    op.create_table(
        "cc_agent_identities",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
        # UNIQUE is the 1:1 guarantee: two identities for one agent would
        # be two answers to "who is this runtime?", and the second would
        # be unaccountable.
        sa.Column(
            "agent_id", sa.String(32),
            sa.ForeignKey("cc_operational_agents.id"),
            nullable=False, unique=True, index=True,
        ),
        sa.Column("realm", sa.String(128), nullable=False, server_default=""),
        sa.Column(
            "keycloak_client_id", sa.String(255), nullable=False, unique=True,
        ),
        sa.Column("keycloak_sub", sa.String(128), nullable=False,
                  server_default="", index=True),
        sa.Column("status", sa.String(16), nullable=False,
                  server_default="active", index=True),
        sa.Column("issued_by", sa.String(255), nullable=False, server_default=""),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rotated_by", sa.String(255), nullable=False, server_default=""),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.String(255), nullable=False, server_default=""),
        sa.Column("revoke_reason", sa.String(512), nullable=False,
                  server_default=""),
        # Observation only. Never an authorization input.
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_source", sa.String(255), nullable=False,
                  server_default=""),
    )
    # The authentication hot path: every machine-principal request
    # resolves (realm, keycloak_sub) before anything else happens.
    op.create_index(
        "ix_cc_agent_identities_realm_sub",
        "cc_agent_identities", ["realm", "keycloak_sub"],
    )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "cc_agent_identities" in set(inspector.get_table_names()):
        op.drop_table("cc_agent_identities")
