import { useParams } from "react-router-dom";
import { type CSSProperties, useCallback, useEffect, useState } from "react";
import PageHeader from "../components/PageHeader";
import DataTable, { type Column } from "../components/DataTable";
import DetailPanel from "../components/DetailPanel";
import MetricCard from "../components/MetricCard";
import StatusBadge from "../components/StatusBadge";
import ConfirmDialog from "../components/ConfirmDialog";
import EmptyState from "../components/EmptyState";
import Toast from "../components/Toast";
import { useToast } from "../components/useToast";
import { getJson, postJson } from "../api";
import { useAuth } from "../useAuth";

/* S5 — Autonomy: the governed decision boundary for action.
 *
 * This page is a CONSUMER of GET /api/autonomy, not its owner. The same
 * contract answers the same question for a future Operational Agent, so
 * nothing here computes a disposition, an evidence bar, or a safety
 * verdict in the browser — it renders what Central Command decided.
 *
 * Three things the page must never blur:
 *   1. Autonomy is not permission. A class that may run unattended still
 *      passes every execution gate on the node.
 *   2. Demotion is automatic; promotion is always a human act.
 *   3. A site that has not reported safety state is UNKNOWN, never safe. */

interface BlockingCondition {
  code: string;
  detail: string;
  scope: string;
  site_id?: string;
  domain_id?: string;
}

interface ActionClass {
  action_type: string;
  risk: string;
  granted_at_level: number | null;
  budget_mapped: boolean;
  never_budget_grantable: boolean;
  disposition: string;
  disposition_reason: string;
  blocking_conditions: BlockingCondition[];
  evidence: {
    executions: number;
    success: number;
    failure: number;
    success_rate: number | null;
    resolution_rate: number | null;
    sites_observed: number;
    sufficient: boolean;
  };
  learning: { signal_id: string; statement: string; confidence: number | null }[];
  safety: {
    reported: boolean;
    error_budget: {
      dropped_back: boolean;
      total: number;
      success: number;
      failure: number;
      success_rate: number | null;
      sites_dropped_back: string[];
    } | null;
    suppressed_domains: { domain_id: string; site_id?: string; trigger_reason?: string }[];
    site_budget_remaining: Record<string, number>;
  };
  approval: { required: boolean; mode: string; required_approvers: number };
  advancement: {
    next_level: number | null;
    gate: string;
    qualified_on_evidence: boolean;
    blocked_by: string[];
    statement: string;
  };
}

interface Contract {
  contract_version: string;
  generated_at: string;
  actor: { may_approve: boolean; may_change_posture: boolean };
  scope: {
    sites: { id: string; name: string; safety_reported: boolean; safety_as_of: string | null }[];
  };
  posture: {
    stop_switch: {
      active: boolean;
      changed_by: string;
      changed_at: string | null;
      sites_reporting_active: number;
    };
    configured_level: number;
    level_source: string;
    budget_limit: number;
    budget_period: string;
    actions_used: number;
    ladder: { level: number; name: string; grants: string[]; statement: string }[];
  };
  safety_state: {
    reported: boolean;
    sites_reporting: string[];
    sites_not_reporting: string[];
    suppressions: { domain_id: string; site_id?: string; trigger_reason?: string }[];
  };
  action_classes: ActionClass[];
}

const DISPOSITION_LABEL: Record<string, string> = {
  autonomous: "Runs unattended",
  requires_approval: "Requires approval",
  denied: "Denied",
  not_budget_mapped: "Not mapped",
};

const DISPOSITION_VARIANT: Record<
  string,
  "success" | "warning" | "critical" | "info" | "neutral"
> = {
  autonomous: "success",
  requires_approval: "warning",
  denied: "critical",
  not_budget_mapped: "neutral",
};

const RISK_VARIANT: Record<string, "success" | "warning" | "critical" | "neutral"> = {
  none: "neutral",
  low: "success",
  medium: "warning",
  high: "critical",
};

/* Plain language for the codes the contract returns. An operator should
   never have to read an enum to learn why something is blocked. */
const BLOCK_LABEL: Record<string, string> = {
  never_budget_grantable:
    "Never granted by an autonomy budget — this class has its own approval path",
  not_budget_mapped: "No autonomy level maps this class",
  stop_switch_active: "The stop switch is active",
  level_below_grant: "The tenant's autonomy level is below the level that grants this",
  error_budget_dropped_back: "Withdrawn automatically after repeated failures",
  budget_window_exhausted: "This window's budget is spent",
  domain_suppressed: "A correlated fault has suppressed this fault domain",
};

const metricsRow: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fit, minmax(190px, 1fr))",
  gap: "1rem",
  marginBottom: "1.5rem",
};

const helpText: CSSProperties = {
  fontSize: "0.8125rem",
  color: "var(--text-secondary)",
  marginBottom: "1rem",
  maxWidth: "72ch",
  lineHeight: 1.6,
};

const bannerBase: CSSProperties = {
  padding: "0.75rem 1rem",
  borderRadius: "var(--radius-md)",
  fontSize: "0.8125rem",
  marginBottom: "1rem",
  display: "flex",
  alignItems: "center",
  gap: "0.75rem",
  flexWrap: "wrap",
};

const ladderRow = (active: boolean): CSSProperties => ({
  display: "flex",
  gap: "1rem",
  alignItems: "flex-start",
  padding: "0.7rem 0.9rem",
  borderRadius: "var(--radius-md)",
  border: `1px solid ${active ? "var(--color-primary, #0E7A73)" : "var(--border-light)"}`,
  background: active ? "var(--status-info-bg)" : "var(--bg-card)",
  marginBottom: "0.5rem",
});

const ladderLevel: CSSProperties = {
  fontWeight: 700,
  fontSize: "0.75rem",
  minWidth: 96,
  textTransform: "uppercase",
  letterSpacing: "0.04em",
};

const sectionTitle: CSSProperties = {
  fontSize: "0.8125rem",
  fontWeight: 600,
  color: "var(--text-secondary)",
  textTransform: "uppercase",
  letterSpacing: "0.04em",
  marginBottom: "0.75rem",
  marginTop: "1.25rem",
  borderBottom: "1px solid var(--border-light)",
  paddingBottom: "0.375rem",
};

const detailRow: CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  gap: "1rem",
  padding: "0.375rem 0",
  fontSize: "0.8125rem",
  borderBottom: "1px solid var(--border-light)",
};
const detailLabel: CSSProperties = { color: "var(--text-secondary)", fontWeight: 500 };
const detailValue: CSSProperties = {
  color: "var(--text-primary)",
  fontWeight: 500,
  textAlign: "right",
};

const actionsRow: CSSProperties = {
  display: "flex",
  gap: "0.5rem",
  flexWrap: "wrap",
  marginBottom: "1.25rem",
};

const btn = (danger: boolean): CSSProperties => ({
  padding: "0.45rem 0.95rem",
  borderRadius: "var(--radius-md)",
  border: `1px solid ${danger ? "var(--status-critical)" : "var(--border-color)"}`,
  background: danger ? "var(--status-critical)" : "var(--bg-card)",
  color: danger ? "#fff" : "inherit",
  cursor: "pointer",
  font: "inherit",
  fontSize: "0.8125rem",
  fontWeight: 600,
});

function pct(v: number | null | undefined): string {
  return v == null ? "not enough data" : `${Math.round(v * 100)}%`;
}

function fmtDate(iso: string | null): string {
  if (!iso) return "--";
  return new Date(iso).toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export default function Autonomy() {
  const { tenantId = "" } = useParams<{ tenantId: string }>();
  const { toasts, toast, dismiss } = useToast();
  const { user } = useAuth();

  const [contract, setContract] = useState<Contract | null>(null);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<ActionClass | null>(null);
  const [confirmStop, setConfirmStop] = useState<null | "on" | "off">(null);
  const [busy, setBusy] = useState(false);

  const canChangePosture =
    user?.permissions?.includes("site.manage") || user?.permissions?.includes("*") || false;

  const fetchAll = useCallback(async () => {
    try {
      setContract(await getJson<Contract>(`/api/t/${tenantId}/autonomy/`));
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to load autonomy posture", "error");
    } finally {
      setLoading(false);
    }
  }, [tenantId, toast]);

  useEffect(() => {
    setLoading(true);
    void fetchAll();
  }, [fetchAll]);

  const flipStopSwitch = async (activate: boolean) => {
    setBusy(true);
    try {
      await postJson(
        `/api/t/${tenantId}/policies/stop-switch${activate ? "" : "/deactivate"}`,
        {},
      );
      toast(
        activate
          ? "Stop switch active — agents drop to observe-only on the next lease"
          : "Stop switch cleared",
        activate ? "info" : "success",
      );
      await fetchAll();
    } catch (err) {
      toast(err instanceof Error ? err.message : "Stop switch change failed", "error");
    } finally {
      setBusy(false);
      setConfirmStop(null);
    }
  };

  const columns: Column<ActionClass>[] = [
    {
      key: "action_type",
      header: "Action class",
      render: (r) => (
        <span style={{ fontSize: "0.8125rem", fontWeight: 600 }}>
          {r.action_type.replace(/_/g, " ")}
          <div style={{ fontSize: "0.72rem", color: "var(--text-muted)", fontWeight: 400 }}>
            {r.granted_at_level == null
              ? r.never_budget_grantable
                ? "never budget-granted"
                : "unmapped"
              : `granted at level ${r.granted_at_level}`}
          </div>
        </span>
      ),
    },
    {
      key: "risk",
      header: "Risk",
      render: (r) => (
        <StatusBadge status={r.risk} variant={RISK_VARIANT[r.risk] ?? "neutral"} size="sm" />
      ),
    },
    {
      key: "disposition",
      header: "Right now",
      render: (r) => (
        <StatusBadge
          status={DISPOSITION_LABEL[r.disposition] ?? r.disposition}
          variant={DISPOSITION_VARIANT[r.disposition] ?? "neutral"}
          size="sm"
        />
      ),
    },
    {
      key: "evidence",
      header: "Evidence",
      render: (r) =>
        r.evidence.executions === 0 ? (
          <span style={{ color: "var(--text-muted)", fontSize: "0.8125rem" }}>
            never run
          </span>
        ) : (
          <span style={{ fontSize: "0.8125rem" }}>
            {pct(r.evidence.success_rate)}
            <div style={{ fontSize: "0.72rem", color: "var(--text-muted)" }}>
              {r.evidence.executions} execution{r.evidence.executions === 1 ? "" : "s"}
            </div>
          </span>
        ),
    },
    {
      key: "safety",
      header: "Safety",
      render: (r) =>
        !r.safety.reported ? (
          <StatusBadge status="unknown" variant="neutral" size="sm" />
        ) : r.safety.error_budget?.dropped_back ? (
          <StatusBadge status="dropped back" variant="critical" size="sm" />
        ) : (
          <StatusBadge status="ok" variant="success" size="sm" />
        ),
    },
    {
      key: "advancement",
      header: "What advances it",
      render: (r) => (
        <span style={{ fontSize: "0.78rem", color: "var(--text-secondary)", lineHeight: 1.5 }}>
          {r.advancement.statement}
        </span>
      ),
    },
  ];

  const classes = contract?.action_classes ?? [];
  const autonomousCount = classes.filter((c) => c.disposition === "autonomous").length;
  const droppedBack = classes.filter((c) => c.safety.error_budget?.dropped_back).length;
  const stop = contract?.posture.stop_switch;
  const unreported = contract?.safety_state.sites_not_reporting ?? [];

  return (
    <div>
      <Toast toasts={toasts} onDismiss={dismiss} />
      <PageHeader
        title="Autonomy"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Governance" }, { label: "Autonomy" }]}
      />

      <p style={helpText}>
        What this system may do without a human, and what it would take to widen that.
        Autonomy is not permission: a class that runs unattended still passes every
        execution gate on the device — preconditions, lease, blast radius, stop switch.
        Autonomy is withdrawn automatically when evidence turns bad; it is only ever
        widened by a person.
      </p>

      {stop?.active && (
        <div
          style={{
            ...bannerBase,
            background: "var(--status-critical-bg)",
            color: "var(--status-critical)",
            border: "1px solid var(--status-critical)",
          }}
        >
          <strong>Stop switch active.</strong>
          <span>
            Every autonomous action is denied fleet-wide. Agents drop to observe-only as
            their leases renew. Activated by {stop.changed_by || "an operator"}
            {stop.changed_at ? ` at ${fmtDate(stop.changed_at)}` : ""}.
          </span>
        </div>
      )}

      {unreported.length > 0 && (
        <div
          style={{
            ...bannerBase,
            background: "var(--status-neutral-bg)",
            color: "var(--text-secondary)",
            border: "1px solid var(--border-color)",
          }}
        >
          <strong>Safety state unknown for {unreported.length} site
            {unreported.length === 1 ? "" : "s"}.</strong>
          <span>
            Those sites have not reported suppression or error-budget state. Treat their
            safety as unknown, not as clear.
          </span>
        </div>
      )}

      <div style={metricsRow}>
        <MetricCard
          title="Autonomy level"
          value={
            contract?.posture.level_source === "unconfigured"
              ? "not set"
              : (contract?.posture.configured_level ?? "--")
          }
        />
        <MetricCard title="Classes running unattended" value={autonomousCount} />
        <MetricCard title="Withdrawn on evidence" value={droppedBack} />
        <MetricCard
          title="Budget"
          value={
            contract == null || contract.posture.budget_limit === 0
              ? "none"
              : `${contract.posture.actions_used}/${contract.posture.budget_limit}`
          }
          unit={contract?.posture.budget_period || undefined}
        />
      </div>

      {canChangePosture && (
        <div style={actionsRow}>
          {stop?.active ? (
            <button style={btn(false)} disabled={busy} onClick={() => setConfirmStop("off")}>
              Clear stop switch
            </button>
          ) : (
            <button style={btn(true)} disabled={busy} onClick={() => setConfirmStop("on")}>
              Activate stop switch
            </button>
          )}
        </div>
      )}

      <h2 style={sectionTitle}>The trust ladder</h2>
      <p style={helpText}>
        Each level is earned per action class, on real outcomes. A level is raised only by
        a person; it drops on its own when the evidence says it should.
      </p>
      {(contract?.posture.ladder ?? []).map((lvl) => (
        <div key={lvl.level} style={ladderRow(lvl.level === contract?.posture.configured_level)}>
          <span style={ladderLevel}>
            {lvl.level} · {lvl.name}
          </span>
          <span style={{ fontSize: "0.8125rem", lineHeight: 1.55 }}>
            {lvl.statement}
            {lvl.grants.length > 0 && (
              <div style={{ fontSize: "0.72rem", color: "var(--text-muted)", marginTop: 2 }}>
                {lvl.grants.map((g) => g.replace(/_/g, " ")).join(" · ")}
              </div>
            )}
          </span>
        </div>
      ))}

      <h2 style={sectionTitle}>Action classes</h2>
      {classes.length === 0 && !loading ? (
        <EmptyState
          title="No action classes"
          description="The autonomy contract returned no classes for this tenant."
          icon="&#x2696;"
        />
      ) : (
        <DataTable<ActionClass>
          columns={columns}
          data={classes}
          loading={loading}
          emptyMessage="No action classes"
          onRowClick={(r) => setSelected(r)}
          striped
        />
      )}

      <DetailPanel
        open={selected !== null}
        onClose={() => setSelected(null)}
        title={selected?.action_type.replace(/_/g, " ") ?? ""}
        subtitle={
          selected ? DISPOSITION_LABEL[selected.disposition] ?? selected.disposition : ""
        }
      >
        {selected && (
          <div>
            <div style={sectionTitle}>Why</div>
            <p style={{ fontSize: "0.8125rem", lineHeight: 1.6 }}>
              {selected.disposition_reason}
            </p>

            {selected.blocking_conditions.length > 0 && (
              <>
                <div style={sectionTitle}>What is blocking it</div>
                {selected.blocking_conditions.map((b, i) => (
                  <div key={`${b.code}-${i}`} style={detailRow}>
                    <span style={detailLabel}>{BLOCK_LABEL[b.code] ?? b.code}</span>
                    <span style={detailValue}>
                      {b.scope}
                      {b.site_id ? ` · ${b.site_id}` : ""}
                      {b.domain_id ? ` · ${b.domain_id}` : ""}
                    </span>
                  </div>
                ))}
              </>
            )}

            <div style={sectionTitle}>Evidence</div>
            <div style={detailRow}>
              <span style={detailLabel}>Executions</span>
              <span style={detailValue}>{selected.evidence.executions}</span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Success rate</span>
              <span style={detailValue}>{pct(selected.evidence.success_rate)}</span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Fault resolved</span>
              <span style={detailValue}>{pct(selected.evidence.resolution_rate)}</span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Sites observed</span>
              <span style={detailValue}>{selected.evidence.sites_observed}</span>
            </div>

            <div style={sectionTitle}>Safety state</div>
            {!selected.safety.reported ? (
              <p style={{ fontSize: "0.8125rem", color: "var(--text-secondary)" }}>
                No site has reported safety state. Unknown, not clear.
              </p>
            ) : (
              <>
                <div style={detailRow}>
                  <span style={detailLabel}>Error budget</span>
                  <span style={detailValue}>
                    {selected.safety.error_budget
                      ? selected.safety.error_budget.dropped_back
                        ? `dropped back (${pct(selected.safety.error_budget.success_rate)})`
                        : `ok (${pct(selected.safety.error_budget.success_rate)})`
                      : "no outcomes yet"}
                  </span>
                </div>
                <div style={detailRow}>
                  <span style={detailLabel}>Suppressed fault domains</span>
                  <span style={detailValue}>
                    {selected.safety.suppressed_domains.length || "none"}
                  </span>
                </div>
                {Object.entries(selected.safety.site_budget_remaining).map(([site, rem]) => (
                  <div key={site} style={detailRow}>
                    <span style={detailLabel}>Budget remaining · {site}</span>
                    <span style={detailValue}>{rem === -1 ? "unlimited" : rem}</span>
                  </div>
                ))}
              </>
            )}

            <div style={sectionTitle}>Approval</div>
            <div style={detailRow}>
              <span style={detailLabel}>Required</span>
              <span style={detailValue}>{selected.approval.required ? "yes" : "no"}</span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Approvers needed</span>
              <span style={detailValue}>{selected.approval.required_approvers}</span>
            </div>

            <div style={sectionTitle}>What advances it</div>
            <p style={{ fontSize: "0.8125rem", lineHeight: 1.6 }}>
              {selected.advancement.statement}
            </p>

            {selected.learning.length > 0 && (
              <>
                <div style={sectionTitle}>What the fleet has learned</div>
                {selected.learning.map((l) => (
                  <p
                    key={l.signal_id}
                    style={{ fontSize: "0.8125rem", lineHeight: 1.6, marginBottom: "0.5rem" }}
                  >
                    {l.statement}
                    {l.confidence != null && (
                      <span style={{ color: "var(--text-muted)" }}>
                        {" "}
                        ({Math.round(l.confidence * 100)}% confidence)
                      </span>
                    )}
                  </p>
                ))}
              </>
            )}
          </div>
        )}
      </DetailPanel>

      <ConfirmDialog
        open={confirmStop !== null}
        title={confirmStop === "on" ? "Activate the stop switch?" : "Clear the stop switch?"}
        message={
          confirmStop === "on"
            ? "Every autonomous action across the fleet is denied until this is cleared. Agents drop to observe-only as their leases renew. Diagnosis continues."
            : "Autonomous action resumes at the tenant's configured level. Classes withdrawn on evidence stay withdrawn."
        }
        variant={confirmStop === "on" ? "danger" : "default"}
        confirmLabel={confirmStop === "on" ? "Activate" : "Clear"}
        loading={busy}
        onConfirm={() => void flipStopSwitch(confirmStop === "on")}
        onCancel={() => setConfirmStop(null)}
      />
    </div>
  );
}
