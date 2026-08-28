import { type CSSProperties, useCallback, useEffect, useState } from "react";
import { useParams } from "react-router-dom";
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

interface SlaSummary {
  avg_time_to_diagnose_min: number;
  avg_time_to_approve_min: number;
  avg_time_to_resolve_min: number;
  tickets_within_sla_pct: number;
  open_tickets: number;
  closed_tickets: number;
}

interface ActionRecord {
  id: string;
  action_type: string;
  device: string;
  site: string;
  result: string;
  executed_at: string;
}

interface DeviceHealthRow {
  device_id: string;
  device_name: string;
  site: string;
  incident_count: number;
  mttr_hours: number;
  recurring_issues: string;
  trend: "improving" | "degrading" | "stable";
}

interface MonthTrend {
  month: string;
  incidents: number;
  resolved: number;
  mttr_hours: number;
}

/* ── Styles ───────────────────────────────────────── */

const metricsRow: CSSProperties = {
  display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(180px, 1fr))",
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

const trendBar: CSSProperties = {
  display: "flex", alignItems: "flex-end", gap: "3px", height: 80,
};

const TREND_VARIANT: Record<string, "success" | "critical" | "neutral"> = {
  improving: "success", degrading: "critical", stable: "neutral",
};

/* ── Helpers ──────────────────────────────────────── */

function formatDate(iso: string | null): string {
  if (!iso) return "--";
  return new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}

function formatMinutes(m: number): string {
  if (m < 60) return `${m.toFixed(0)}m`;
  if (m < 1440) return `${(m / 60).toFixed(1)}h`;
  return `${(m / 1440).toFixed(1)}d`;
}

function exportCSV(actions: ActionRecord[]): void {
  const header = "ID,Action Type,Device,Site,Result,Executed At\n";
  const rows = actions.map(a => `${a.id},${a.action_type},${a.device},${a.site},${a.result},${a.executed_at}`).join("\n");
  const blob = new Blob([header + rows], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `action-history-${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

/* ── Component ────────────────────────────────────── */

export default function ReportingAnalytics() {
  const { toasts, toast, dismiss } = useToast();
  const [loading, setLoading] = useState(true);
  const [sla, setSla] = useState<SlaSummary | null>(null);
  const [actions, setActions] = useState<ActionRecord[]>([]);
  const [devices, setDevices] = useState<DeviceHealthRow[]>([]);
  const [trends, setTrends] = useState<MonthTrend[]>([]);

  const { tenantId = "" } = useParams<{ tenantId: string }>();

  const fetchAll = useCallback(async () => {
    setLoading(true);
    try {
      const [slaRes, actRes, devRes, trendRes] = await Promise.allSettled([
        getJson<SlaSummary>(`/api/tenants/${tenantId}/reports/sla`),
        getJson<{ items: ActionRecord[] }>(`/api/tenants/${tenantId}/reports/actions?page_size=50`),
        getJson<{ items: DeviceHealthRow[] }>(`/api/tenants/${tenantId}/reports/device-health`),
        getJson<{ items: MonthTrend[] }>(`/api/tenants/${tenantId}/reports/trends`),
      ]);
      if (slaRes.status === "fulfilled") setSla(slaRes.value);
      if (actRes.status === "fulfilled") setActions(actRes.value.items);
      if (devRes.status === "fulfilled") setDevices(devRes.value.items);
      if (trendRes.status === "fulfilled") setTrends(trendRes.value.items);
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to load reports", "error");
    } finally {
      setLoading(false);
    }
  }, [tenantId, toast]);

  useEffect(() => { void fetchAll(); }, [fetchAll]);

  const actionColumns: Column<ActionRecord>[] = [
    { key: "action_type", header: "Action" },
    { key: "device", header: "Device" },
    { key: "site", header: "Site" },
    { key: "result", header: "Result", render: (r) => (
      <StatusBadge status={r.result} variant={r.result === "success" ? "success" : r.result === "failed" ? "critical" : "neutral"} size="sm" />
    )},
    { key: "executed_at", header: "Date", render: (r) => formatDate(r.executed_at) },
  ];

  const deviceColumns: Column<DeviceHealthRow>[] = [
    { key: "device_name", header: "Device" },
    { key: "site", header: "Site" },
    { key: "incident_count", header: "Incidents", render: (r) => String(r.incident_count) },
    { key: "mttr_hours", header: "MTTR", render: (r) => `${r.mttr_hours.toFixed(1)}h` },
    { key: "recurring_issues", header: "Recurring Issues" },
    { key: "trend", header: "Trend", render: (r) => <StatusBadge status={r.trend} variant={TREND_VARIANT[r.trend] ?? "neutral"} size="sm" /> },
  ];

  const maxIncidents = Math.max(...trends.map(t => t.incidents), 1);

  if (loading) {
    return (
      <div>
        <PageHeader title="Reporting & Analytics" breadcrumbs={[{ label: "HarkenIQ" }, { label: "Operations" }, { label: "Reports" }]} />
        <div style={{ display: "flex", justifyContent: "center", padding: "4rem" }}><Spinner size="lg" /></div>
      </div>
    );
  }

  return (
    <div>
      <Toast toasts={toasts} onDismiss={dismiss} />
      <PageHeader
        title="Reporting & Analytics"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Operations" }, { label: "Reports" }]}
        actions={[{ label: "Export Actions CSV", onClick: () => exportCSV(actions), variant: "default" as const }]}
      />

      {/* SLA metrics */}
      <div style={sectionHeader}>SLA Tracking</div>
      <div style={metricsRow}>
        <MetricCard title="Avg Time to Diagnose" value={sla ? formatMinutes(sla.avg_time_to_diagnose_min) : "--"} />
        <MetricCard title="Avg Time to Approve" value={sla ? formatMinutes(sla.avg_time_to_approve_min) : "--"} />
        <MetricCard title="Avg Time to Resolve" value={sla ? formatMinutes(sla.avg_time_to_resolve_min) : "--"} />
        <MetricCard title="Within SLA" value={sla ? `${sla.tickets_within_sla_pct}` : "--"} unit="%" trend={sla && sla.tickets_within_sla_pct >= 90 ? "up" : "down"} />
        <MetricCard title="Open Tickets" value={sla?.open_tickets ?? "--"} />
        <MetricCard title="Closed (period)" value={sla?.closed_tickets ?? "--"} />
      </div>

      {/* Month-over-month trends */}
      <div style={sectionHeader}>Month-over-Month Trends</div>
      {trends.length === 0 ? (
        <EmptyState title="No trend data" description="Trends will appear once incidents are tracked over time." icon="&#x2B22;" />
      ) : (
        <div style={cardStyle}>
          <div style={trendBar}>
            {trends.map((t) => (
              <div key={t.month} style={{ flex: 1, display: "flex", flexDirection: "column", alignItems: "center" }}>
                <div
                  style={{
                    width: "100%", maxWidth: 40, borderRadius: "var(--radius-sm) var(--radius-sm) 0 0",
                    height: `${Math.max((t.incidents / maxIncidents) * 100, 8)}%`,
                    background: t.incidents > t.resolved ? "var(--warning)" : "var(--accent)",
                  }}
                  title={`${t.month}: ${t.incidents} incidents, ${t.resolved} resolved, MTTR ${t.mttr_hours.toFixed(1)}h`}
                />
                <div style={{ fontSize: "0.5625rem", color: "var(--text-muted)", marginTop: "0.25rem" }}>{t.month}</div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Action history */}
      <div style={sectionHeader}>Action History</div>
      {actions.length === 0 ? (
        <EmptyState title="No actions recorded" description="Diagnostics, actions, and approvals will appear here." icon="&#x2714;" />
      ) : (
        <DataTable<ActionRecord> columns={actionColumns} data={actions} loading={false} emptyMessage="No actions" striped />
      )}

      {/* Device health */}
      <div style={sectionHeader}>Device Health Reports</div>
      {devices.length === 0 ? (
        <EmptyState title="No device health data" description="Device health reports will appear once incidents are tracked." icon="&#x2318;" />
      ) : (
        <DataTable<DeviceHealthRow> columns={deviceColumns} data={devices} loading={false} emptyMessage="No devices" striped />
      )}
    </div>
  );
}
