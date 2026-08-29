/**
 * Route rules must survive the tenant prefix.
 *
 * ROUTE_ACCESS is keyed on bare paths, but tenant-plane routes now carry
 * `/t/{tenantId}` in front. If ruleFor stopped stripping that, every tenant
 * page would fall through to "no rule" — which `canAccess` treats as
 * allowed, so the guard would silently stop guarding.
 */

import { describe, expect, it } from "vitest";
import {
  canAccess,
  redirectTargetFor,
  ruleFor,
  stripTenantPrefix,
  tenantFromPath,
} from "./permissions";
import type { AuthUser } from "./useAuth";

const viewer: AuthUser = {
  email: "v@t.example",
  name: "V",
  role: "viewer",
  tenant_id: "t1",
  permissions: ["fleet.view", "incident.view"],
  is_platform_user: false,
};

// The owner fixture carries tenant.view DELIBERATELY — the server's real
// tenant_owner set includes it, and the review found the /tenants rule
// leaked the registry page to exactly this role. Omitting it here would
// sidestep the question the rule must answer (testing-pass finding).
const owner: AuthUser = {
  ...viewer,
  role: "tenant_owner",
  permissions: [
    "fleet.view", "incident.view", "audit.view", "user.manage", "tenant.view",
  ],
};

describe("stripTenantPrefix", () => {
  it("removes the tenant segment", () => {
    expect(stripTenantPrefix("/t/acme/audit")).toBe("/audit");
    expect(stripTenantPrefix("/t/acme/invoices/inv1")).toBe("/invoices/inv1");
  });

  it("maps a bare tenant root to the dashboard", () => {
    expect(stripTenantPrefix("/t/acme")).toBe("/dashboard");
  });

  it("leaves platform-plane paths alone", () => {
    expect(stripTenantPrefix("/admin/features")).toBe("/admin/features");
    expect(stripTenantPrefix("/tenants")).toBe("/tenants");
  });
});

describe("tenantFromPath", () => {
  it("identifies the tenant plane", () => {
    expect(tenantFromPath("/t/acme/audit")).toBe("acme");
    expect(tenantFromPath("/t/acme")).toBe("acme");
  });

  it("returns null on the platform plane", () => {
    expect(tenantFromPath("/tenants")).toBeNull();
    expect(tenantFromPath("/admin")).toBeNull();
  });
});

describe("ruleFor under a tenant prefix", () => {
  it("still finds the rule, so the guard keeps guarding", () => {
    expect(ruleFor("/t/acme/audit")).toEqual(ruleFor("/audit"));
    expect(ruleFor("/t/acme/downloads")).toEqual(ruleFor("/downloads"));
  });

  it("refuses a viewer the pages they do not hold", () => {
    // /api-keys retired 2026-08-29 (P0): keys authenticated nothing.
    expect(canAccess(viewer, ruleFor("/t/acme/audit"))).toBe(false);
    expect(canAccess(viewer, ruleFor("/t/acme/downloads"))).toBe(false);
    expect(canAccess(viewer, ruleFor("/t/acme/billing"))).toBe(false);
  });

  it("D3 (P0 2026-08-29): support-access rule exists and is super-admin only", () => {
    const rule = ruleFor("/admin/support-access");
    expect(rule).toEqual({ platformOnly: true });
    const support = {
      email: "s@x", name: "s", role: "platform_support",
      tenant_id: "", permissions: ["tenant.view", "support.view"],
      is_platform_user: true,
    };
    expect(canAccess(support, rule)).toBe(false);
  });

  it("still admits a viewer to the fleet pages they do hold", () => {
    expect(canAccess(viewer, ruleFor("/t/acme/dashboard"))).toBe(true);
    expect(canAccess(viewer, ruleFor("/t/acme/fleet"))).toBe(true);
  });

  it("admits an owner to audit and api keys", () => {
    expect(canAccess(owner, ruleFor("/t/acme/audit"))).toBe(true);
    expect(canAccess(owner, ruleFor("/t/acme/api-keys"))).toBe(true);
  });
});

describe("redirectTargetFor", () => {
  const platform = { is_platform_user: true, tenant_id: "" };
  const tenant = { is_platform_user: false, tenant_id: "t1" };

  it("sends a tenant user's bare path into their own tenant", () => {
    expect(redirectTargetFor(tenant, "/audit")).toBe("/t/t1/audit");
    expect(redirectTargetFor(tenant, "/")).toBe("/t/t1/dashboard");
  });

  it("never re-prefixes an already-scoped path", () => {
    // The naive version produced /t/t1/t/acme/bogus, which fails to match
    // again and redirects forever.
    const target = redirectTargetFor(tenant, "/t/acme/bogus");
    expect(target).toBe("/t/t1/dashboard");
    expect(target).not.toContain("/t/t1/t/");
  });

  it("terminates: the target of a redirect is never itself redirected", () => {
    // Feed each result back in. A fixed point must be reached, or the
    // browser would loop.
    let path = "/t/acme/bogus";
    for (let i = 0; i < 5; i += 1) {
      const next = redirectTargetFor(tenant, path);
      if (next === path) break;
      path = next!;
    }
    expect(path).toBe("/t/t1/dashboard");
  });

  it("never places a platform user in a tenant automatically", () => {
    expect(redirectTargetFor(platform, "/audit")).toBe("/tenants");
    expect(redirectTargetFor(platform, "/t/acme/bogus")).toBe("/tenants");
  });

  it("returns null with no user, so the caller can wait for auth", () => {
    expect(redirectTargetFor(null, "/audit")).toBeNull();
  });
});

describe("the platform plane", () => {
  it("lets PLATFORM tenant.view holders see the registry", () => {
    const support: AuthUser = {
      ...viewer,
      role: "platform_support",
      is_platform_user: true,
      permissions: ["tenant.view", "support.view", "audit.view"],
    };
    expect(canAccess(support, ruleFor("/tenants"))).toBe(true);
  });

  it("keeps a tenant OWNER out of the registry despite tenant.view", () => {
    // tenant.view is shared vocabulary; holding it must not open the
    // vendor registry to a customer role [review CRITICAL].
    expect(canAccess(owner, ruleFor("/tenants"))).toBe(false);
  });

  it("keeps a tenant viewer out of the registry", () => {
    expect(canAccess(viewer, ruleFor("/tenants"))).toBe(false);
  });

  it("keeps non-super-admins out of platform admin pages", () => {
    const support: AuthUser = {
      ...viewer,
      role: "platform_support",
      is_platform_user: true,
      permissions: ["tenant.view"],
    };
    expect(canAccess(support, ruleFor("/admin/features"))).toBe(false);
  });
});
