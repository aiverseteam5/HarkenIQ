"""A23-5: pin the existing tenant's posture before strict birth (A23.11/A23.14).

  cc_tenant_settings  <- one explicit row for the configured tenant
                         when it has none, value `legacy_open`

The default `missing row -> legacy_open` is retired in the same slice
(`TenantSettingsRepo.enforcement`). Retiring it without pinning first
would silently convert every historically legacy tenant to strict on
upgrade -- which is exactly the lockout E1.2 seeded `legacy_open` to
avoid. So this migration states what the default has been implying since
E1.2, and only then does the code stop implying it.

WHICH TENANT (A23.14 D1). Central Command is single-tenant software
(doc 01 SS7): every request resolves `config.tenant_id`, and CC holds no
tenant table -- the authoritative registry is the Console's `tenants`, in
another service and another database that this migration cannot and must
not read. So `HARKEN_CC_TENANT_ID` is read here as the deployment's
authoritative tenant IDENTITY, never as an inventory. Operational tables
are deliberately NOT enumerated: inferring "a tenant exists because a
site row mentions it" misses a quiet tenant entirely, which is the case
this migration exists to cover.

Migration 0012 is the precedent for reading configuration at migration
time; 0011 is the precedent for the row shape and the `migration:NNNN`
attribution.

WHAT IS PRESERVED. An explicit row of EITHER posture is left exactly as
it is -- including a row 0011 seeded, which is already an explicit pin,
and a row a human has since set. This migration only ever fills an
ABSENCE.

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-03
"""

import os
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

#: Attribution for the rows this migration writes. Defined once: the
#: downgrade deletes on this exact string, so upgrade and downgrade
#: cannot drift (the chain has both `migration:0011` and `migration-0018`).
_TAG = "migration:0021"

#: The historical posture. An existing tenant has been answering
#: `legacy_open` from the missing-row default since E1.2, so this pin
#: changes no behaviour -- it makes the answer explicit before the
#: default is retired.
_HISTORICAL_POSTURE = "legacy_open"


def _configured_tenant() -> str:
    """The deployment's own tenant identity, or "" when unset.

    Unset is normal in a unit test and in `alembic upgrade` run outside
    a configured container; the migration then pins nothing and remains
    correct, because a deployment with no tenant identity has no tenant
    to lock out.
    """
    return (os.environ.get("HARKEN_CC_TENANT_ID") or "").strip()


def _has_history(bind, inspector) -> bool:
    """Has this Central Command ever recorded anything?

    A fresh database and an upgraded one are otherwise indistinguishable
    from inside this migration: on a new deployment alembic runs
    0001..0021 in ONE invocation, so "was the schema at 0020 a moment
    ago" is not observable here.

    The audit log answers the question that actually matters. It is
    Central Command's record of every act it has taken, so a database
    with no entries has never served anybody, has no posture anybody
    relies on, and has nothing for this migration to preserve -- its
    tenant is born strict. A database with entries has been served by a
    pre-A23-5 binary under the `legacy_open` default, and that posture
    is pinned.

    This asks whether the DATABASE is new. It is deliberately NOT an
    enumeration of tenants: the tenant identity comes from the
    deployment's configuration and from nowhere else (A23.14 D1).
    """
    if "cc_audit_log" not in set(inspector.get_table_names()):
        return False
    return bind.execute(
        sa.text("SELECT 1 FROM cc_audit_log LIMIT 1")
    ).fetchone() is not None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "cc_tenant_settings" not in set(inspector.get_table_names()):
        return

    tenant_id = _configured_tenant()
    if not tenant_id:
        return

    if not _has_history(bind, inspector):
        # A brand-new deployment. Pinning it `legacy_open` here would
        # hand every fresh install the permissive posture and defeat the
        # slice: strict birth would never once happen.
        return

    existing = bind.execute(
        sa.text("SELECT tenant_id FROM cc_tenant_settings WHERE tenant_id = :t"),
        {"t": tenant_id},
    ).fetchone()
    if existing:
        # Already explicit -- 0011's seed, or an operator's own choice.
        # Never overwritten: this migration fills absences only.
        return

    bind.execute(
        sa.text(
            "INSERT INTO cc_tenant_settings "
            "(tenant_id, scope_enforcement, updated_by, updated_at) "
            "VALUES (:t, :mode, :tag, :now)"
        ),
        {
            "t": tenant_id,
            "mode": _HISTORICAL_POSTURE,
            "tag": _TAG,
            "now": datetime.now(timezone.utc),
        },
    )


def downgrade() -> None:
    """Remove only the rows this migration wrote.

    A row a human has since re-set carries their attribution, not
    `_TAG`, and is deliberately retained -- it is their decision, not
    this migration's. The table itself belongs to 0011.

    Note that the default flip is CODE, not schema: downgrading the
    schema alone leaves a running A23-5 binary still answering `strict`
    for a missing row, so a downgrade is only meaningful together with a
    code rollback.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "cc_tenant_settings" not in set(inspector.get_table_names()):
        return
    bind.execute(
        sa.text("DELETE FROM cc_tenant_settings WHERE updated_by = :tag"),
        {"tag": _TAG},
    )
