import { useParams } from "react-router-dom";
import { useCallback, useEffect, useState } from "react";
import type { CSSProperties } from "react";
import MetricCard from "../components/MetricCard";
import PageHeader from "../components/PageHeader";
import EmptyState from "../components/EmptyState";
import { getJson } from "../api";

/* QA ISSUE-002: this page shipped as a stale "Coming in R2b" placeholder.
   The fleet summary API has existed since R2b — render it. */

interface FleetSummary {
  total_nodes: number;
  by_health: Record<string, number>;
  incidents_open: number;
  sites_count: number;
  tenant_id: string;
}

const metricsRow: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))",
  gap: "1rem",
  marginBottom: "1.5rem",
};

export default function Dashboard() {
  const { tenantId = "" } = useParams<{ tenantId: string }>();
  const [summary, setSummary] = useState<FleetSummary | null>(null);
  const [failed, setFailed] = useState(false);

  const fetchSummary = useCallback(async () => {
    try {
      setSummary(await getJson<FleetSummary>(`/api/t/${tenantId}/fleet/summary`));
      setFailed(false);
    } catch {
      setFailed(true);
    }
  }, []);

  useEffect(() => {
    fetchSummary();
    const timer = setInterval(fetchSummary, 30000);
    return () => clearInterval(timer);
  }, [fetchSummary]);

  const healthyPct =
    summary && summary.total_nodes
      ? Math.round(((summary.by_health?.ok ?? 0) / summary.total_nodes) * 100)
      : summary
        ? 100
        : null;

  return (
    <div>
      <PageHeader
        title="Dashboard"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Dashboard" }]}
      />
      <div style={metricsRow}>
        <MetricCard title="Total Nodes" value={summary?.total_nodes ?? "--"} />
        <MetricCard
          title="Healthy"
          value={healthyPct ?? "--"}
          unit="%"
          trend={
            healthyPct == null ? undefined : healthyPct >= 95 ? "up" : healthyPct < 80 ? "down" : "flat"
          }
        />
        <MetricCard
          title="Open Incidents"
          value={summary?.incidents_open ?? "--"}
          trend={summary && summary.incidents_open > 0 ? "down" : "flat"}
        />
        <MetricCard title="Sites" value={summary?.sites_count ?? "--"} />
      </div>
      {failed && (
        <div className="card">
          <EmptyState
            title="Fleet summary unavailable"
            description="Central Command did not answer. Check connectivity and refresh."
            icon="&#x25A6;"
          />
        </div>
      )}
      {summary && summary.total_nodes === 0 && (
        <div className="card">
          <EmptyState
            title="No devices yet"
            description="Metrics populate once agents are registered and reporting."
            icon="&#x25A6;"
          />
        </div>
      )}
    </div>
  );
}
