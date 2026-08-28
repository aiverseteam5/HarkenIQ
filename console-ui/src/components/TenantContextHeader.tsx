import { type CSSProperties, useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { getJson } from "../api";
import { tenantFromPath } from "../permissions";
import { useAuth } from "../useAuth";

/**
 * Says which tenant you are inside, and gives you a way out.
 *
 * Review fixes (2026-08-28):
 * - useParams returned {} here — this component renders in the pathless
 *   layout route ABOVE the Outlet, and react-router only exposes params
 *   matched at or above the component. The bar therefore never rendered
 *   at all. tenantFromPath(pathname) is the same derivation the layout
 *   itself uses.
 * - The tenant NAME now comes from /api/me/tenants for tenant users
 *   (their own tenant; every role can read it) and from the platform
 *   registry only for platform staff — most tenant roles do not hold
 *   tenant.view and were shown a raw 32-hex id.
 * - Palette: the old styles referenced --bg-secondary, which does not
 *   exist, so the dark fallbacks fired and painted a near-black bar on a
 *   light console. Tokens below are the real ones from variables.css.
 * - One switch control instead of three redundant ones.
 */

interface TenantOption {
  id: string;
  name: string;
  slug: string;
}

const barStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: "0.5rem",
  padding: "0.5rem 0.75rem",
  marginBottom: "1rem",
  borderRadius: "var(--radius-sm)",
  background: "var(--bg-card)",
  border: "1px solid var(--border-light)",
  fontSize: "0.8125rem",
};

const crumbMuted: CSSProperties = {
  color: "var(--text-secondary)",
};

const tenantNameStyle: CSSProperties = {
  fontWeight: 600,
  color: "var(--text-primary)",
};

const spacerStyle: CSSProperties = { marginLeft: "auto" };

export default function TenantContextHeader() {
  const location = useLocation();
  const tenantId = tenantFromPath(location.pathname);
  const { user } = useAuth();
  const navigate = useNavigate();
  const [tenant, setTenant] = useState<TenantOption | null>(null);

  const isPlatform = user?.is_platform_user ?? false;

  useEffect(() => {
    if (!tenantId || !user) return;
    let cancelled = false;
    void (async () => {
      try {
        if (user.is_platform_user) {
          const res = await getJson<TenantOption>(
            `/api/admin/tenants/${tenantId}`,
          );
          if (!cancelled) setTenant(res);
        } else {
          const res = await getJson<{ tenants: TenantOption[] }>(
            "/api/me/tenants",
          );
          const own = res.tenants?.find((t) => t.id === tenantId) ?? null;
          if (!cancelled) setTenant(own);
        }
      } catch {
        if (!cancelled) setTenant(null);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [tenantId, user]);

  if (!tenantId) return null;

  // Never show a raw hex id as a "name": fall back to a neutral label.
  const label = tenant?.name ?? (isPlatform ? tenantId : "Your tenant");

  return (
    <div style={barStyle}>
      {isPlatform ? (
        <>
          <span style={crumbMuted}>Platform Console</span>
          <span style={crumbMuted}>›</span>
          <span style={crumbMuted}>Tenants</span>
          <span style={crumbMuted}>›</span>
        </>
      ) : (
        <span style={crumbMuted}>Tenant</span>
      )}
      <span style={tenantNameStyle}>{label}</span>

      {isPlatform && (
        <span style={spacerStyle}>
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => navigate("/tenants")}
          >
            Switch tenant
          </button>
        </span>
      )}
    </div>
  );
}
