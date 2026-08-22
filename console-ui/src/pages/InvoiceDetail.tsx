import { type CSSProperties, useCallback, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import PageHeader from "../components/PageHeader";
import StatusBadge from "../components/StatusBadge";
import DataTable, { type Column } from "../components/DataTable";
import Spinner from "../components/Spinner";
import Toast from "../components/Toast";
import { useToast } from "../components/useToast";
import { getJson, postJson } from "../api";
import type { Invoice, InvoiceLine, CreditNote, PaymentRecord } from "../types";

/* ── Styles ───────────────────────────────────────── */

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

const headerCard: CSSProperties = {
  ...cardStyle,
  display: "grid",
  gridTemplateColumns: "repeat(auto-fill, minmax(160px, 1fr))",
  gap: "1rem",
};

const statBlock: CSSProperties = {
  textAlign: "center",
};

const statValue: CSSProperties = {
  fontSize: "1.375rem",
  fontWeight: 700,
  color: "var(--text-primary)",
};

const statLabel: CSSProperties = {
  fontSize: "0.6875rem",
  fontWeight: 600,
  color: "var(--text-secondary)",
  textTransform: "uppercase",
  letterSpacing: "0.04em",
  marginTop: "0.25rem",
};

const barContainer: CSSProperties = {
  display: "flex",
  height: 24,
  borderRadius: "var(--radius-sm)",
  overflow: "hidden",
  marginBottom: "1rem",
  background: "var(--bg-primary)",
};

const sectionHeader: CSSProperties = {
  fontSize: "0.9375rem",
  fontWeight: 600,
  color: "var(--text-primary)",
  marginBottom: "0.75rem",
  marginTop: "1.5rem",
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

const STATUS_VARIANT: Record<string, "success" | "warning" | "critical" | "info" | "neutral"> = {
  draft: "neutral",
  issued: "info",
  paid: "success",
  void: "neutral",
};

/* ── Component ────────────────────────────────────── */

interface InvoiceDetailData {
  invoice: Invoice;
  lines: InvoiceLine[];
  credit_notes: CreditNote[];
  payments: PaymentRecord[];
}

export default function InvoiceDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { toasts, toast, dismiss } = useToast();
  const [loading, setLoading] = useState(true);
  const [data, setData] = useState<InvoiceDetailData | null>(null);
  const [paying, setPaying] = useState(false);

  const tenantId = "current";

  const fetchInvoice = useCallback(async () => {
    setLoading(true);
    try {
      const res = await getJson<InvoiceDetailData>(`/api/tenants/${tenantId}/invoices/${id}`);
      setData(res);
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to load invoice", "error");
    } finally {
      setLoading(false);
    }
  }, [id, tenantId, toast]);

  useEffect(() => {
    void fetchInvoice();
  }, [fetchInvoice]);

  const handlePay = useCallback(async () => {
    if (!id) return;
    setPaying(true);
    try {
      await postJson(`/api/tenants/${tenantId}/invoices/${id}/pay`, {});
      toast("Payment initiated", "success");
      void fetchInvoice();
    } catch (err) {
      toast(err instanceof Error ? err.message : "Payment failed", "error");
    } finally {
      setPaying(false);
    }
  }, [id, tenantId, toast, fetchInvoice]);

  if (loading) {
    return (
      <div>
        <PageHeader
          title="Invoice"
          breadcrumbs={[{ label: "HarkenIQ" }, { label: "Billing", href: "/billing" }, { label: "Invoice" }]}
        />
        <div style={{ display: "flex", justifyContent: "center", padding: "4rem" }}>
          <Spinner size="lg" />
        </div>
      </div>
    );
  }

  if (!data) {
    return (
      <div>
        <PageHeader
          title="Invoice"
          breadcrumbs={[{ label: "HarkenIQ" }, { label: "Billing", href: "/billing" }, { label: "Invoice" }]}
        />
        <div style={cardStyle}>Invoice not found.</div>
      </div>
    );
  }

  const { invoice, lines, credit_notes, payments } = data;
  const currency = invoice.currency;

  // Calculate commit vs overage breakdown for visual bar
  const commitTotal = lines.filter((l) => l.line_type === "commit").reduce((s, l) => s + l.amount_cents, 0);
  const overageTotal = lines.filter((l) => l.line_type === "overage").reduce((s, l) => s + l.amount_cents, 0);
  const barTotal = commitTotal + overageTotal || 1;

  const lineColumns: Column<InvoiceLine>[] = [
    { key: "description", header: "Description" },
    { key: "quantity", header: "Qty", render: (r) => String(r.quantity) },
    { key: "unit_price_cents", header: "Unit Price", render: (r) => formatCents(r.unit_price_cents, currency) },
    { key: "amount_cents", header: "Amount", render: (r) => formatCents(r.amount_cents, currency) },
    {
      key: "line_type",
      header: "Type",
      render: (r) => (
        <StatusBadge
          status={r.line_type}
          variant={r.line_type === "commit" ? "info" : r.line_type === "overage" ? "warning" : "neutral"}
          size="sm"
        />
      ),
    },
  ];

  const creditColumns: Column<CreditNote>[] = [
    { key: "reason", header: "Reason" },
    { key: "amount_cents", header: "Amount", render: (r) => formatCents(r.amount_cents, currency) },
    { key: "issued_at", header: "Issued", render: (r) => formatDate(r.issued_at) },
  ];

  const paymentColumns: Column<PaymentRecord>[] = [
    { key: "provider", header: "Provider" },
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

  return (
    <div>
      <Toast toasts={toasts} onDismiss={dismiss} />
      <PageHeader
        title={`Invoice ${invoice.invoice_number}`}
        breadcrumbs={[
          { label: "HarkenIQ" },
          { label: "Billing", href: "/billing" },
          { label: invoice.invoice_number },
        ]}
        actions={
          invoice.status === "issued"
            ? [
                {
                  label: paying ? "Processing..." : "Pay Now",
                  onClick: handlePay,
                  variant: "primary" as const,
                },
              ]
            : undefined
        }
      />

      {/* Header card */}
      <div style={headerCard}>
        <div style={statBlock}>
          <div style={statValue}>
            <StatusBadge status={invoice.status} variant={STATUS_VARIANT[invoice.status] ?? "neutral"} />
          </div>
          <div style={statLabel}>Status</div>
        </div>
        <div style={statBlock}>
          <div style={statValue}>{formatCents(invoice.total_cents, currency)}</div>
          <div style={statLabel}>Total</div>
        </div>
        <div style={statBlock}>
          <div style={statValue}>{currency}</div>
          <div style={statLabel}>Currency</div>
        </div>
        <div style={statBlock}>
          <div style={statValue}>{formatDate(invoice.issued_at)}</div>
          <div style={statLabel}>Issued</div>
        </div>
        <div style={statBlock}>
          <div style={statValue}>{formatDate(invoice.due_at)}</div>
          <div style={statLabel}>Due</div>
        </div>
        <div style={statBlock}>
          <div style={statValue}>{formatDate(invoice.period_start)} - {formatDate(invoice.period_end)}</div>
          <div style={statLabel}>Period</div>
        </div>
      </div>

      {/* Commit vs overage bar */}
      {(commitTotal > 0 || overageTotal > 0) && (
        <div style={cardStyle}>
          <div style={cardTitle}>Commit vs Overage</div>
          <div style={barContainer}>
            {commitTotal > 0 && (
              <div
                style={{
                  width: `${(commitTotal / barTotal) * 100}%`,
                  background: "var(--accent)",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  color: "#fff",
                  fontSize: "0.6875rem",
                  fontWeight: 600,
                }}
              >
                Commit {formatCents(commitTotal, currency)}
              </div>
            )}
            {overageTotal > 0 && (
              <div
                style={{
                  width: `${(overageTotal / barTotal) * 100}%`,
                  background: "var(--warning)",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  color: "#fff",
                  fontSize: "0.6875rem",
                  fontWeight: 600,
                }}
              >
                Overage {formatCents(overageTotal, currency)}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Line items */}
      <div style={sectionHeader}>Line Items</div>
      <DataTable<InvoiceLine>
        columns={lineColumns}
        data={lines}
        loading={false}
        emptyMessage="No line items"
        striped
      />

      {/* Credit notes */}
      {credit_notes.length > 0 && (
        <>
          <div style={sectionHeader}>Credit Notes</div>
          <DataTable<CreditNote>
            columns={creditColumns}
            data={credit_notes}
            loading={false}
            emptyMessage="No credit notes"
            striped
          />
        </>
      )}

      {/* Payments */}
      <div style={sectionHeader}>Payments</div>
      {payments.length === 0 ? (
        <div style={{ ...cardStyle, textAlign: "center", color: "var(--text-muted)", fontSize: "0.875rem" }}>
          No payments recorded for this invoice.
        </div>
      ) : (
        <DataTable<PaymentRecord>
          columns={paymentColumns}
          data={payments}
          loading={false}
          emptyMessage="No payments"
          striped
        />
      )}

      <div style={{ marginTop: "1.5rem" }}>
        <button className="btn btn-sm" onClick={() => navigate("/billing")}>
          Back to Billing
        </button>
      </div>
    </div>
  );
}
