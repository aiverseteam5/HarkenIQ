import { Link, useParams } from "react-router-dom";
import { useCallback, useEffect, useState } from "react";
import type { CSSProperties } from "react";
import MetricCard from "../components/MetricCard";
import PageHeader from "../components/PageHeader";
import EmptyState from "../components/EmptyState";
import StatusBadge from "../components/StatusBadge";
import { getJson } from "../api";
import { useAuth } from "../useAuth";
import { can } from "../permissions";

/* QA ISSUE-002: this page shipped as a stale "Coming in R2b" placeholder.
   The fleet summary API has existed since R2b — render it.

   S1 2026-08-29: the Overview now answers "is my fleet okay and is anything
   waiting on me" — pending approvals (for holders of action.approve), the
   stop-switch state, and the autonomy posture, all from endpoints that
   already existed but had no consumer (p1-agentic-product.md §3). */

interface FleetSummary {
  total_nodes: number;
  by_health: Record<string, number>;
  incidents_open: number;
  sites_count: number;
  tenant_id: string;
}

interface AutonomyBudget {
  device_type: string;
  level: number;
}

const AUTONOMY_LABELS: Record<number, string> = {
  0: "Observe only",
  1: "Recommend",
  2: "Supervised (low-risk)",
  3: "Autonomous (in budget)",
};

const metricsRow: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))",
  gap: "1rem",
  marginBottom: "1.5rem",
};

const governRow: CSSProperties = {
  display: "flex",
  flexWrap: "wrap",
  gap: "0.75rem",
  alignItems: "center",
  marginBottom: "1.5rem",
  padding: "0.75rem 1rem",
  background: "var(--bg-card)",
  border: "1px solid var(--border-light)",
  borderRadius: "var(--radius-md)",
  fontSize: "0.8125rem",
};

const governLabel: CSSProperties = {
  color: "var(--text-secondary)",
  fontWeight: 600,
  textTransform: "uppercase",
  fontSize: "0.6875rem",
  letterSpacing: "0.04em",
};

export default function Dashboard() {
  const { tenantId = "" } = useParams<{ tenantId: string }>();
  const { user } = useAuth();
  const [summary, setSummary] = useState<FleetSummary | null>(null);
  const [failed, setFailed] = useState(false);
  const [pendingApprovals, setPendingApprovals] = useState<number | null>(null);
  const [stopSwitch, setStopSwitch] = useState<boolean | null>(null);
  const [autonomyLevel, setAutonomyLevel] = useState<number | null>(null);

  const canApprove = can(user, "action.approve");

  const fetchSummary = useCallback(async () => {
    try {
      setSummary(await getJson<FleetSummary>(`/api/t/${tenantId}/fleet/summary`));
      setFailed(false);
    } catch {
      setFailed(true);
    }
    // Governance chips — each degrades to "--" independently, never
    // blocking the page. Reads are fleet.view (D2 split), except the
    // approvals count which needs action.approve and is skipped without it.
    if (canApprove) {
      try {
        const res = await getJson<{ total: number }>(
          `/api/t/${tenantId}/approvals?page_size=1`,
        );
        setPendingApprovals(res.total ?? 0);
      } catch {
        setPendingApprovals(null);
      }
    }
    try {
      const res = await getJson<{ stop_switch: boolean }>(
        `/api/t/${tenantId}/policies/stop-switch`,
      );
      setStopSwitch(res.stop_switch);
    } catch {
      setStopSwitch(null);
    }
    try {
      const res = await getJson<{ budgets: AutonomyBudget[] }>(
        `/api/t/${tenantId}/policies/autonomy`,
      );
      const wildcard = res.budgets.find((b) => b.device_type === "*");
      setAutonomyLevel(wildcard ? wildcard.level : 0);
    } catch {
      setAutonomyLevel(null);
    }
  }, [tenantId, canApprove]);

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
        title="Overview"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Overview" }]}
      />

      {/* Governance strip: what may the system do right now, and is
          anything waiting on a human. */}
      <div style={governRow}>
        <span style={governLabel}>Stop switch</span>
        {stopSwitch === null ? (
          <span>--</span>
        ) : (
          <StatusBadge
            status={stopSwitch ? "ACTIVE — autonomy halted" : "inactive"}
            variant={stopSwitch ? "critical" : "success"}
            size="sm"
          />
        )}
        <span style={governLabel}>Autonomy</span>
        {autonomyLevel === null ? (
          <span>--</span>
        ) : (
          // S5: the strip states the level; the Autonomy surface states
          // what that level means per action class, on what evidence.
          <Link to={`/t/${tenantId}/autonomy`} style={{ textDecoration: "none" }}>
            <StatusBadge
              status={AUTONOMY_LABELS[autonomyLevel] ?? `level ${autonomyLevel}`}
              variant={autonomyLevel >= 2 ? "info" : "neutral"}
              size="sm"
            />
          </Link>
        )}
        {canApprove && (
          <>
            <span style={governLabel}>Awaiting approval</span>
            {pendingApprovals === null ? (
              <span>--</span>
            ) : (
              <Link to={`/t/${tenantId}/approvals`} style={{ textDecoration: "none" }}>
                <StatusBadge
                  status={`${pendingApprovals} pending`}
                  variant={pendingApprovals > 0 ? "warning" : "neutral"}
                  size="sm"
                />
              </Link>
            )}
          </>
        )}
      </div>

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
