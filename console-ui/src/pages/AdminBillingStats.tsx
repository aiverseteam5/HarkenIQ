import { type CSSProperties, useCallback, useEffect, useState } from "react";
import PageHeader from "../components/PageHeader";
import MetricCard from "../components/MetricCard";
import DataTable, { type Column } from "../components/DataTable";
import StatusBadge from "../components/StatusBadge";
import EmptyState from "../components/EmptyState";
import Spinner from "../components/Spinner";
import Toast from "../components/Toast";
import { useToast } from "../components/useToast";
import { getJson, postJson } from "../api";

/* ── Types ────────────────────────────────────────── */

interface AdminBillingOverview {
  mrr_cents: number;
  arr_cents: number;
  active_tenants: number;
  total_nodes: number;
  currency: string;
}

interface RevenueByPlan {
  type: string;
  currency: string;
  total_cents: number;
  count: number;
}

interface DelinquentTenant {
  tenant_id: string;
  tenant_name: string;
  slug: string;
  delinquency_status: string;
  overdue_amount_cents: number;
  days_overdue: number;
  currency: string;
}

interface ReconciliationAlert {
  tenant_id: string;
  tenant_name: string;
  type: string;
  details: string;
}

/* ── Styles ───────────────────────────────────────── */

const metricsRow: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))",
  gap: "1rem",
  marginBottom: "1.5rem",
};

const sectionHeader: CSSProperties = {
  fontSize: "0.9375rem",
  fontWeight: 600,
  color: "var(--text-primary)",
  marginBottom: "0.75rem",
  marginTop: "1.5rem",
};

const cardStyle: CSSProperties = {
  background: "var(--bg-card)",
  borderRadius: "var(--radius-md)",
  border: "1px solid var(--border-light)",
  padding: "1.25rem",
  marginBottom: "1.5rem",
};

const barGroup: CSSProperties = {
  display: "flex",
  alignItems: "flex-end",
  gap: "0.75rem",
  height: 120,
  padding: "0.5rem 0",
};

const barItem: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  alignItems: "center",
  flex: 1,
};

const barLabel: CSSProperties = {
  fontSize: "0.6875rem",
  fontWeight: 600,
  color: "var(--text-secondary)",
  marginTop: "0.375rem",
  textTransform: "uppercase",
};

const DELINQUENCY_VARIANT: Record<string, "warning" | "critical" | "neutral"> = {
  overdue: "warning",
  restricted: "critical",
  suspended: "critical",
};

/* ── Helpers ──────────────────────────────────────── */

function formatCents(cents: number, currency: string): string {
  const symbols: Record<string, string> = { USD: "$", INR: "\u20B9", EUR: "\u20AC" };
  const sym = symbols[currency] ?? currency + " ";
  return `${sym}${(cents / 100).toLocaleString("en-US", { minimumFractionDigits: 2 })}`;
}

function formatCompact(cents: number, currency: string): string {
  const val = cents / 100;
  const symbols: Record<string, string> = { USD: "$", INR: "\u20B9", EUR: "\u20AC" };
  const sym = symbols[currency] ?? "";
  if (val >= 1_000_000) return `${sym}${(val / 1_000_000).toFixed(1)}M`;
  if (val >= 1_000) return `${sym}${(val / 1_000).toFixed(1)}K`;
  return `${sym}${val.toFixed(0)}`;
}

/* ── Component ────────────────────────────────────── */

export default function AdminBillingStats() {
  const { toasts, toast, dismiss } = useToast();
  const [loading, setLoading] = useState(true);
  const [overview, setOverview] = useState<AdminBillingOverview | null>(null);
  const [revenue, setRevenue] = useState<RevenueByPlan[]>([]);
  const [delinquents, setDelinquents] = useState<DelinquentTenant[]>([]);
  const [reconciliation, setReconciliation] = useState<ReconciliationAlert[]>([]);
  const [generating, setGenerating] = useState(false);

  const fetchAll = useCallback(async () => {
    setLoading(true);
    try {
      const [ovRes, revRes, delRes, recRes] = await Promise.allSettled([
        getJson<AdminBillingOverview>("/api/admin/billing/overview"),
        getJson<{ items: RevenueByPlan[] }>("/api/admin/billing/revenue"),
        getJson<{ items: DelinquentTenant[] }>("/api/admin/billing/delinquent"),
        getJson<{ items: ReconciliationAlert[] }>("/api/admin/billing/reconciliation"),
      ]);
      if (ovRes.status === "fulfilled") setOverview(ovRes.value);
      if (revRes.status === "fulfilled") setRevenue(revRes.value.items);
      if (delRes.status === "fulfilled") setDelinquents(delRes.value.items);
      if (recRes.status === "fulfilled") setReconciliation(recRes.value.items);
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to load admin billing", "error");
    } finally {
      setLoading(false);
    }
  }, [toast]);

  useEffect(() => {
    void fetchAll();
  }, [fetchAll]);

  const handleGenerateTrueups = useCallback(async () => {
    setGenerating(true);
    try {
      const res = await postJson<{ generated: number }>("/api/admin/invoices/generate-trueups", {});
      toast(`Generated ${res.generated} true-up invoices`, "success");
      void fetchAll();
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to generate true-ups", "error");
    } finally {
      setGenerating(false);
    }
  }, [toast, fetchAll]);

  const currency = overview?.currency ?? "USD";

  const delinquentColumns: Column<DelinquentTenant>[] = [
    { key: "tenant_name", header: "Tenant" },
    { key: "slug", header: "Slug", render: (r) => <code>{r.slug}</code> },
    {
      key: "delinquency_status",
      header: "Status",
      render: (r) => (
        <StatusBadge
          status={r.delinquency_status}
          variant={DELINQUENCY_VARIANT[r.delinquency_status] ?? "neutral"}
          size="sm"
        />
      ),
    },
    {
      key: "overdue_amount_cents",
      header: "Amount Owed",
      render: (r) => formatCents(r.overdue_amount_cents, r.currency),
    },
    {
      key: "days_overdue",
      header: "Days Overdue",
      render: (r) => (
        <span style={{ fontWeight: r.days_overdue >= 14 ? 700 : 400, color: r.days_overdue >= 14 ? "var(--critical)" : "inherit" }}>
          {r.days_overdue}
        </span>
      ),
    },
  ];

  // Revenue chart -- simple bar visualization
  const maxRevenue = Math.max(...revenue.map((r) => r.total_cents), 1);

  if (loading) {
    return (
      <div>
        <PageHeader
          title="Billing Administration"
          breadcrumbs={[{ label: "HarkenIQ" }, { label: "Admin" }, { label: "Billing" }]}
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
        title="Billing Administration"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Admin" }, { label: "Billing" }]}
        actions={[
          {
            label: generating ? "Generating..." : "Generate True-Ups",
            onClick: handleGenerateTrueups,
            variant: "primary" as const,
          },
        ]}
      />

      {/* KPI cards */}
      <div style={metricsRow}>
        <MetricCard
          title="MRR"
          value={overview ? formatCompact(overview.mrr_cents, currency) : "--"}
        />
        <MetricCard
          title="ARR"
          value={overview ? formatCompact(overview.arr_cents, currency) : "--"}
        />
        <MetricCard title="Active Tenants" value={overview?.active_tenants ?? "--"} />
        <MetricCard title="Total Nodes" value={overview?.total_nodes ?? "--"} />
      </div>

      {/* Revenue by plan chart */}
      <div style={sectionHeader}>Revenue by Type</div>
      {revenue.length === 0 ? (
        <EmptyState title="No revenue data" description="Revenue data will appear once invoices are paid." icon="&#x2211;" />
      ) : (
        <div style={cardStyle}>
          <div style={barGroup}>
            {revenue.map((r, i) => (
              <div key={`${r.type}-${r.currency}-${i}`} style={barItem}>
                <div
                  style={{
                    width: "100%",
                    maxWidth: 60,
                    height: `${Math.max((r.total_cents / maxRevenue) * 100, 8)}%`,
                    background: r.type === "commit" ? "var(--accent)" : "var(--warning)",
                    borderRadius: "var(--radius-sm) var(--radius-sm) 0 0",
                    display: "flex",
                    alignItems: "flex-start",
                    justifyContent: "center",
                    paddingTop: "0.25rem",
                    color: "#fff",
                    fontSize: "0.625rem",
                    fontWeight: 700,
                  }}
                >
                  {formatCompact(r.total_cents, r.currency)}
                </div>
                <div style={barLabel}>
                  {r.type} ({r.currency})
                </div>
                <div style={{ fontSize: "0.625rem", color: "var(--text-muted)" }}>{r.count} inv</div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Delinquency dashboard */}
      <div style={sectionHeader}>Delinquent Tenants</div>
      {delinquents.length === 0 ? (
        <div style={{ ...cardStyle, textAlign: "center", color: "var(--text-muted)", fontSize: "0.875rem" }}>
          No delinquent tenants. All accounts are current.
        </div>
      ) : (
        <DataTable<DelinquentTenant>
          columns={delinquentColumns}
          data={delinquents}
          loading={false}
          emptyMessage="No delinquent tenants"
          striped
        />
      )}

      {/* Reconciliation alerts */}
      {reconciliation.length > 0 && (
        <>
          <div style={sectionHeader}>Reconciliation Alerts</div>
          <div style={cardStyle}>
            {reconciliation.map((alert, i) => (
              <div
                key={i}
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  padding: "0.5rem 0",
                  borderBottom: i < reconciliation.length - 1 ? "1px solid var(--border-light)" : "none",
                  fontSize: "0.8125rem",
                }}
              >
                <span>
                  <strong>{alert.tenant_name}</strong> -- {alert.type}
                </span>
                <span style={{ color: "var(--text-muted)" }}>{alert.details}</span>
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
