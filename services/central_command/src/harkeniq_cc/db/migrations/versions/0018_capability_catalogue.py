"""A4: the condition -> capability catalogue (spec A21).

  cc_capability_catalogue   which capability is a CANDIDATE for which
                            observed condition. Tenant-scoped, readable,
                            auditable.

SEEDED, and that is the point: `REMEDIATION_CANDIDATES` was a module
constant, so an existing tenant must come out of this migration able to
propose exactly what it could before -- with ONE correction A21.4
mandates. The `interface` subsystem mapped only to CLEAR_COUNTERS, which
no executor implements, so A17's zero-reach rule refused the binding and
a switch-scoped agent had no proposable action at all. It now maps to the
gNMI actions R6 actually shipped.

The seed also adds the implemented classes that had no path to an agent.
None of them becomes autonomous: A4 does not touch the autonomy ladder,
so a class that is not budget-mapped still requires a named human
(A21.5).

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-01
"""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    # 0001 is a create_all from CURRENT models, so a fresh database is
    # born with this table and only an existing one needs the create.
    if "cc_capability_catalogue" not in set(inspector.get_table_names()):
        op.create_table(
            "cc_capability_catalogue",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("tenant_id", sa.String(64), nullable=False, index=True),
            sa.Column("subsystem", sa.String(64), nullable=False, index=True),
            sa.Column("action_type", sa.String(64), nullable=False),
            sa.Column("because", sa.String(512), nullable=False, server_default=""),
            sa.Column("provenance", sa.String(255), nullable=False,
                      server_default=""),
            sa.Column("enabled", sa.Boolean(), nullable=False,
                      server_default=sa.true()),
            sa.Column("created_by", sa.String(255), nullable=False,
                      server_default=""),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_by", sa.String(255), nullable=False,
                      server_default=""),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "tenant_id", "subsystem", "action_type",
                name="uq_cc_capability_catalogue_entry",
            ),
        )

    # Seed every tenant that already has agents or sites. A tenant with
    # neither is seeded lazily on first read, so a database with no
    # tenants at all needs nothing here.
    _seed_existing_tenants(bind)


def _seed_existing_tenants(bind) -> None:
    """One catalogue per tenant that already exists.

    Read from the SEED constant rather than duplicated here: a migration
    that carried its own copy would drift from the module the runtime
    reads, and the drift would be invisible until an operator asked why
    the two disagreed.
    """
    import uuid
    from datetime import datetime, timezone

    from harkeniq_cc.capability_catalogue import SEED

    tenants = {
        row[0] for row in bind.execute(
            sa.text("SELECT DISTINCT tenant_id FROM cc_sites")
        ) if row[0]
    }
    tenants |= {
        row[0] for row in bind.execute(
            sa.text("SELECT DISTINCT tenant_id FROM cc_operational_agents")
        ) if row[0]
    }
    if not tenants:
        return

    existing = {
        (r[0], r[1], r[2]) for r in bind.execute(
            sa.text("SELECT tenant_id, subsystem, action_type "
                    "FROM cc_capability_catalogue")
        )
    }
    now = datetime.now(timezone.utc)
    for tenant_id in sorted(tenants):
        for entry in SEED:
            key = (tenant_id, entry["subsystem"], entry["action_type"])
            if key in existing:
                continue
            bind.execute(
                sa.text(
                    "INSERT INTO cc_capability_catalogue "
                    "(id, tenant_id, subsystem, action_type, because, "
                    " provenance, enabled, created_by, created_at, "
                    " updated_by, updated_at) "
                    "VALUES (:id, :t, :s, :a, :b, :p, :e, :cb, :ca, :ub, :ua)"
                ),
                {
                    "id": uuid.uuid4().hex, "t": tenant_id,
                    "s": entry["subsystem"], "a": entry["action_type"],
                    "b": entry["because"], "p": entry["provenance"],
                    "e": True, "cb": "migration-0018", "ca": now,
                    "ub": "migration-0018", "ua": now,
                },
            )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "cc_capability_catalogue" in set(inspector.get_table_names()):
        op.drop_table("cc_capability_catalogue")
