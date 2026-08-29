/**
 * Route access rules, mirroring the server.
 *
 * Spec S4: "Permission checks are enforced server-side per request; the UI
 * only reflects them." This module is the reflection — one map used by both
 * the sidebar and the route guards, so a nav entry and its page can never
 * disagree about who may see it.
 *
 * Permission names are the Console's own atomic set
 * (harkeniq_console/permissions.py). `platformOnly` mirrors endpoints
 * guarded by require_super_admin, which checks the role directly rather
 * than a permission, so no bundle can satisfy it.
 */

import type { AuthUser } from "./useAuth";

export interface AccessRule {
  perm?: string;
  /** super admin only (role check, no bundle can satisfy it) */
  platformOnly?: boolean;
  /** any PLATFORM user holding `perm` — mirrors the server's
   *  require_platform_permission: tenant.view is shared vocabulary
   *  (tenant_owner holds it), so a bare perm check leaked the registry
   *  page to customers [review CRITICAL]. */
  platform?: boolean;
}

export const ROUTE_ACCESS: Record<string, AccessRule> = {
  // Fleet — fleet.view is held by every tenant role including viewer.
  "/dashboard": { perm: "fleet.view" },
  "/fleet": { perm: "fleet.view" },
  // S2: read-only intelligence, same read grade as the fleet it ranks.
  "/risk": { perm: "fleet.view" },
  // S3: read-only view of what the fleet learned.
  "/learning": { perm: "fleet.view" },
  "/reliability": { perm: "fleet.view" },
  // Auditor and viewer may read the queue; the page gates the approve and
  // deny buttons on action.approve separately.
  // S4: real incidents with their diagnosis.
  "/incidents": { perm: "incident.view" },
  "/approvals": { perm: "incident.view" },
  "/agents": { perm: "fleet.view" },

  // Operations
  "/policies": { perm: "site.manage" },
  // Listing tenants is `tenant.view` held by PLATFORM staff. Entering one
  // is a separate act, gated server-side by tenant_scope plus a
  // support-access grant, so a visible registry is not a reachable tenant.
  "/tenants": { perm: "tenant.view", platform: true },
  "/users": { perm: "user.view" },
  "/licenses": { perm: "license.view" },
  "/support": { perm: "support.view" },
  "/audit": { perm: "audit.view" },
  "/marketplace": { perm: "skill.submit" },

  // Billing
  "/billing": { perm: "billing.view" },
  "/invoices": { perm: "billing.view" },
  "/usage": { perm: "billing.view" },
  "/admin/billing": { platformOnly: true },

  // Administration — every /admin route is require_super_admin server-side.
  "/admin": { platformOnly: true },
  "/admin/features": { platformOnly: true },
  "/admin/releases": { platformOnly: true },
  "/admin/health": { platformOnly: true },
  // D3 fix (P0 2026-08-29): this key was MISSING, and the sidebar's
  // direct-lookup filter treats an absent rule as visible-to-everyone —
  // so platform_support saw a "Support Access" item that 403ed on click
  // (the approver queue is require_super_admin, A14).
  "/admin/support-access": { platformOnly: true },
  "/downloads": { perm: "site.view" },
  // /reports, /settings, /api-keys, /admin/impersonation: retired
  // 2026-08-29 with their phantom pages (final assessment §3).
};

/** Does this user hold a single atomic permission? */
export function can(user: AuthUser | null, perm: string): boolean {
  if (!user) return false;
  return user.permissions.includes(perm);
}

/** May this user reach a route? Unknown routes are allowed: the server is
 *  the enforcement point, and a missing map entry must not silently hide a
 *  page that everyone is entitled to. */
export function canAccess(user: AuthUser | null, rule?: AccessRule): boolean {
  if (!rule) return true;
  if (!user) return false;
  if (rule.platformOnly) {
    return user.is_platform_user && user.role === "platform_super_admin";
  }
  if (rule.platform && !user.is_platform_user) return false;
  return rule.perm ? can(user, rule.perm) : true;
}

/** Strip the tenant-plane prefix, so `/t/acme/audit` is ruled on as
 *  `/audit`. One ROUTE_ACCESS map serves both planes; the tenant id in the
 *  path says WHICH tenant, never WHAT the caller may do there. */
export function stripTenantPrefix(pathname: string): string {
  const m = /^\/t\/[^/]+(\/.*)?$/.exec(pathname);
  if (!m) return pathname;
  return m[1] ?? "/dashboard";
}

/** Is this path inside a tenant context, and which tenant? */
export function tenantFromPath(pathname: string): string | null {
  const m = /^\/t\/([^/]+)(?:\/|$)/.exec(pathname);
  return m ? m[1] : null;
}

/**
 * Where a bare or unmatched path should send this user.
 *
 * Pure so it can be tested: the naive version prefixed whatever it was
 * given, so an unknown path that ALREADY carried a tenant segment became
 * `/t/x/t/x/...` and redirected forever. A path that is already scoped has
 * a good tenant and a bad page, so the answer is that tenant's dashboard —
 * never another prefix.
 */
export function redirectTargetFor(
  user: { is_platform_user: boolean; tenant_id: string } | null,
  pathname: string,
): string | null {
  if (!user) return null;
  // Platform users are never placed in a tenant automatically; they choose
  // one from the registry.
  if (user.is_platform_user || !user.tenant_id) return "/tenants";
  if (tenantFromPath(pathname) !== null) {
    return `/t/${user.tenant_id}/dashboard`;
  }
  const path = pathname === "/" ? "/dashboard" : pathname;
  return `/t/${user.tenant_id}${path}`;
}

/** Rule for a route key, longest-prefix first so /admin/features does not
 *  match /admin. */
export function ruleFor(pathname: string): AccessRule | undefined {
  const path = stripTenantPrefix(pathname);
  const key = Object.keys(ROUTE_ACCESS)
    .sort((a, b) => b.length - a.length)
    .find((k) => path === k || path.startsWith(`${k}/`));
  return key ? ROUTE_ACCESS[key] : undefined;
}
