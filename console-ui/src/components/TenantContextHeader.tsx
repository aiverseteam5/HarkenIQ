import { type CSSProperties, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { getJson } from "../api";
import { useAuth } from "../useAuth";

/**
 * Says which tenant you are inside, and gives you a way out.
 *
 * Replaces the old global "Acting on tenant" selector, which rendered on
 * every page — platform plane included, where it meant nothing — and
 * auto-entered the first tenant alphabetically on login. Context now comes
 * from the URL, so this component reports rather than decides: it cannot
 * put anyone into a tenant, it can only show the one the route names and
 * offer the registry as the way to change it.
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
  borderRadius: "var(--radius-sm, 4px)",
  background: "var(--bg-secondary, #16181d)",
  border: "1px solid var(--border-light, #2a2a2a)",
  fontSize: "0.8125rem",
};

const crumbMuted: CSSProperties = {
  color: "var(--text-muted, #888)",
};

const crumbLink: CSSProperties = {
  ...crumbMuted,
  background: "none",
  border: "none",
  padding: 0,
  font: "inherit",
  cursor: "pointer",
  textDecoration: "underline",
};

const tenantNameStyle: CSSProperties = {
  fontWeight: 600,
  color: "var(--text-primary, #e8e8e8)",
};

const spacerStyle: CSSProperties = { marginLeft: "auto", display: "flex", gap: "0.5rem" };

const actionStyle: CSSProperties = {
  background: "none",
  border: "1px solid var(--border-light, #2a2a2a)",
  borderRadius: "var(--radius-sm, 4px)",
  color: "inherit",
  padding: "0.25rem 0.625rem",
  fontSize: "0.75rem",
  cursor: "pointer",
};

export default function TenantContextHeader() {
  const { tenantId } = useParams<{ tenantId: string }>();
  const { user } = useAuth();
  const navigate = useNavigate();
  const [tenant, setTenant] = useState<TenantOption | null>(null);

  useEffect(() => {
    if (!tenantId || !user) return;
    let cancelled = false;
    void (async () => {
      try {
        // Reads need tenant.view, which every tenant role and
        // platform_support holds. A failure here must not blank the page.
        const res = await getJson<TenantOption>(
          `/api/admin/tenants/${tenantId}`,
        );
        if (!cancelled) setTenant(res);
      } catch {
        if (!cancelled) setTenant(null);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [tenantId, user]);

  if (!tenantId) return null;

  // A tenant user has exactly one tenant and never chose it, so a
  // breadcrumb back to a registry they cannot read would be a dead end.
  const isPlatform = user?.is_platform_user ?? false;
  const label = tenant?.name ?? tenantId;

  return (
    <div style={barStyle}>
      {isPlatform ? (
        <>
          <span style={crumbMuted}>Platform Console</span>
          <span style={crumbMuted}>›</span>
          <button
            type="button"
            style={crumbLink}
            onClick={() => navigate("/tenants")}
          >
            Tenants
          </button>
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
            style={actionStyle}
            onClick={() => navigate("/tenants")}
          >
            Switch tenant
          </button>
          <button
            type="button"
            style={actionStyle}
            onClick={() => navigate("/tenants")}
          >
            Exit to platform
          </button>
        </span>
      )}
    </div>
  );
}
