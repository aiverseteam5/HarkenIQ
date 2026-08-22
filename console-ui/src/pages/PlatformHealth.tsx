import { type CSSProperties, useCallback, useEffect, useState } from "react";
import PageHeader from "../components/PageHeader";
import MetricCard from "../components/MetricCard";
import StatusBadge from "../components/StatusBadge";
import Spinner from "../components/Spinner";
import Toast from "../components/Toast";
import { useToast } from "../components/useToast";
import { getJson } from "../api";

/* ── Types ────────────────────────────────────────── */

interface ServiceHealth {
  status: string;
  version?: string;
  note?: string;
}

interface DetailedHealth {
  services: Record<string, ServiceHealth>;
  checks: {
    database?: { status: string; latency_ms?: number; error?: string };
    data?: { tenants?: number; invoices?: number; tickets?: number; status?: string };
  };
}

/* ── Styles ───────────────────────────────────────── */

const sectionHeader: CSSProperties = {
  fontSize: "0.9375rem", fontWeight: 600, color: "var(--text-primary)",
  marginBottom: "0.75rem", marginTop: "1.5rem",
};

const cardStyle: CSSProperties = {
  background: "var(--bg-card)", borderRadius: "var(--radius-md)",
  border: "1px solid var(--border-light)", padding: "1.25rem", marginBottom: "1.5rem",
};

const serviceGrid: CSSProperties = {
  display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))",
  gap: "1rem", marginBottom: "1.5rem",
};

const serviceCard: CSSProperties = {
  ...cardStyle, textAlign: "center", padding: "1.5rem 1rem", marginBottom: 0,
};

const statusDot: CSSProperties = {
  width: 12, height: 12, borderRadius: "50%", display: "inline-block", marginRight: "0.5rem",
};

const checkRow: CSSProperties = {
  display: "flex", justifyContent: "space-between", padding: "0.5rem 0",
  borderBottom: "1px solid var(--border-light)", fontSize: "0.8125rem",
};

const POLL_INTERVAL = 30000;

const STATUS_COLOR: Record<string, string> = {
  healthy: "#22c55e", unhealthy: "#ef4444", unknown: "#9ca3af", degraded: "#f59e0b",
};

/* ── Component ────────────────────────────────────── */

export default function PlatformHealth() {
  const { toasts, toast, dismiss } = useToast();
  const [loading, setLoading] = useState(true);
  const [health, setHealth] = useState<DetailedHealth | null>(null);

  const fetchHealth = useCallback(async () => {
    try {
      const res = await getJson<DetailedHealth>("/api/admin/health/detailed");
      setHealth(res);
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to load health", "error");
    } finally {
      setLoading(false);
    }
  }, [toast]);

  useEffect(() => { void fetchHealth(); }, [fetchHealth]);
  useEffect(() => {
    const timer = setInterval(() => { void fetchHealth(); }, POLL_INTERVAL);
    return () => clearInterval(timer);
  }, [fetchHealth]);

  if (loading) return (
    <div>
      <PageHeader title="Platform Health" breadcrumbs={[{ label: "HarkenIQ" }, { label: "Admin" }, { label: "Health" }]} />
      <div style={{ display: "flex", justifyContent: "center", padding: "4rem" }}><Spinner size="lg" /></div>
    </div>
  );

  const services = health?.services ?? {};
  const checks = health?.checks ?? {};
  const allHealthy = Object.values(services).every(s => s.status === "healthy");

  return (
    <div>
      <Toast toasts={toasts} onDismiss={dismiss} />
      <PageHeader title="Platform Health" breadcrumbs={[{ label: "HarkenIQ" }, { label: "Admin" }, { label: "Health" }]} />

      {/* Overall status */}
      <div style={{ ...cardStyle, display: "flex", alignItems: "center", gap: "0.75rem" }}>
        <span style={{ ...statusDot, background: allHealthy ? STATUS_COLOR.healthy : STATUS_COLOR.unhealthy, width: 16, height: 16 }} />
        <span style={{ fontSize: "1rem", fontWeight: 600 }}>
          {allHealthy ? "All systems operational" : "Some services need attention"}
        </span>
      </div>

      {/* Service cards */}
      <div style={sectionHeader}>Services</div>
      <div style={serviceGrid}>
        {Object.entries(services).map(([name, info]) => (
          <div key={name} style={serviceCard}>
            <span style={{ ...statusDot, background: STATUS_COLOR[info.status] ?? STATUS_COLOR.unknown }} />
            <div style={{ fontSize: "0.9375rem", fontWeight: 600, marginTop: "0.5rem", textTransform: "capitalize" }}>{name.replace(/_/g, " ")}</div>
            <StatusBadge status={info.status} variant={info.status === "healthy" ? "success" : info.status === "unhealthy" ? "critical" : "neutral"} size="sm" />
            {info.version && <div style={{ fontSize: "0.6875rem", color: "var(--text-muted)", marginTop: "0.25rem" }}>v{info.version}</div>}
            {info.note && <div style={{ fontSize: "0.6875rem", color: "var(--text-muted)", marginTop: "0.25rem" }}>{info.note}</div>}
          </div>
        ))}
      </div>

      {/* Database check */}
      {checks.database && (
        <>
          <div style={sectionHeader}>Database</div>
          <div style={cardStyle}>
            <div style={checkRow}>
              <span>Connectivity</span>
              <StatusBadge status={checks.database.status} variant={checks.database.status === "healthy" ? "success" : "critical"} size="sm" />
            </div>
            {checks.database.latency_ms !== undefined && (
              <div style={checkRow}>
                <span>Latency</span>
                <span style={{ fontWeight: 600 }}>{checks.database.latency_ms}ms</span>
              </div>
            )}
            {checks.database.error && (
              <div style={checkRow}>
                <span>Error</span>
                <span style={{ color: "var(--critical)", fontSize: "0.75rem" }}>{checks.database.error}</span>
              </div>
            )}
          </div>
        </>
      )}

      {/* Data counts */}
      {checks.data && checks.data.tenants !== undefined && (
        <>
          <div style={sectionHeader}>Data Summary</div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(160px, 1fr))", gap: "1rem" }}>
            <MetricCard title="Tenants" value={checks.data.tenants ?? 0} />
            <MetricCard title="Invoices" value={checks.data.invoices ?? 0} />
            <MetricCard title="Tickets" value={checks.data.tickets ?? 0} />
          </div>
        </>
      )}
    </div>
  );
}
