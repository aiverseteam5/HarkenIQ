import { type CSSProperties, useEffect, useState } from "react";
import { getActiveTenant, getJson, setActiveTenant } from "../api";
import { useAuth } from "../useAuth";

/**
 * QA-046 remainder. The "current" tenant alias resolves the caller's own
 * tenant, or the sole tenant when exactly one exists — which covers every
 * tenant user and the single-tenant / sovereign shape. A platform admin on
 * a multi-tenant install had no way to say which tenant they meant, so
 * those pages returned an honest but unhelpful 404.
 *
 * Renders only for callers the server says may choose.
 */

interface TenantOption {
  id: string;
  name: string;
  slug: string;
}

const wrapStyle: CSSProperties = {
  padding: "0.5rem 0.75rem",
  borderBottom: "1px solid var(--color-border, #2a2a2a)",
};

const labelStyle: CSSProperties = {
  fontSize: "0.6875rem",
  textTransform: "uppercase",
  letterSpacing: "0.06em",
  color: "var(--color-text-muted, #888)",
  marginBottom: "0.25rem",
};

const selectStyle: CSSProperties = {
  width: "100%",
  padding: "0.375rem 0.5rem",
  fontSize: "0.8125rem",
  background: "var(--color-bg-input, #1a1a1a)",
  color: "inherit",
  border: "1px solid var(--color-border, #2a2a2a)",
  borderRadius: "4px",
};

export default function TenantSelector() {
  const { user } = useAuth();
  const [tenants, setTenants] = useState<TenantOption[]>([]);
  const [selectable, setSelectable] = useState(false);
  const [active, setActive] = useState(getActiveTenant());

  useEffect(() => {
    if (!user) return;
    let cancelled = false;
    void (async () => {
      try {
        const res = await getJson<{
          tenants: TenantOption[];
          selectable: boolean;
        }>("/api/me/tenants");
        if (cancelled) return;
        setTenants(res.tenants ?? []);
        setSelectable(res.selectable ?? false);
        // Deliberately no default. Auto-selecting the first tenant put a
        // platform admin inside a customer's tenant on login, with no
        // action and no intent — the opposite of explicit elevation. An
        // unresolved "current" is answered by a clean 400 from the
        // middleware, so the cost of choosing is one click, not a crash.
      } catch {
        /* selector is a convenience; never block the shell on it */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [user]);

  if (!selectable || tenants.length === 0) return null;

  return (
    <div style={wrapStyle}>
      <div style={labelStyle}>Acting on tenant</div>
      <select
        style={selectStyle}
        value={active}
        aria-label="Active tenant"
        onChange={(e) => {
          setActiveTenant(e.target.value);
          setActive(e.target.value);
          // Simplest correct refresh: every page caches tenant-scoped data
          // in its own state, so reload rather than hunt for stale views.
          window.location.reload();
        }}
      >
        <option value="">Select a tenant…</option>
        {tenants.map((t) => (
          <option key={t.id} value={t.id}>
            {t.name}
          </option>
        ))}
      </select>
    </div>
  );
}
