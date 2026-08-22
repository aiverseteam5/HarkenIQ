import { type CSSProperties, useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import PageHeader from "../components/PageHeader";
import MetricCard from "../components/MetricCard";
import DataTable, { type Column } from "../components/DataTable";
import StatusBadge from "../components/StatusBadge";
import EmptyState from "../components/EmptyState";
import Spinner from "../components/Spinner";
import Toast from "../components/Toast";
import { useToast } from "../components/useToast";
import { getJson } from "../api";

/* ── Types ────────────────────────────────────────── */

interface DashboardOverview {
  active_tenants: number;
  total_nodes: number;
  total_revenue_cents: number;
  open_tickets: number;
}

interface TenantHealth {
  id: string;
  name: string;
  slug: string;
  status: string;
  delinquency_status: string;
  plan: string;
  node_commit: number;
  open_tickets: number;
  created_at: string;
}

interface RecentEvent {
  id: string;
  ts: string;
  action: string;
  actor_email: string;
  subject_type: string;
  tenant_id: string | null;
}

interface SystemHealth {
  services: Record<string, { status: string; version?: string }>;
}

/* ── Styles ───────────────────────────────────────── */

const metricsRow: CSSProperties = {
  display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))",
  gap: "1rem", marginBottom: "1.5rem",
};

const sectionHeader: CSSProperties = {
  fontSize: "0.9375rem", fontWeight: 600, color: "var(--text-primary)",
  marginBottom: "0.75rem", marginTop: "1.5rem",
};

const cardStyle: CSSProperties = {
  background: "var(--bg-card)", borderRadius: "var(--radius-md)",
  border: "1px solid var(--border-light)", padding: "1.25rem", marginBottom: "1.5rem",
};

const healthGrid: CSSProperties = {
  display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(180px, 1fr))",
  gap: "0.75rem", marginBottom: "1.5rem",
};

const healthCard: CSSProperties = {
  ...cardStyle, textAlign: "center", padding: "1rem", marginBottom: 0,
};

const timelineItem: CSSProperties = {
  display: "flex", gap: "0.75rem", padding: "0.5rem 0",
  borderBottom: "1px solid var(--border-light)", fontSize: "0.8125rem",
};

const quickActions: CSSProperties = {
  display: "flex", gap: "0.5rem", flexWrap: "wrap", marginBottom: "1.5rem",
};

const DELINQUENCY_VARIANT: Record<string, "success" | "warning" | "critical" | "neutral"> = {
  current: "success", overdue: "warning", restricted: "critical", suspended: "critical",
};

/* ── Helpers ──────────────────────────────────────── */

function formatDate(iso: string | null): string {
  if (!iso) return "--";
  return new Date(iso).toLocaleString("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function formatCompact(cents: number): string {
  const val = cents / 100;
  if (val >= 1_000_000) return `$${(val / 1_000_000).toFixed(1)}M`;
  if (val >= 1_000) return `$${(val / 1_000).toFixed(1)}K`;
  return `$${val.toFixed(0)}`;
}

/* ── Component ────────────────────────────────────── */

export default function AdminDashboard() {
  const navigate = useNavigate();
  const { toasts, toast, dismiss } = useToast();
  const [loading, setLoading] = useState(true);
  const [overview, setOverview] = useState<DashboardOverview | null>(null);
  const [tenants, setTenants] = useState<TenantHealth[]>([]);
  const [events, setEvents] = useState<RecentEvent[]>([]);
  const [system, setSystem] = useState<SystemHealth | null>(null);

  const fetchAll = useCallback(async () => {
    setLoading(true);
    try {
      const [ovRes, thRes, evRes, sysRes] = await Promise.allSettled([
        getJson<DashboardOverview>("/api/admin/dashboard"),
        getJson<{ items: TenantHealth[] }>("/api/admin/tenants/health?page_size=20"),
        getJson<{ items: RecentEvent[] }>("/api/admin/events/recent?limit=10"),
        getJson<SystemHealth>("/api/admin/system"),
      ]);
      if (ovRes.status === "fulfilled") setOverview(ovRes.value);
      if (thRes.status === "fulfilled") setTenants(thRes.value.items);
      if (evRes.status === "fulfilled") setEvents(evRes.value.items);
      if (sysRes.status === "fulfilled") setSystem(sysRes.value);
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to load dashboard", "error");
    } finally {
      setLoading(false);
    }
  }, [toast]);

  useEffect(() => { void fetchAll(); }, [fetchAll]);

  const tenantColumns: Column<TenantHealth>[] = [
    { key: "name", header: "Tenant" },
    { key: "slug", header: "Slug", render: (r) => <code>{r.slug}</code> },
    { key: "status", header: "Status", render: (r) => <StatusBadge status={r.status} variant={r.status === "active" ? "success" : "critical"} size="sm" /> },
    { key: "plan", header: "Plan" },
    { key: "node_commit", header: "Nodes", render: (r) => String(r.node_commit) },
    { key: "delinquency_status", header: "Billing", render: (r) => <StatusBadge status={r.delinquency_status} variant={DELINQUENCY_VARIANT[r.delinquency_status] ?? "neutral"} size="sm" /> },
    { key: "open_tickets", header: "Tickets", render: (r) => String(r.open_tickets) },
  ];

  if (loading) {
    return (
      <div>
        <PageHeader title="Admin Dashboard" breadcrumbs={[{ label: "HarkenIQ" }, { label: "Admin" }, { label: "Dashboard" }]} />
        <div style={{ display: "flex", justifyContent: "center", padding: "4rem" }}><Spinner size="lg" /></div>
      </div>
    );
  }

  return (
    <div>
      <Toast toasts={toasts} onDismiss={dismiss} />
      <PageHeader title="Admin Dashboard" breadcrumbs={[{ label: "HarkenIQ" }, { label: "Admin" }, { label: "Dashboard" }]} />

      {/* KPI cards */}
      <div style={metricsRow}>
        <MetricCard title="Active Tenants" value={overview?.active_tenants ?? "--"} />
        <MetricCard title="Total Nodes" value={overview?.total_nodes ?? "--"} />
        <MetricCard title="Revenue" value={overview ? formatCompact(overview.total_revenue_cents) : "--"} />
        <MetricCard title="Open Tickets" value={overview?.open_tickets ?? "--"} trend={overview && overview.open_tickets > 0 ? "down" : "flat"} />
      </div>

      {/* Quick actions */}
      <div style={quickActions}>
        <button className="btn btn-sm btn-primary" onClick={() => navigate("/tenants")}>Create Tenant</button>
        <button className="btn btn-sm" onClick={() => navigate("/admin/billing")}>Billing Stats</button>
        <button className="btn btn-sm" onClick={() => navigate("/admin/features")}>Feature Toggles</button>
        <button className="btn btn-sm" onClick={() => navigate("/admin/health")}>Platform Health</button>
      </div>

      {/* System health */}
      <div style={sectionHeader}>System Health</div>
      <div style={healthGrid}>
        {system && Object.entries(system.services).map(([name, info]) => (
          <div key={name} style={healthCard}>
            <StatusBadge status={info.status} variant={info.status === "healthy" ? "success" : info.status === "unhealthy" ? "critical" : "neutral"} />
            <div style={{ fontSize: "0.75rem", fontWeight: 600, marginTop: "0.375rem", textTransform: "capitalize" }}>{name}</div>
            {info.version && <div style={{ fontSize: "0.625rem", color: "var(--text-muted)" }}>v{info.version}</div>}
          </div>
        ))}
      </div>

      {/* Tenant health */}
      <div style={sectionHeader}>Tenant Health</div>
      {tenants.length === 0 ? (
        <EmptyState title="No tenants" description="Tenants will appear here once created." icon="&#x2302;" />
      ) : (
        <DataTable<TenantHealth> columns={tenantColumns} data={tenants} loading={false} emptyMessage="No tenants" striped />
      )}

      {/* Recent events */}
      <div style={sectionHeader}>Recent Events</div>
      {events.length === 0 ? (
        <div style={{ ...cardStyle, textAlign: "center", color: "var(--text-muted)", fontSize: "0.875rem" }}>No recent events.</div>
      ) : (
        <div style={cardStyle}>
          {events.map((e) => (
            <div key={e.id} style={timelineItem}>
              <span style={{ color: "var(--text-muted)", minWidth: 130 }}>{formatDate(e.ts)}</span>
              <code style={{ fontSize: "0.75rem" }}>{e.action}</code>
              <span style={{ color: "var(--text-secondary)", marginLeft: "auto" }}>{e.actor_email}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
