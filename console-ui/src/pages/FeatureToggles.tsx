import { type CSSProperties, useCallback, useEffect, useState } from "react";
import PageHeader from "../components/PageHeader";
import DataTable, { type Column } from "../components/DataTable";
import StatusBadge from "../components/StatusBadge";
import EmptyState from "../components/EmptyState";
import Spinner from "../components/Spinner";
import Toast from "../components/Toast";
import { useToast } from "../components/useToast";
import { getJson, postJson } from "../api";

/* ── Types ────────────────────────────────────────── */

interface FeatureFlag {
  id: string;
  feature_name: string;
  enabled: boolean;
  tenant_id: string | null;
  updated_by: string | null;
  updated_at: string | null;
}

interface TenantSummary { id: string; name: string; slug: string; }

/* ── Styles ───────────────────────────────────────── */

const sectionHeader: CSSProperties = {
  fontSize: "0.9375rem", fontWeight: 600, color: "var(--text-primary)",
  marginBottom: "0.75rem", marginTop: "1.5rem",
};

const cardStyle: CSSProperties = {
  background: "var(--bg-card)", borderRadius: "var(--radius-md)",
  border: "1px solid var(--border-light)", padding: "1.25rem", marginBottom: "1.5rem",
};

const matrixRow: CSSProperties = {
  display: "flex", alignItems: "center", padding: "0.375rem 0",
  borderBottom: "1px solid var(--border-light)", fontSize: "0.8125rem",
};

const toggleStyle: CSSProperties = {
  width: 36, height: 20, borderRadius: 10, cursor: "pointer",
  border: "none", position: "relative", transition: "background 0.2s",
};

const FEATURES = [
  { name: "autonomy_mode", description: "Enable autonomy tier for agents", category: "Core" },
  { name: "credential_rotation", description: "Automatic credential rotation", category: "Security" },
  { name: "peer_diagnostics", description: "Cross-device peer diagnostic checks", category: "Core" },
  { name: "premium_reporting", description: "Advanced analytics and reporting", category: "Premium" },
  { name: "air_gapped_billing", description: "Air-gapped usage upload for billing", category: "Billing" },
  { name: "advanced_correlation", description: "Multi-site incident correlation", category: "Premium" },
];

/* ── Helpers ──────────────────────────────────────── */

async function putJson<T>(path: string, body: unknown): Promise<T> {
  const resp = await fetch(path, { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
  if (!resp.ok) throw new Error((await resp.json().catch(() => ({}))).detail ?? resp.statusText);
  return resp.json();
}

/* ── Component ────────────────────────────────────── */

export default function FeatureToggles() {
  const { toasts, toast, dismiss } = useToast();
  const [loading, setLoading] = useState(true);
  const [globalFlags, setGlobalFlags] = useState<FeatureFlag[]>([]);
  const [tenants, setTenants] = useState<TenantSummary[]>([]);
  const [tenantFlags, setTenantFlags] = useState<Record<string, FeatureFlag[]>>({});
  const [selectedTenant, setSelectedTenant] = useState("");
  const [toggling, setToggling] = useState<string | null>(null);

  const fetchGlobals = useCallback(async () => {
    setLoading(true);
    try {
      const [fRes, tRes] = await Promise.allSettled([
        getJson<{ items: FeatureFlag[] }>("/api/admin/features"),
        getJson<{ items: TenantSummary[] }>("/api/admin/tenants/health?page_size=100"),
      ]);
      if (fRes.status === "fulfilled") setGlobalFlags(fRes.value.items);
      if (tRes.status === "fulfilled") {
        const t = tRes.value.items.map((i: any) => ({ id: i.id, name: i.name, slug: i.slug ?? i.name }));
        setTenants(t);
        if (t.length > 0 && !selectedTenant) setSelectedTenant(t[0].id);
      }
    } catch (err) { toast(err instanceof Error ? err.message : "Failed", "error"); }
    finally { setLoading(false); }
  }, [toast, selectedTenant]);

  const fetchTenantFlags = useCallback(async (tid: string) => {
    if (!tid) return;
    try {
      const res = await getJson<{ items: FeatureFlag[] }>(`/api/admin/features/tenant/${tid}`);
      setTenantFlags(prev => ({ ...prev, [tid]: res.items }));
    } catch { /* non-fatal */ }
  }, []);

  useEffect(() => { void fetchGlobals(); }, [fetchGlobals]);
  useEffect(() => { if (selectedTenant) void fetchTenantFlags(selectedTenant); }, [selectedTenant, fetchTenantFlags]);

  const handleToggleGlobal = useCallback(async (name: string, enabled: boolean) => {
    setToggling(name);
    try {
      await putJson(`/api/admin/features/global/${name}`, { enabled });
      toast(`${name} ${enabled ? "enabled" : "disabled"} globally`, "success");
      void fetchGlobals();
    } catch (err) { toast(err instanceof Error ? err.message : "Toggle failed", "error"); }
    finally { setToggling(null); }
  }, [toast, fetchGlobals]);

  const handleToggleTenant = useCallback(async (tid: string, name: string, enabled: boolean) => {
    setToggling(`${tid}-${name}`);
    try {
      await putJson(`/api/admin/features/tenant/${tid}/${name}`, { enabled });
      toast(`${name} ${enabled ? "enabled" : "disabled"}`, "success");
      void fetchTenantFlags(tid);
    } catch (err) { toast(err instanceof Error ? err.message : "Toggle failed", "error"); }
    finally { setToggling(null); }
  }, [toast, fetchTenantFlags]);

  const globalMap = new Map(globalFlags.map(f => [f.feature_name, f.enabled]));
  const tenantFlagMap = new Map((tenantFlags[selectedTenant] ?? []).map(f => [f.feature_name, f.enabled]));

  const Toggle = ({ on, onClick, disabled }: { on: boolean; onClick: () => void; disabled: boolean }) => (
    <button style={{ ...toggleStyle, background: on ? "var(--accent)" : "var(--border-light)" }} onClick={onClick} disabled={disabled}>
      <span style={{ position: "absolute", top: 2, left: on ? 18 : 2, width: 16, height: 16, borderRadius: "50%", background: "#fff", transition: "left 0.2s" }} />
    </button>
  );

  const globalColumns: Column<typeof FEATURES[0]>[] = [
    { key: "name", header: "Feature", render: (r) => <code>{r.name}</code> },
    { key: "description", header: "Description" },
    { key: "category", header: "Category", render: (r) => <StatusBadge status={r.category} variant="neutral" size="sm" /> },
    { key: "enabled", header: "Default", render: (r) => {
      const on = globalMap.get(r.name) ?? false;
      return <Toggle on={on} onClick={() => void handleToggleGlobal(r.name, !on)} disabled={toggling === r.name} />;
    }},
  ];

  if (loading) return (
    <div>
      <PageHeader title="Feature Toggles" breadcrumbs={[{ label: "HarkenIQ" }, { label: "Admin" }, { label: "Features" }]} />
      <div style={{ display: "flex", justifyContent: "center", padding: "4rem" }}><Spinner size="lg" /></div>
    </div>
  );

  return (
    <div>
      <Toast toasts={toasts} onDismiss={dismiss} />
      <PageHeader title="Feature Toggles" breadcrumbs={[{ label: "HarkenIQ" }, { label: "Admin" }, { label: "Features" }]} />

      <div style={sectionHeader}>Global Defaults</div>
      <DataTable columns={globalColumns} data={FEATURES} loading={false} emptyMessage="No features" striped />

      <div style={sectionHeader}>Per-Tenant Overrides</div>
      {tenants.length === 0 ? (
        <EmptyState title="No tenants" description="Create tenants to manage per-tenant flags." icon="&#x2692;" />
      ) : (
        <>
          <select value={selectedTenant} onChange={(e) => setSelectedTenant(e.target.value)} style={{ padding: "0.5rem", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-light)", fontSize: "0.875rem", marginBottom: "0.75rem" }}>
            {tenants.map(t => <option key={t.id} value={t.id}>{t.name} ({t.slug})</option>)}
          </select>
          <div style={cardStyle}>
            {FEATURES.map(f => {
              const tVal = tenantFlagMap.get(f.name);
              const gVal = globalMap.get(f.name) ?? false;
              const effective = tVal ?? gVal;
              return (
                <div key={f.name} style={matrixRow}>
                  <code style={{ flex: 1, fontSize: "0.8125rem" }}>{f.name}</code>
                  <span style={{ flex: 1, fontSize: "0.75rem", color: "var(--text-muted)" }}>{tVal !== undefined ? "overridden" : "inherited"}</span>
                  <Toggle on={effective} onClick={() => void handleToggleTenant(selectedTenant, f.name, !effective)} disabled={toggling === `${selectedTenant}-${f.name}`} />
                </div>
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}
