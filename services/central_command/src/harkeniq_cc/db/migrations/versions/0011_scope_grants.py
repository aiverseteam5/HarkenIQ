"""E1.2: scope grants -- server-side authorization for humans and agents.

Central Command had one authorization question (does this role hold this
permission) and no answer to "over which objects". This migration lands
the model that answers it.

  cc_scope_grants                one table, principal_type user|agent
  cc_tenant_settings             legacy_open | strict, per tenant
  cc_approval_records
      +scope_snapshot            ratified L2: an approval is valid on the
      +authority_snapshot        authority held AT THE TIME, so the values
                                 are recorded, not a boolean verdict
  cc_audit_log.site_id           authorization/indexing metadata

`cc_agent_scopes` is COPIED IN as principal_type="agent" and dropped.
Every A0 agent keeps exactly the reach it had: same scope types, same
refs. One resolver now serves humans and agents, which is the point.

`cc_audit_log.site_id` sits OUTSIDE the hash-chain payload.
`AuditRepo._chain_payload` hashes ts, actor, action, subject, tenant_id
and detail, and nothing else, so adding a column it does not name leaves
every existing chain verifiable. A test asserts that rather than
trusting it. Pre-E1.2 rows read as tenant-level: the site was never
recorded and cannot be invented now.

Existing tenants land `legacy_open`, which is today's behaviour exactly.
Central Command cannot enumerate a realm's principals to backfill
grants, so treating the absence of a grant as a decision would lock out
every existing deployment on upgrade.

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-30
"""

import uuid
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 is a create_all from CURRENT models, so a fresh database is born
    # with these objects; only pre-E1.2 databases need the DDL.
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("cc_scope_grants"):
        op.create_table(
            "cc_scope_grants",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column("principal_type", sa.String(16), server_default="user"),
            sa.Column("principal_ref", sa.String(128), nullable=False),
            sa.Column("scope_type", sa.String(16), nullable=False),
            sa.Column("scope_ref", sa.String(128), server_default=""),
            sa.Column("permission_subset", sa.JSON(), nullable=True),
            # The role this grant narrows, as named by the grantor. Not
            # the authorization input (the token's role is), but what
            # lets the L1 strict preflight answer "would anybody still
            # hold role.manage at tenant scope" without enumerating a
            # Keycloak realm -- which CC deliberately cannot do.
            sa.Column("role", sa.String(64), server_default=""),
            # 255 not 32: a Keycloak subject is a 36-character UUID, and
            # sqlite ignores VARCHAR length so only PostgreSQL would have
            # caught it (the E0.1 lesson, guarded by a width invariant test).
            sa.Column("granted_by", sa.String(255), server_default=""),
            sa.Column("granted_at", sa.DateTime(timezone=True)),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked_by", sa.String(255), server_default=""),
            sa.Column("note", sa.String(512), server_default=""),
            sa.UniqueConstraint(
                "tenant_id", "principal_type", "principal_ref",
                "scope_type", "scope_ref",
                name="uq_scope_grant_principal_scope",
            ),
        )
        op.create_index(
            "ix_cc_scope_grants_tenant_id", "cc_scope_grants", ["tenant_id"]
        )
        op.create_index(
            "ix_cc_scope_grants_principal_ref", "cc_scope_grants", ["principal_ref"]
        )
        op.create_index(
            "ix_scope_grants_principal", "cc_scope_grants",
            ["tenant_id", "principal_ref"],
        )

    if not inspector.has_table("cc_tenant_settings"):
        op.create_table(
            "cc_tenant_settings",
            sa.Column("tenant_id", sa.String(64), primary_key=True),
            sa.Column(
                "scope_enforcement", sa.String(16), server_default="legacy_open"
            ),
            sa.Column("updated_by", sa.String(255), server_default=""),
            sa.Column("updated_at", sa.DateTime(timezone=True)),
        )

    record_columns = {
        c["name"] for c in inspector.get_columns("cc_approval_records")
    }
    for column in ("scope_snapshot", "authority_snapshot"):
        if column not in record_columns:
            op.add_column(
                "cc_approval_records", sa.Column(column, sa.JSON(), nullable=True)
            )

    audit_columns = {c["name"] for c in inspector.get_columns("cc_audit_log")}
    if "site_id" not in audit_columns:
        op.add_column(
            "cc_audit_log", sa.Column("site_id", sa.String(32), nullable=True)
        )
        op.create_index("ix_cc_audit_log_site_id", "cc_audit_log", ["site_id"])

    _migrate_agent_scopes(bind, inspector)
    _seed_enforcement(bind)


def _migrate_agent_scopes(bind, inspector) -> None:
    """Copy `cc_agent_scopes` into the one grant table, then drop it.

    Re-runnable: a scope already present as a grant is skipped, so a
    retry cannot double-grant an agent.
    """
    if not inspector.has_table("cc_agent_scopes"):
        return

    now = datetime.now(timezone.utc)
    rows = bind.execute(
        sa.text(
            "SELECT agent_id, tenant_id, scope_type, scope_ref FROM cc_agent_scopes"
        )
    ).fetchall()

    for agent_id, tenant_id, scope_type, scope_ref in rows:
        existing = bind.execute(
            sa.text(
                "SELECT id FROM cc_scope_grants WHERE tenant_id = :t "
                "AND principal_type = 'agent' AND principal_ref = :p "
                "AND scope_type = :st AND scope_ref = :sr"
            ),
            {"t": tenant_id, "p": agent_id, "st": scope_type, "sr": scope_ref or ""},
        ).fetchone()
        if existing:
            continue
        bind.execute(
            sa.text(
                # Every non-nullable column is named explicitly: on a
                # database born from 0001's create_all the defaults are
                # Python-side, not server-side, so an omitted column is
                # a NOT NULL violation rather than a default.
                "INSERT INTO cc_scope_grants "
                "(id, tenant_id, principal_type, principal_ref, scope_type, "
                " scope_ref, permission_subset, role, granted_by, granted_at, "
                " revoked_by, note) "
                "VALUES (:id, :t, 'agent', :p, :st, :sr, NULL, '', "
                " 'migration:0011', :now, '', 'migrated from cc_agent_scopes')"
            ),
            {
                "id": uuid.uuid4().hex,
                "t": tenant_id,
                "p": agent_id,
                "st": scope_type,
                "sr": scope_ref or "",
                "now": now,
            },
        )

    op.drop_table("cc_agent_scopes")


def _seed_enforcement(bind) -> None:
    """Every tenant that already has data lands `legacy_open`.

    Behaviour is unchanged on upgrade. A tenant adopts scoping by
    granting principals first and flipping second, and the flip itself
    is refused if it would leave the tenant with no administrator.
    """
    now = datetime.now(timezone.utc)
    tenants = {
        row[0]
        for row in bind.execute(
            sa.text("SELECT DISTINCT tenant_id FROM cc_sites WHERE tenant_id IS NOT NULL")
        ).fetchall()
    }
    tenants |= {
        row[0]
        for row in bind.execute(
            sa.text(
                "SELECT DISTINCT tenant_id FROM cc_org_units WHERE tenant_id IS NOT NULL"
            )
        ).fetchall()
    }
    for tenant_id in tenants:
        existing = bind.execute(
            sa.text("SELECT tenant_id FROM cc_tenant_settings WHERE tenant_id = :t"),
            {"t": tenant_id},
        ).fetchone()
        if existing:
            continue
        bind.execute(
            sa.text(
                "INSERT INTO cc_tenant_settings "
                "(tenant_id, scope_enforcement, updated_by, updated_at) "
                "VALUES (:t, 'legacy_open', 'migration:0011', :now)"
            ),
            {"t": tenant_id, "now": now},
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("cc_agent_scopes"):
        op.create_table(
            "cc_agent_scopes",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("agent_id", sa.String(32), nullable=False),
            sa.Column("tenant_id", sa.String(64), nullable=False),
            sa.Column("scope_type", sa.String(16), nullable=False),
            sa.Column("scope_ref", sa.String(128), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint(
                "agent_id", "scope_type", "scope_ref", name="uq_agent_scope"
            ),
        )
        op.get_bind().execute(
            sa.text(
                "INSERT INTO cc_agent_scopes "
                "(id, agent_id, tenant_id, scope_type, scope_ref, created_at) "
                "SELECT id, principal_ref, tenant_id, scope_type, scope_ref, granted_at "
                "FROM cc_scope_grants WHERE principal_type = 'agent'"
            )
        )
    audit_columns = {c["name"] for c in inspector.get_columns("cc_audit_log")}
    if "site_id" in audit_columns:
        op.drop_index("ix_cc_audit_log_site_id", "cc_audit_log")
        op.drop_column("cc_audit_log", "site_id")
    for column in ("authority_snapshot", "scope_snapshot"):
        op.drop_column("cc_approval_records", column)
    op.drop_table("cc_tenant_settings")
    op.drop_table("cc_scope_grants")
