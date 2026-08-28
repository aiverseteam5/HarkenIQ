import { type CSSProperties, useCallback, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import PageHeader from "../components/PageHeader";
import MetricCard from "../components/MetricCard";
import DataTable, { type Column } from "../components/DataTable";
import StatusBadge from "../components/StatusBadge";
import EmptyState from "../components/EmptyState";
import Spinner from "../components/Spinner";
import Toast from "../components/Toast";
import { useToast } from "../components/useToast";
import { getJson, postJson } from "../api";
import type {
  Subscription,
  Invoice,
  PaymentRecord,
  TrueUpEstimate,
  DelinquencyState,
} from "../types";

/* ── Styles ───────────────────────────────────────── */

const metricsRow: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))",
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

const cardTitle: CSSProperties = {
  fontSize: "0.8125rem",
  fontWeight: 600,
  color: "var(--text-secondary)",
  textTransform: "uppercase",
  letterSpacing: "0.04em",
  marginBottom: "0.75rem",
};

const infoRow: CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  padding: "0.375rem 0",
  fontSize: "0.8125rem",
  borderBottom: "1px solid var(--border-light)",
};

const infoLabel: CSSProperties = { color: "var(--text-secondary)", fontWeight: 500 };
const infoValue: CSSProperties = { color: "var(--text-primary)", fontWeight: 600 };

const sectionHeader: CSSProperties = {
  fontSize: "0.9375rem",
  fontWeight: 600,
  color: "var(--text-primary)",
  marginBottom: "0.75rem",
  marginTop: "1.5rem",
};

const STATUS_VARIANT: Record<string, "success" | "warning" | "critical" | "info" | "neutral"> = {
  draft: "neutral",
  issued: "info",
  paid: "success",
  void: "neutral",
};

const TYPE_VARIANT: Record<string, "info" | "warning"> = {
  commit: "info",
  overage: "warning",
};

/* ── Helpers ──────────────────────────────────────── */

function formatCents(cents: number, currency: string): string {
  const symbols: Record<string, string> = { USD: "$", INR: "\u20B9", EUR: "\u20AC" };
  const sym = symbols[currency] ?? currency + " ";
  return `${sym}${(cents / 100).toLocaleString("en-US", { minimumFractionDigits: 2 })}`;
}

function formatDate(iso: string | null): string {
  if (!iso) return "--";
  return new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}

/* ── Component ────────────────────────────────────── */

const PAGE_SIZE = 10;

export default function BillingDashboard() {
  const navigate = useNavigate();
  const { toasts, toast, dismiss } = useToast();
  const [loading, setLoading] = useState(true);
  const [sub, setSub] = useState<Subscription | null>(null);
  const [estimate, setEstimate] = useState<TrueUpEstimate | null>(null);
  const [delinquency, setDelinquency] = useState<DelinquencyState | null>(null);
  const [invoices, setInvoices] = useState<Invoice[]>([]);
  const [invoiceTotal, setInvoiceTotal] = useState(0);
  const [invoicePage, setInvoicePage] = useState(1);
  const [payments, setPayments] = useState<PaymentRecord[]>([]);
  const [paying, setPaying] = useState<string | null>(null);

  // Use tenant_id from a reasonable default -- in production this comes from auth context
  const { tenantId = "" } = useParams<{ tenantId: string }>();

  const fetchAll = useCallback(async () => {
    setLoading(true);
    try {
      const [subRes, estRes, delRes, invRes, payRes] = await Promise.allSettled([
        getJson<{ subscription: Subscription }>(`/api/tenants/${tenantId}/subscription`),
        getJson<TrueUpEstimate>(`/api/tenants/${tenantId}/usage/estimate`),
        getJson<DelinquencyState>(`/api/tenants/${tenantId}/delinquency`),
        getJson<{ items: Invoice[]; total: number }>(`/api/tenants/${tenantId}/invoices?page=${invoicePage}&page_size=${PAGE_SIZE}`),
        getJson<{ items: PaymentRecord[]; total: number }>(`/api/tenants/${tenantId}/payments?page=1&page_size=10`),
      ]);
      if (subRes.status === "fulfilled") setSub(subRes.value.subscription);
      if (estRes.status === "fulfilled") setEstimate(estRes.value);
      if (delRes.status === "fulfilled") setDelinquency(delRes.value);
      if (invRes.status === "fulfilled") {
        setInvoices(invRes.value.items);
        setInvoiceTotal(invRes.value.total);
      }
      if (payRes.status === "fulfilled") setPayments(payRes.value.items);
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to load billing data", "error");
    } finally {
      setLoading(false);
    }
  }, [tenantId, invoicePage, toast]);

  useEffect(() => {
    void fetchAll();
  }, [fetchAll]);

  const handlePay = useCallback(
    async (invoiceId: string) => {
      setPaying(invoiceId);
      try {
        await postJson(`/api/tenants/${tenantId}/invoices/${invoiceId}/pay`, {});
        toast("Payment initiated", "success");
        void fetchAll();
      } catch (err) {
        toast(err instanceof Error ? err.message : "Payment failed", "error");
      } finally {
        setPaying(null);
      }
    },
    [tenantId, toast, fetchAll],
  );

  const currency = sub?.currency ?? "USD";

  const invoiceColumns: Column<Invoice>[] = [
    { key: "invoice_number", header: "#", render: (r) => <code>{r.invoice_number}</code> },
    {
      key: "type",
      header: "Type",
      render: (r) => <StatusBadge status={r.type} variant={TYPE_VARIANT[r.type] ?? "neutral"} size="sm" />,
    },
    { key: "total_cents", header: "Amount", render: (r) => formatCents(r.total_cents, r.currency) },
    {
      key: "status",
      header: "Status",
      render: (r) => <StatusBadge status={r.status} variant={STATUS_VARIANT[r.status] ?? "neutral"} size="sm" />,
    },
    { key: "issued_at", header: "Issued", render: (r) => formatDate(r.issued_at) },
    { key: "due_at", header: "Due", render: (r) => formatDate(r.due_at) },
    {
      key: "actions",
      header: "",
      render: (r) =>
        r.status === "issued" ? (
          <button
            className="btn btn-sm btn-primary"
            onClick={(e) => {
              e.stopPropagation();
              void handlePay(r.id);
            }}
            disabled={paying === r.id}
          >
            {paying === r.id ? "..." : "Pay"}
          </button>
        ) : null,
    },
  ];

  const paymentColumns: Column<PaymentRecord>[] = [
    { key: "provider", header: "Provider", render: (r) => r.provider },
    { key: "amount_cents", header: "Amount", render: (r) => formatCents(r.amount_cents, r.currency) },
    {
      key: "status",
      header: "Status",
      render: (r) => (
        <StatusBadge
          status={r.status}
          variant={r.status === "completed" ? "success" : r.status === "failed" ? "critical" : "neutral"}
          size="sm"
        />
      ),
    },
    { key: "created_at", header: "Date", render: (r) => formatDate(r.created_at) },
  ];

  if (loading) {
    return (
      <div>
        <PageHeader title="Billing" breadcrumbs={[{ label: "HarkenIQ" }, { label: "Billing" }, { label: "Overview" }]} />
        <div style={{ display: "flex", justifyContent: "center", padding: "4rem" }}>
          <Spinner size="lg" />
        </div>
      </div>
    );
  }

  if (!sub) {
    return (
      <div>
        <PageHeader title="Billing" breadcrumbs={[{ label: "HarkenIQ" }, { label: "Billing" }, { label: "Overview" }]} />
        <EmptyState
          title="No subscription"
          description="No active subscription found for this tenant."
          icon="&#x2B22;"
        />
      </div>
    );
  }

  return (
    <div>
      <Toast toasts={toasts} onDismiss={dismiss} />
      <PageHeader title="Billing" breadcrumbs={[{ label: "HarkenIQ" }, { label: "Billing" }, { label: "Overview" }]} />

      {/* Delinquency banner */}
      {delinquency && delinquency.status !== "current" && (
        <div
          style={{
            background: delinquency.status === "restricted" ? "var(--critical-bg)" : "var(--warning-bg)",
            color: delinquency.status === "restricted" ? "var(--critical)" : "var(--warning)",
            padding: "0.75rem 1rem",
            borderRadius: "var(--radius-md)",
            marginBottom: "1rem",
            fontSize: "0.875rem",
            fontWeight: 600,
          }}
        >
          Account is {delinquency.status.toUpperCase()} -- {delinquency.days_overdue} days overdue
          ({formatCents(delinquency.overdue_amount_cents, currency)} outstanding)
        </div>
      )}

      {/* Subscription + estimate cards */}
      <div style={metricsRow}>
        <MetricCard title="Plan" value={sub.plan.charAt(0).toUpperCase() + sub.plan.slice(1)} />
        <MetricCard title="Committed Nodes" value={sub.node_commit} />
        <MetricCard
          title="High-Water (this period)"
          value={estimate?.high_water_so_far ?? "--"}
          trend={estimate && estimate.high_water_so_far > sub.node_commit ? "up" : "flat"}
        />
        <MetricCard
          title="Est. True-Up"
          value={estimate ? formatCents(estimate.estimated_amount_cents, currency) : "--"}
          trend={estimate && estimate.estimated_overage > 0 ? "up" : "flat"}
        />
      </div>

      {/* Subscription detail card */}
      <div style={cardStyle}>
        <div style={cardTitle}>Subscription</div>
        <div style={infoRow}>
          <span style={infoLabel}>Billing Frequency</span>
          <span style={infoValue}>{sub.billing_frequency}</span>
        </div>
        <div style={infoRow}>
          <span style={infoLabel}>Billing Cycle Start</span>
          <span style={infoValue}>{formatDate(sub.billing_cycle_start)}</span>
        </div>
        <div style={infoRow}>
          <span style={infoLabel}>Status</span>
          <span style={infoValue}>
            <StatusBadge status={sub.status} variant={sub.status === "active" ? "success" : "warning"} size="sm" />
          </span>
        </div>
        <div style={infoRow}>
          <span style={infoLabel}>Currency</span>
          <span style={infoValue}>{currency}</span>
        </div>
      </div>

      {/* Invoices */}
      <div style={sectionHeader}>Invoices</div>
      {invoices.length === 0 ? (
        <EmptyState title="No invoices yet" description="Invoices will appear here once generated." icon="&#x2B22;" />
      ) : (
        <DataTable<Invoice>
          columns={invoiceColumns}
          data={invoices}
          loading={false}
          emptyMessage="No invoices"
          page={invoicePage}
          pageSize={PAGE_SIZE}
          total={invoiceTotal}
          onPageChange={setInvoicePage}
          onRowClick={(inv) => navigate(`/invoices/${inv.id}`)}
          striped
        />
      )}

      {/* Payments */}
      <div style={sectionHeader}>Payment History</div>
      {payments.length === 0 ? (
        <EmptyState title="No payments" description="Payment records will appear here." icon="&#x2B22;" />
      ) : (
        <DataTable<PaymentRecord>
          columns={paymentColumns}
          data={payments}
          loading={false}
          emptyMessage="No payments"
          striped
        />
      )}
    </div>
  );
}
