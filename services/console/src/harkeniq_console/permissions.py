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

    "auditor": {
        "fleet.view",
        "incident.view",
        "billing.view",
        "audit.view",
        "audit.export",
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
    if custom_permissions and permission in custom_permissions:
        return True
    return False
