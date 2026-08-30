"""RBAC permissions and fixed role definitions.

Seven fixed roles (spec S4) mapped to 24 atomic permissions. Tenants
may define custom roles via ``CustomRole`` records; the
``has_permission`` helper merges fixed + custom grants.
"""

from __future__ import annotations

from typing import Optional

# ── atomic permissions ──────────────────────────────────────────────
PERMISSIONS: dict[str, str] = {
    "tenant.manage":          "Create, update, suspend tenants",
    "tenant.view":            "View tenant details",
    "user.manage":            "Invite, update, remove users",
    "user.view":              "View user list and profiles",
    "role.manage":            "Create and assign custom roles",
    "site.manage":            "Register and configure sites",
    "site.view":              "View site details",
    "fleet.view":             "View fleet-wide device status",
    "action.approve":         "Approve or deny proposed actions",
    "incident.view":          "View incidents",
    "incident.acknowledge":   "Acknowledge incidents",
    "billing.manage":         "Manage subscriptions and payments",
    "billing.view":           "View invoices and usage",
    "license.manage":         "Generate and revoke licenses",
    "license.view":           "View license details",
    "support.manage":         "Manage support tickets (staff)",
    "support.create":         "Create support tickets",
    "support.view":           "View support tickets",
    "audit.view":             "View audit logs",
    "audit.export":           "Export audit logs",
    "admin.dashboard":        "Access platform admin dashboard",
    # R4-3: community skill marketplace (OQ-22)
    "skill.submit":           "Submit skills to the marketplace",
    "skill.review":           "Review and promote marketplace skills (staff)",
    "skill.install":          "Install marketplace skills for a tenant",
}

# ── fixed roles ─────────────────────────────────────────────────────
ROLE_PERMISSIONS: dict[str, set[str]] = {
    "platform_super_admin": set(PERMISSIONS.keys()),

    "platform_support": {
        "tenant.view",
        "support.manage",
        "support.view",
        "audit.view",
    },

    "tenant_owner": {
        "tenant.view",
        "user.manage",
        "user.view",
        "role.manage",
        "site.manage",
        "site.view",
        "fleet.view",
        "action.approve",
        "incident.view",
        "incident.acknowledge",
        "billing.manage",
        "billing.view",
        "license.view",
        "support.create",
        "support.view",
        "audit.view",
        "audit.export",
        "skill.submit",
        "skill.install",
    },

    "site_admin": {
        "site.manage",
        "site.view",
        "fleet.view",
        "action.approve",
        "incident.view",
        "incident.acknowledge",
        "user.view",
    },

    "operator": {
        "fleet.view",
        "action.approve",
        "incident.view",
        "incident.acknowledge",
        "support.create",
        "support.view",
        "skill.submit",
    },

    # A13 (OQ-24, decided by Vinod 2026-08-28): the auditor is read-only
    # EVERYTHING — every atomic *.view plus audit.export, and nothing else.
    # No mutation, no approvals, no elevation, no tenant.view (that is a
    # platform-plane read since A11.4; the auditor's own-tenant name comes
    # from /api/me/tenants). The custom-role ceiling does not move:
    # tenant_owner already holds every permission granted here.
    "auditor": {
        "fleet.view",
        "incident.view",
        "billing.view",
        "audit.view",
        "audit.export",
        "user.view",
        "site.view",
        "license.view",
        "support.view",
    },

    "viewer": {
        "fleet.view",
        "incident.view",
    },
}


def has_permission(
    role: str,
    permission: str,
    custom_permissions: Optional[list[str]] = None,
) -> bool:
    """Check whether *role* (plus optional custom grants) includes *permission*."""
    role_perms = ROLE_PERMISSIONS.get(role, set())
    if permission in role_perms:
        return True
    # Custom grants are already intersected with the role by
    # `effective_permissions`, so this can only ever confirm something the
    # role also holds. Kept as a separate branch because a caller may
    # legitimately pass a narrower set than the role's own.
    if custom_permissions and permission in custom_permissions:
        return True
    return False


async def effective_permissions(
    session, user_id: str, email: str = "", tenant_id: Optional[str] = None,
    role: str = "",
) -> list[str]:
    """Custom-role grants for the JWT subject *user_id*, if any.

    Spec S4 allows tenants to define permission bundles and "assign them
    like fixed roles". The tables and the assignment API shipped in R2b,
    but nothing ever loaded the grants into a request, so
    has_permission's custom branch was dead in production and an assigned
    bundle granted nothing.

    Returns [] for an unknown subject: a token whose user row is missing
    keeps exactly its fixed-role permissions, never more.
    """
    from harkeniq_console.db.repos import CustomRoleRepo, UserRepo

    repo = UserRepo(session)
    local = await repo.get_by_keycloak_id(user_id) if user_id else None
    if local is None and email and tenant_id:
        # Fallback for realms whose access tokens carry no `sub` claim
        # (Keycloak 24+ moved that mapper into the `basic` client scope,
        # which this deployment's realm import omitted). Scoped to the
        # caller's own tenant, so it can only ever find the same person.
        local = await repo.get_by_email(tenant_id, email)
    if local is None:
        return []
    grants: set[str] = set()
    for bundle in await CustomRoleRepo(session).get_user_custom_roles(local.id):
        for perm in bundle.permissions or []:
            # A bundle can never name a permission outside the vocabulary.
            if perm in PERMISSIONS:
                grants.add(perm)

    # E1.4: a bundle INTERSECTS the holder's role; it never widens it.
    #
    # It used to be OR-ed with the role in `has_permission`, so a tenant
    # could define a bundle naming any permission in the vocabulary --
    # including platform-side ones like tenant.manage -- and assign it to
    # a viewer. That is the same widening E1.2 refused for
    # `permission_subset`, and the rule is the same here: a bundle
    # re-shapes authority WITHIN a role, and can only ever remove.
    if role:
        ceiling = ROLE_PERMISSIONS.get(role, set())
        grants &= set(ceiling)
    return sorted(grants)
