import { useParams } from "react-router-dom";
import { type CSSProperties, useCallback, useEffect, useState } from "react";
import PageHeader from "../components/PageHeader";
import MetricCard from "../components/MetricCard";
import DataTable, { type Column } from "../components/DataTable";
import StatusBadge from "../components/StatusBadge";
import EmptyState from "../components/EmptyState";
import Spinner from "../components/Spinner";
import Toast from "../components/Toast";
import { useToast } from "../components/useToast";
import { getJson } from "../api";

/* ── Types (CC /api/t/{tenantId}/outcomes) ─────────────────────── */

interface OutcomeMetric {
  action_type: string;
  vendor: string;
  model: string;
  total_count: number;
  success_count: number;
  failure_count: number;
  partial_count: number;
  success_rate: number;
  failure_rate: number;
  resolution_rate: number;
  site_count: number;
  failing_site_count: number;
  sites: string[];
}

interface FleetPattern {
  pattern_id: string;
  pattern_type: string;
  description: string;
  affected_scope: Record<string, string>;
  confidence: number;
  evidence: Record<string, unknown>;
  status: string;
  detected_at: string | null;
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

const barRow: CSSProperties = {
  display: "grid", gridTemplateColumns: "200px 1fr 90px",
  alignItems: "center", gap: "0.75rem", padding: "0.3125rem 0",
};

const barLabel: CSSProperties = {
  fontSize: "0.8125rem", color: "var(--text-secondary)",
  whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis",
};

const barTrack: CSSProperties = {
  height: 14, background: "var(--bg-subtle, transparent)",
  borderRadius: "var(--radius-sm)", overflow: "hidden",
};

const barValue: CSSProperties = {
  fontSize: "0.8125rem", fontWeight: 600, color: "var(--text-primary)",
  textAlign: "right", fontVariantNumeric: "tabular-nums",
};

const PATTERN_VARIANT: Record<string, "critical" | "warning" | "neutral"> = {
  cross_site_batch: "critical",
  batch_failure: "critical",
  anomaly: "warning",
  reliability: "warning",
};

/* ── Helpers ──────────────────────────────────────── */

function formatDate(iso: string | null): string {
  if (!iso) return "--";
  return new Date(iso).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}

function pct(rate: number): string {
  return `${(rate * 100).toFixed(1)}%`;
}

function groupLabel(m: OutcomeMetric): string {
  return `${m.vendor} ${m.model} · ${m.action_type}`;
}

/* ── Component ────────────────────────────────────── */

export default function VendorReliability() {
  const { tenantId = "" } = useParams<{ tenantId: string }>();
  const { toasts, toast, dismiss } = useToast();
  const [loading, setLoading] = useState(true);
  const [metrics, setMetrics] = useState<OutcomeMetric[]>([]);
  const [patterns, setPatterns] = useState<FleetPattern[]>([]);

  const fetchAll = useCallback(async () => {
    setLoading(true);
    try {
      const [metricsRes, patternsRes] = await Promise.allSettled([
        getJson<{ metrics: OutcomeMetric[]; total_outcomes: number }>(`/api/t/${tenantId}/outcomes/metrics`),
        getJson<{ patterns: FleetPattern[] }>(`/api/t/${tenantId}/outcomes/patterns`),
      ]);
      if (metricsRes.status === "fulfilled") setMetrics(metricsRes.value.metrics);
      if (patternsRes.status === "fulfilled") setPatterns(patternsRes.value.patterns);
      if (metricsRes.status === "rejected" && patternsRes.status === "rejected") {
        toast("Failed to load reliability data", "error");
      }
    } finally {
      setLoading(false);
    }
  }, [toast]);

  useEffect(() => { void fetchAll(); }, [fetchAll]);

  const totalOutcomes = metrics.reduce((sum, m) => sum + m.total_count, 0);
  const crossSiteCount = patterns.filter(p => p.pattern_type === "cross_site_batch").length;
  const byFailureRate = [...metrics]
    .filter(m => m.total_count > 0)
    .sort((a, b) => b.failure_rate - a.failure_rate)
    .slice(0, 12);
  const maxFailureRate = Math.max(...byFailureRate.map(m => m.failure_rate), 0.01);

  const metricColumns: Column<OutcomeMetric>[] = [
    { key: "vendor", header: "Vendor" },
    { key: "model", header: "Model" },
    { key: "action_type", header: "Action" },
    { key: "total_count", header: "Outcomes", render: (m) => String(m.total_count) },
    { key: "success_rate", header: "Success", render: (m) => pct(m.success_rate) },
    { key: "failure_rate", header: "Failure", render: (m) => pct(m.failure_rate) },
    { key: "resolution_rate", header: "Fault Resolved", render: (m) => pct(m.resolution_rate) },
    { key: "site_count", header: "Sites", render: (m) => (
      m.failing_site_count >= 2
        ? <span title={m.sites.join(", ")}>{m.site_count} ({m.failing_site_count} failing)</span>
        : <span title={m.sites.join(", ")}>{String(m.site_count)}</span>
    )},
  ];

  const patternColumns: Column<FleetPattern>[] = [
    { key: "pattern_type", header: "Type", render: (p) => (
      <StatusBadge
        status={p.pattern_type.replace(/_/g, " ")}
        variant={PATTERN_VARIANT[p.pattern_type] ?? "neutral"}
        size="sm"
      />
    )},
    { key: "description", header: "Description" },
    { key: "confidence", header: "Confidence", render: (p) => pct(p.confidence) },
    { key: "sites", header: "Sites", render: (p) => p.affected_scope.sites ?? "--" },
    { key: "detected_at", header: "Detected", render: (p) => formatDate(p.detected_at) },
  ];

  if (loading) {
    return (
      <div>
        <PageHeader title="Vendor Reliability" breadcrumbs={[{ label: "HarkenIQ" }, { label: "Fleet" }, { label: "Vendor Reliability" }]} />
        <div style={{ display: "flex", justifyContent: "center", padding: "4rem" }}><Spinner size="lg" /></div>
      </div>
    );
  }

  return (
    <div>
      <Toast toasts={toasts} onDismiss={dismiss} />
      <PageHeader
        title="Vendor Reliability"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Fleet" }, { label: "Vendor Reliability" }]}
        actions={
          <button
            type="button"
            onClick={() => void fetchAll()}
            style={{
              padding: "0.375rem 0.75rem", fontSize: "0.8125rem", fontWeight: 500,
              background: "var(--bg-card)", color: "var(--text-primary)",
              border: "1px solid var(--border-light)", borderRadius: "var(--radius-sm)",
              cursor: "pointer",
            }}
          >
            Refresh
          </button>
        }
      />

      {/* KPI row */}
      <div style={metricsRow}>
        <MetricCard title="Action Outcomes" value={totalOutcomes} />
        <MetricCard title="Vendor / Model Groups" value={metrics.length} />
        <MetricCard title="Active Patterns" value={patterns.length} trend={patterns.length > 0 ? "down" : undefined} />
        <MetricCard title="Cross-Site Patterns" value={crossSiteCount} trend={crossSiteCount > 0 ? "down" : undefined} />
      </div>

      {/* Failure rate comparison — single measure, one hue; table below is the data view */}
      <div style={sectionHeader}>Failure Rate by Vendor / Model</div>
      {byFailureRate.length === 0 ? (
        <EmptyState title="No outcome data yet" description="Reliability comparisons appear once agents report action outcomes." icon="&#x2318;" />
      ) : (
        <div style={cardStyle}>
          {byFailureRate.map((m) => (
            <div
              key={groupLabel(m)}
              style={barRow}
              title={`${groupLabel(m)}: ${m.failure_count}/${m.total_count} failed across ${m.site_count} site(s)`}
            >
              <div style={barLabel}>{groupLabel(m)}</div>
              <div style={barTrack}>
                <div style={{
                  width: `${Math.max((m.failure_rate / maxFailureRate) * 100, 1.5)}%`,
                  height: "100%",
                  background: "var(--accent)",
                  borderRadius: "var(--radius-sm)",
                }} />
              </div>
              <div style={barValue}>{pct(m.failure_rate)}</div>
            </div>
          ))}
        </div>
      )}

      {/* Detected patterns */}
      <div style={sectionHeader}>Detected Fleet Patterns</div>
      {patterns.length === 0 ? (
        <EmptyState title="No active patterns" description="Batch failures, cross-site correlations, and anomalies appear here when detected." icon="&#x2714;" />
      ) : (
        <DataTable<FleetPattern> columns={patternColumns} data={patterns} loading={false} emptyMessage="No patterns" striped />
      )}

      {/* Full metrics table */}
      <div style={sectionHeader}>Outcome Metrics</div>
      {metrics.length === 0 ? (
        <EmptyState title="No metrics" description="Aggregated outcome metrics appear once actions have executed." icon="&#x2261;" />
      ) : (
        <DataTable<OutcomeMetric> columns={metricColumns} data={metrics} loading={false} emptyMessage="No metrics" striped />
      )}
    </div>
  );
}
