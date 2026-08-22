import { type CSSProperties, useCallback, useEffect, useMemo, useState } from "react";
import PageHeader from "../components/PageHeader";
import MetricCard from "../components/MetricCard";
import DataTable, { type Column } from "../components/DataTable";
import FilterBar, { type FilterDef } from "../components/FilterBar";
import EmptyState from "../components/EmptyState";
import Spinner from "../components/Spinner";
import Toast from "../components/Toast";
import { useToast } from "../components/useToast";
import { getJson } from "../api";
import type { Subscription, TrueUpEstimate } from "../types";

/* ── Types ────────────────────────────────────────── */

interface SiteUsage {
  site_name: string;
  avg_nodes: number;
  peak_nodes: number;
  days: number;
}

interface UsageSummaryResponse {
  high_water: number;
  daily_counts: { date: string; node_count: number }[];
  per_site: SiteUsage[];
}

/* ── Styles ───────────────────────────────────────── */

const metricsRow: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))",
  gap: "1rem",
  marginBottom: "1.5rem",
};

const cardStyle: CSSProperties = {
  background: "var(--bg-card)",
  borderRadius: "var(--radius-md)",
  border: "1px solid var(--border-light)",
  padding: "1.25rem",
  marginBottom: "1.5rem",
};

const sectionHeader: CSSProperties = {
  fontSize: "0.9375rem",
  fontWeight: 600,
  color: "var(--text-primary)",
  marginBottom: "0.75rem",
  marginTop: "1.5rem",
};

const chartContainer: CSSProperties = {
  ...cardStyle,
  overflowX: "auto",
};

const trendRow: CSSProperties = {
  display: "flex",
  alignItems: "flex-end",
  gap: "2px",
  height: 100,
  padding: "0.5rem 0",
};

const exportButton: CSSProperties = {
  background: "var(--bg-card)",
  border: "1px solid var(--border-light)",
  borderRadius: "var(--radius-sm)",
  padding: "0.375rem 0.75rem",
  fontSize: "0.8125rem",
  fontWeight: 500,
  cursor: "pointer",
  color: "var(--text-primary)",
};

/* ── Helpers ──────────────────────────────────────── */

function formatCents(cents: number, currency: string): string {
  const symbols: Record<string, string> = { USD: "$", INR: "\u20B9", EUR: "\u20AC" };
  const sym = symbols[currency] ?? currency + " ";
  return `${sym}${(cents / 100).toLocaleString("en-US", { minimumFractionDigits: 2 })}`;
}

function todayStr(): string {
  return new Date().toISOString().slice(0, 10);
}

function monthStartStr(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-01`;
}

function exportCSV(sites: SiteUsage[], sub: Subscription | null, currency: string): void {
  const header = "Site,Avg Nodes,Peak Nodes,Days Reporting\n";
  const rows = sites.map((s) => `${s.site_name},${s.avg_nodes.toFixed(1)},${s.peak_nodes},${s.days}`).join("\n");
  const summary = `\n\nPlan,${sub?.plan ?? ""},Committed Nodes,${sub?.node_commit ?? ""},Currency,${currency}`;
  const blob = new Blob([header + rows + summary], { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `usage-chargeback-${todayStr()}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}

/* ── Component ────────────────────────────────────── */

export default function UsageChargeback() {
  const { toasts, toast, dismiss } = useToast();
  const [loading, setLoading] = useState(true);
  const [sub, setSub] = useState<Subscription | null>(null);
  const [usage, setUsage] = useState<UsageSummaryResponse | null>(null);
  const [estimate, setEstimate] = useState<TrueUpEstimate | null>(null);
  const [periodStart, setPeriodStart] = useState(monthStartStr());
  const [periodEnd, setPeriodEnd] = useState(todayStr());

  const tenantId = "current";
  const currency = sub?.currency ?? estimate?.currency ?? "USD";

  const filterDefs = useMemo<FilterDef[]>(
    () => [
      { key: "period_start", label: "From", type: "date" as const },
      { key: "period_end", label: "To", type: "date" as const },
    ],
    [],
  );

  const fetchAll = useCallback(async () => {
    setLoading(true);
    try {
      const [subRes, usageRes, estRes] = await Promise.allSettled([
        getJson<{ subscription: Subscription }>(`/api/tenants/${tenantId}/subscription`),
        getJson<UsageSummaryResponse>(
          `/api/tenants/${tenantId}/usage?period_start=${periodStart}&period_end=${periodEnd}`,
        ),
        getJson<TrueUpEstimate>(`/api/tenants/${tenantId}/usage/estimate`),
      ]);
      if (subRes.status === "fulfilled") setSub(subRes.value.subscription);
      if (usageRes.status === "fulfilled") setUsage(usageRes.value);
      if (estRes.status === "fulfilled") setEstimate(estRes.value);
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to load usage data", "error");
    } finally {
      setLoading(false);
    }
  }, [tenantId, periodStart, periodEnd, toast]);

  useEffect(() => {
    void fetchAll();
  }, [fetchAll]);

  const handleFilterChange = useCallback((key: string, value: string) => {
    if (key === "period_start") setPeriodStart(value);
    if (key === "period_end") setPeriodEnd(value);
  }, []);

  const handleFilterClear = useCallback(() => {
    setPeriodStart(monthStartStr());
    setPeriodEnd(todayStr());
  }, []);

  // Daily usage trend bar chart
  const dailyCounts = usage?.daily_counts ?? [];
  const maxDaily = Math.max(...dailyCounts.map((d) => d.node_count), 1);
  const committed = sub?.node_commit ?? 0;

  const siteColumns: Column<SiteUsage>[] = [
    { key: "site_name", header: "Site" },
    { key: "avg_nodes", header: "Avg Nodes", render: (r) => r.avg_nodes.toFixed(1) },
    { key: "peak_nodes", header: "Peak Nodes", render: (r) => String(r.peak_nodes) },
    { key: "days", header: "Days Reporting", render: (r) => String(r.days) },
  ];

  // Billing breakdown
  const nodePrice = estimate ? (committed > 0 && estimate.estimated_amount_cents > 0
    ? Math.round(estimate.estimated_amount_cents / Math.max(estimate.estimated_overage, 1))
    : 0) : 0;
  const baseCost = committed * nodePrice;
  const overageCost = estimate?.estimated_amount_cents ?? 0;

  if (loading) {
    return (
      <div>
        <PageHeader
          title="Usage & Chargeback"
          breadcrumbs={[{ label: "HarkenIQ" }, { label: "Billing" }, { label: "Usage" }]}
        />
        <div style={{ display: "flex", justifyContent: "center", padding: "4rem" }}>
          <Spinner size="lg" />
        </div>
      </div>
    );
  }

  return (
    <div>
      <Toast toasts={toasts} onDismiss={dismiss} />
      <PageHeader
        title="Usage & Chargeback"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Billing" }, { label: "Usage" }]}
        actions={[
          {
            label: "Export CSV",
            onClick: () => exportCSV(usage?.per_site ?? [], sub, currency),
            variant: "default" as const,
          },
        ]}
      />

      {/* Period filter */}
      <FilterBar
        filters={filterDefs}
        values={{ period_start: periodStart, period_end: periodEnd }}
        onChange={handleFilterChange}
        onClear={handleFilterClear}
      />

      {/* KPI cards */}
      <div style={metricsRow}>
        <MetricCard title="High-Water Mark" value={usage?.high_water ?? "--"} unit=" nodes" />
        <MetricCard title="Committed" value={committed || "--"} unit=" nodes" />
        <MetricCard
          title="Overage"
          value={estimate ? Math.max(0, estimate.estimated_overage) : "--"}
          unit=" nodes"
          trend={estimate && estimate.estimated_overage > 0 ? "up" : "flat"}
        />
        <MetricCard
          title="Est. Overage Cost"
          value={estimate ? formatCents(estimate.estimated_amount_cents, currency) : "--"}
        />
      </div>

      {/* Node usage trending */}
      <div style={sectionHeader}>Daily Node Usage</div>
      {dailyCounts.length === 0 ? (
        <EmptyState title="No usage data" description="Usage events will appear once agents report." icon="&#x2B22;" />
      ) : (
        <div style={chartContainer}>
          <div style={trendRow}>
            {dailyCounts.map((d) => {
              const height = Math.max((d.node_count / maxDaily) * 100, 4);
              const isOverCommit = d.node_count > committed;
              return (
                <div
                  key={d.date}
                  title={`${d.date}: ${d.node_count} nodes`}
                  style={{
                    flex: 1,
                    minWidth: 4,
                    maxWidth: 20,
                    height: `${height}%`,
                    background: isOverCommit ? "var(--warning)" : "var(--accent)",
                    borderRadius: "var(--radius-sm) var(--radius-sm) 0 0",
                    cursor: "default",
                  }}
                />
              );
            })}
          </div>
          {committed > 0 && (
            <div
              style={{
                borderTop: "2px dashed var(--text-muted)",
                marginTop: `-${Math.max((committed / maxDaily) * 100, 4)}%`,
                position: "relative",
              }}
            >
              <span
                style={{
                  position: "absolute",
                  right: 0,
                  top: -16,
                  fontSize: "0.625rem",
                  color: "var(--text-muted)",
                  fontWeight: 600,
                }}
              >
                Commit: {committed}
              </span>
            </div>
          )}
        </div>
      )}

      {/* Per-site breakdown */}
      <div style={sectionHeader}>Per-Site Breakdown</div>
      {(usage?.per_site ?? []).length === 0 ? (
        <EmptyState title="No site data" description="Per-site usage will appear once agents report." icon="&#x2B22;" />
      ) : (
        <DataTable<SiteUsage>
          columns={siteColumns}
          data={usage?.per_site ?? []}
          loading={false}
          emptyMessage="No site data"
          striped
        />
      )}

      {/* Billing breakdown */}
      <div style={sectionHeader}>Billing Breakdown</div>
      <div style={cardStyle}>
        <div style={{ display: "flex", justifyContent: "space-between", padding: "0.375rem 0", borderBottom: "1px solid var(--border-light)", fontSize: "0.8125rem" }}>
          <span style={{ color: "var(--text-secondary)" }}>Base node fee ({committed} nodes)</span>
          <span style={{ fontWeight: 600 }}>{formatCents(baseCost, currency)}</span>
        </div>
        <div style={{ display: "flex", justifyContent: "space-between", padding: "0.375rem 0", borderBottom: "1px solid var(--border-light)", fontSize: "0.8125rem" }}>
          <span style={{ color: "var(--text-secondary)" }}>Overage ({estimate?.estimated_overage ?? 0} nodes)</span>
          <span style={{ fontWeight: 600, color: overageCost > 0 ? "var(--warning)" : "inherit" }}>
            {formatCents(overageCost, currency)}
          </span>
        </div>
        <div style={{ display: "flex", justifyContent: "space-between", padding: "0.5rem 0", fontSize: "0.875rem", fontWeight: 700 }}>
          <span>Total</span>
          <span>{formatCents(baseCost + overageCost, currency)}</span>
        </div>
      </div>
    </div>
  );
}
