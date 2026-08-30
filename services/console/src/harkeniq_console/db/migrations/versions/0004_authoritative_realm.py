"""E1.4: the tenant<->realm binding becomes authoritative.

`tenants.keycloak_realm` has existed since R2b and was written once and
read only for display. Authorization resolved a realm to a tenant by
SLUG instead, and the two agreed only because `create_realm(req.slug)`
happens to name the realm after the slug.

That is the same shape E0.2 fixed for sites, where `cc_site_id` was
received and discarded while resolution used a different id space:
rename a slug and the binding silently breaks, and a tenant whose slug
matched another tenant's realm name would resolve to the wrong tenant.

  unique(keycloak_realm)   two tenants can never claim one realm, which
                           is what makes resolution unambiguous
  backfill NULL -> slug    the controlled migration for tenants created
                           before E1.4, when realm provisioning had no
                           caller and every tenant was born realm-less

After the backfill every tenant has a recorded binding, so the slug
leaves the identity path entirely rather than remaining a permanent
alternative identity mechanism.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-30
"""

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # The controlled backfill. A tenant created before E1.4 has no realm
    # at all; its realm, once provisioned, is named for its slug, so the
    # slug is the correct recorded value -- and recording it now is what
    # lets resolution stop consulting the slug afterwards.
    bind.execute(
        sa.text(
            "UPDATE tenants SET keycloak_realm = slug "
            "WHERE keycloak_realm IS NULL OR keycloak_realm = ''"
        )
    )

    inspector = sa.inspect(bind)
    existing = {ix["name"] for ix in inspector.get_indexes("tenants")}
    if "uq_tenants_keycloak_realm" not in existing:
        op.create_index(
            "uq_tenants_keycloak_realm", "tenants", ["keycloak_realm"],
            unique=True,
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = {ix["name"] for ix in inspector.get_indexes("tenants")}
    if "uq_tenants_keycloak_realm" in existing:
        op.drop_index("uq_tenants_keycloak_realm", "tenants")
