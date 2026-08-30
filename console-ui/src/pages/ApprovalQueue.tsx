import { useParams } from "react-router-dom";
import { type CSSProperties, useCallback, useEffect, useMemo, useState } from "react";
import PageHeader from "../components/PageHeader";
import FilterBar, { type FilterDef } from "../components/FilterBar";
import DataTable, { type Column } from "../components/DataTable";
import StatusBadge from "../components/StatusBadge";
import EmptyState from "../components/EmptyState";
import Toast from "../components/Toast";
import Spinner from "../components/Spinner";
import ConfirmDialog from "../components/ConfirmDialog";
import { useToast } from "../components/useToast";
import { getJson, postJson } from "../api";
import type { ApprovalAction } from "../types";

/* ── Constants ────────────────────────────────────── */

const PAGE_SIZE = 20;
const POLL_INTERVAL = 30000;

const DECISION_VARIANT: Record<string, "success" | "warning" | "critical" | "info" | "neutral"> = {
  approved: "success",
  denied: "critical",
  expired: "neutral",
  pending: "info",
};

/* ── Styles ───────────────────────────────────────── */

const tabBarStyle: CSSProperties = {
  display: "flex",
  gap: "0",
  borderBottom: "1px solid var(--border-color)",
  marginBottom: "1.25rem",
};

const tabStyle: CSSProperties = {
  padding: "0.625rem 1.25rem",
  fontSize: "0.875rem",
  fontWeight: 500,
  color: "var(--text-secondary)",
  background: "transparent",
  border: "none",
  borderBottom: "2px solid transparent",
  cursor: "pointer",
  fontFamily: "inherit",
  transition: "color var(--transition), border-color var(--transition)",
};

const tabActiveStyle: CSSProperties = {
  ...tabStyle,
  color: "var(--accent)",
  borderBottomColor: "var(--accent)",
  fontWeight: 600,
};

const cardsGrid: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fill, minmax(340px, 1fr))",
  gap: "0.75rem",
};

const cardStyle: CSSProperties = {
  background: "var(--bg-card)",
  border: "1px solid var(--border-color)",
  borderRadius: "var(--radius-lg)",
  padding: "1rem 1.25rem",
  boxShadow: "var(--shadow-sm)",
  display: "flex",
  flexDirection: "column",
  gap: "0.625rem",
  transition: "box-shadow var(--transition)",
};

const cardHeaderRow: CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  gap: "0.5rem",
};

const agentBlockStyle: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: "0.375rem",
  padding: "0.625rem 0.75rem",
  borderRadius: "var(--radius-md, 6px)",
  background: "var(--bg-subtle, rgba(127,127,127,0.08))",
  borderLeft: "3px solid var(--accent)",
};

const cardDetailRow: CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  fontSize: "0.8125rem",
  color: "var(--text-secondary)",
};

const cardActions: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: "0.5rem",
  marginTop: "0.25rem",
};

const batchBarStyle: CSSProperties = {
  position: "fixed",
  bottom: 0,
  left: "var(--sidebar-width)",
  right: 0,
  background: "var(--bg-card)",
  borderTop: "1px solid var(--border-color)",
  padding: "0.75rem 1.5rem",
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  zIndex: 500,
  boxShadow: "0 -2px 8px rgba(0,0,0,0.08)",
};

const checkboxStyle: CSSProperties = {
  width: 18,
  height: 18,
  cursor: "pointer",
  accentColor: "var(--accent)",
};

/* ── Helpers ──────────────────────────────────────── */

function formatDate(iso: string): string {
  if (!iso) return "--";
  return new Date(iso).toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/* ── Component ────────────────────────────────────── */

export default function ApprovalQueue() {
  const { tenantId = "" } = useParams<{ tenantId: string }>();
  const { toasts, toast, dismiss } = useToast();

  /* ── Tab state ──────────────────────────────────── */
  const [activeTab, setActiveTab] = useState<"pending" | "history">("pending");

  /* ── Pending state ─────────────────────────────── */
  const [pendingActions, setPendingActions] = useState<ApprovalAction[]>([]);
  const [pendingLoading, setPendingLoading] = useState(true);
  const [pendingFilters, setPendingFilters] = useState<Record<string, string>>({
    site_id: "",
    type: "",
    severity: "",
  });
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [batchLoading, setBatchLoading] = useState(false);

  /* ── Batch confirm dialog ──────────────────────── */
  const [batchConfirm, setBatchConfirm] = useState<{ action: "approve" | "deny" } | null>(null);

  /* ── History state ─────────────────────────────── */
  const [historyActions, setHistoryActions] = useState<ApprovalAction[]>([]);
  const [historyTotal, setHistoryTotal] = useState(0);
  const [historyPage, setHistoryPage] = useState(1);
  const [historyLoading, setHistoryLoading] = useState(true);
  const [historyFilters, setHistoryFilters] = useState<Record<string, string>>({
    site_id: "",
    decided_by: "",
    date_from: "",
    date_to: "",
  });

  /* ── Pending filters ───────────────────────────── */
  const pendingFilterDefs = useMemo<FilterDef[]>(() => [
    {
      key: "site_id",
      label: "Site",
      type: "select",
      options: [],
    },
    {
      key: "type",
      label: "Action Type",
      type: "select",
      options: [
        { value: "IDENTIFY_LED", label: "Identify LED" },
        { value: "COLLECT_DIAGNOSTICS", label: "Collect Diagnostics" },
        { value: "RESTART_BMC", label: "Restart BMC" },
        { value: "POWER_CYCLE", label: "Power Cycle" },
        { value: "FIRMWARE_UPDATE", label: "Firmware Update" },
      ],
    },
    {
      key: "severity",
      label: "Severity",
      type: "select",
      options: [
        { value: "info", label: "Info" },
        { value: "warning", label: "Warning" },
        { value: "critical", label: "Critical" },
      ],
    },
  ], []);

  /* ── History filters ───────────────────────────── */
  const historyFilterDefs = useMemo<FilterDef[]>(() => [
    {
      key: "site_id",
      label: "Site",
      type: "select",
      options: [],
    },
    {
      key: "decided_by",
      label: "Decided By",
      type: "text",
      placeholder: "Search by approver...",
    },
    { key: "date_from", label: "From", type: "dateRange" },
    { key: "date_to", label: "To", type: "dateRange" },
  ], []);

  /* ── Fetch pending ─────────────────────────────── */
  const fetchPending = useCallback(async () => {
    try {
      const params = new URLSearchParams();
      params.set("status", "pending");
      if (pendingFilters.site_id) params.set("site_id", pendingFilters.site_id);
      if (pendingFilters.type) params.set("type", pendingFilters.type);
      if (pendingFilters.severity) params.set("severity", pendingFilters.severity);
      params.set("page", "1");
      params.set("page_size", "100");
      // QA ISSUE-008: CC returns {actions,...}, not PaginatedResponse
      // {items,...} — res.items was undefined and .length crashed the
      // page to a white screen.
      const res = await getJson<{ actions: ApprovalAction[]; total: number }>(
        `/api/t/${tenantId}/approvals?${params.toString()}`,
      );
      setPendingActions(res.actions ?? []);
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to load pending approvals", "error");
    } finally {
      setPendingLoading(false);
    }
  }, [pendingFilters, toast]);

  /* ── Fetch history ─────────────────────────────── */
  const fetchHistory = useCallback(async () => {
    try {
      const params = new URLSearchParams();
      if (historyFilters.site_id) params.set("site_id", historyFilters.site_id);
      if (historyFilters.decided_by) params.set("decided_by", historyFilters.decided_by);
      if (historyFilters.date_from) params.set("date_from", historyFilters.date_from);
      if (historyFilters.date_to) params.set("date_to", historyFilters.date_to);
      params.set("page", String(historyPage));
      params.set("page_size", String(PAGE_SIZE));
      const res = await getJson<{ actions: ApprovalAction[]; total: number }>(
        `/api/t/${tenantId}/approvals/history?${params.toString()}`,
      );
      setHistoryActions(res.actions ?? []);
      setHistoryTotal(res.total ?? 0);
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to load approval history", "error");
    } finally {
      setHistoryLoading(false);
    }
  }, [historyFilters, historyPage, toast]);

  /* ── Load data on mount / tab change ───────────── */
  useEffect(() => {
    if (activeTab === "pending") {
      setPendingLoading(true);
      void fetchPending();
    } else {
      setHistoryLoading(true);
      void fetchHistory();
    }
  }, [activeTab, fetchPending, fetchHistory]);

  /* ── Polling (pending only) ────────────────────── */
  useEffect(() => {
    if (activeTab !== "pending") return;
    const timer = setInterval(() => void fetchPending(), POLL_INTERVAL);
    return () => clearInterval(timer);
  }, [activeTab, fetchPending]);

  /* ── Approve / Deny single ─────────────────────── */
  const handleDecision = useCallback(
    async (id: string, decision: "approve" | "deny") => {
      setActionLoading(id);
      try {
        await postJson(`/api/t/${tenantId}/approvals/${id}/${decision}`, {});
        toast(
          decision === "approve" ? "Action approved" : "Action denied",
          decision === "approve" ? "success" : "info",
        );
        setSelected((prev) => {
          const next = new Set(prev);
          next.delete(id);
          return next;
        });
        void fetchPending();
      } catch (err) {
        toast(err instanceof Error ? err.message : `Failed to ${decision} action`, "error");
      } finally {
        setActionLoading(null);
      }
    },
    [toast, fetchPending],
  );

  /* ── Batch action ──────────────────────────────── */
  const handleBatch = useCallback(
    async (decision: "approve" | "deny") => {
      setBatchLoading(true);
      try {
        // CC's contract is {action_ids, decision: approved|denied}. The
        // previous body ({ids, decision: "approve"}) was rejected on
        // every call, so batch decisions never landed.
        await postJson(`/api/t/${tenantId}/approvals/batch`, {
          action_ids: Array.from(selected),
          decision: decision === "approve" ? "approved" : "denied",
        });
        toast(
          `${selected.size} action(s) ${decision === "approve" ? "approved" : "denied"}`,
          decision === "approve" ? "success" : "info",
        );
        setSelected(new Set());
        setBatchConfirm(null);
        void fetchPending();
      } catch (err) {
        toast(err instanceof Error ? err.message : `Batch ${decision} failed`, "error");
      } finally {
        setBatchLoading(false);
      }
    },
    [selected, toast, fetchPending],
  );

  /* ── Selection helpers ─────────────────────────── */
  const toggleSelection = useCallback((id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const toggleSelectAll = useCallback(() => {
    if (selected.size === pendingActions.length) {
      setSelected(new Set());
    } else {
      setSelected(new Set(pendingActions.map((a) => a.id)));
    }
  }, [selected.size, pendingActions]);

  /* ── History table columns ─────────────────────── */
  const historyColumns = useMemo<Column<ApprovalAction>[]>(
    () => [
      {
        key: "action_type",
        header: "Action Type",
        render: (r) => (
          <StatusBadge status={r.action_type} variant="info" size="sm" />
        ),
      },
      {
        key: "device_agent_id",
        header: "Device",
        render: (r) => (
          <code style={{ fontSize: "0.8125rem", fontFamily: "var(--font-mono, monospace)" }}>
            {r.device_agent_id || "--"}
          </code>
        ),
      },
      {
        key: "origin",
        header: "Requested by",
        render: (r) => (
          <StatusBadge
            status={r.origin === "agent" ? "agent" : "node"}
            variant={r.origin === "agent" ? "warning" : "neutral"}
            size="sm"
          />
        ),
      },
      {
        key: "decision",
        header: "Decision",
        render: (r) => (
          <StatusBadge
            status={r.decision ?? "pending"}
            variant={DECISION_VARIANT[r.decision ?? "pending"] ?? "neutral"}
            size="sm"
          />
        ),
      },
      {
        key: "decided_by",
        header: "Decided By",
        render: (r) => r.decided_by || "--",
      },
      {
        key: "decided_at",
        header: "Decided At",
        render: (r) => formatDate(r.decided_at ?? ""),
      },
      {
        key: "delivered_at",
        header: "Delivered",
        render: (r) => (r.delivered_at ? formatDate(r.delivered_at) : "--"),
      },
    ],
    [],
  );

  /* ── Filter handlers ───────────────────────────── */
  const handlePendingFilterChange = useCallback((key: string, value: string) => {
    setPendingFilters((prev) => ({ ...prev, [key]: value }));
  }, []);

  const handlePendingFilterClear = useCallback(() => {
    setPendingFilters({ site_id: "", type: "", severity: "" });
  }, []);

  const handleHistoryFilterChange = useCallback((key: string, value: string) => {
    setHistoryFilters((prev) => ({ ...prev, [key]: value }));
    setHistoryPage(1);
  }, []);

  const handleHistoryFilterClear = useCallback(() => {
    setHistoryFilters({ site_id: "", decided_by: "", date_from: "", date_to: "" });
    setHistoryPage(1);
  }, []);

  /* ── Render ─────────────────────────────────────── */
  return (
    <div>
      <Toast toasts={toasts} onDismiss={dismiss} />

      <PageHeader
        title="Approval Queue"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Fleet" }, { label: "Approvals" }]}
      />

      {/* Tabs */}
      <div style={tabBarStyle}>
        <button
          style={activeTab === "pending" ? tabActiveStyle : tabStyle}
          onClick={() => setActiveTab("pending")}
        >
          Pending
          {pendingActions.length > 0 && (
            <span style={{
              marginLeft: "0.5rem",
              background: "var(--accent)",
              color: "#fff",
              fontSize: "0.6875rem",
              fontWeight: 700,
              padding: "0.0625rem 0.4375rem",
              borderRadius: "999px",
            }}>
              {pendingActions.length}
            </span>
          )}
        </button>
        <button
          style={activeTab === "history" ? tabActiveStyle : tabStyle}
          onClick={() => setActiveTab("history")}
        >
          History
        </button>
      </div>

      {/* ── Pending Tab ──────────────────────────── */}
      {activeTab === "pending" && (
        <>
          <FilterBar
            filters={pendingFilterDefs}
            values={pendingFilters}
            onChange={handlePendingFilterChange}
            onClear={handlePendingFilterClear}
          />

          {pendingLoading ? (
            <div style={{ display: "flex", justifyContent: "center", padding: "3rem" }}>
              <Spinner size="md" />
            </div>
          ) : pendingActions.length === 0 ? (
            <EmptyState
              title="No pending approvals"
              description="All actions have been reviewed. New requests will appear here automatically."
              icon="&#x2714;"
            />
          ) : (
            <>
              {/* Select all */}
              <div style={{
                display: "flex",
                alignItems: "center",
                gap: "0.5rem",
                marginBottom: "0.75rem",
                fontSize: "0.8125rem",
                color: "var(--text-secondary)",
              }}>
                <input
                  type="checkbox"
                  style={checkboxStyle}
                  checked={selected.size === pendingActions.length && pendingActions.length > 0}
                  onChange={toggleSelectAll}
                  aria-label="Select all"
                />
                <span>Select all ({pendingActions.length})</span>
              </div>

              <div style={cardsGrid}>
                {pendingActions.map((action) => (
                  <div key={action.action_id} style={cardStyle}>
                    <div style={cardHeaderRow}>
                      <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
                        <input
                          type="checkbox"
                          style={checkboxStyle}
                          checked={selected.has(action.action_id)}
                          onChange={() => toggleSelection(action.action_id)}
                          aria-label={`Select ${action.action_type}`}
                        />
                        <StatusBadge status={action.action_type} variant="info" size="sm" />
                      </div>
                      <StatusBadge
                        status={action.origin === "agent" ? "agent" : "node"}
                        variant={action.origin === "agent" ? "warning" : "neutral"}
                        size="sm"
                      />
                    </div>

                    <div style={cardDetailRow}>
                      <span>Device</span>
                      <code style={{ fontSize: "0.8125rem", fontFamily: "var(--font-mono, monospace)" }}>
                        {action.device_agent_id || "--"}
                      </code>
                    </div>
                    <div style={cardDetailRow}>
                      <span>Requested by</span>
                      <span style={{ fontWeight: 500, color: "var(--text-primary)" }}>
                        {action.proposal ? action.proposal.actor : "this device"}
                      </span>
                    </div>
                    <div style={cardDetailRow}>
                      <span>Waiting since</span>
                      <span>{formatDate(action.routed_at ?? "")}</span>
                    </div>

                    {/* A1: an agent has to say what it saw and why. A
                        request with no rationale is not reviewable, so
                        the card carries the agent's own reasoning, its
                        evidence and anything blocking it. */}
                    {action.proposal ? (
                      <div style={agentBlockStyle}>
                        <div style={{ fontSize: "0.8125rem", color: "var(--text-primary)" }}>
                          {action.proposal.rationale}
                        </div>
                        {action.proposal.evidence?.incident_ids?.length ? (
                          <div style={cardDetailRow}>
                            <span>Incident</span>
                            <code style={{ fontSize: "0.75rem" }}>
                              {action.proposal.evidence.incident_ids.join(", ")}
                            </code>
                          </div>
                        ) : null}
                        {action.proposal.evidence?.outcome_evidence?.sufficient ? (
                          <div style={cardDetailRow}>
                            <span>Track record</span>
                            <span>
                              {Math.round(
                                (action.proposal.evidence.outcome_evidence.success_rate ?? 0) * 100,
                              )}
                              % over {action.proposal.evidence.outcome_evidence.executions} runs
                            </span>
                          </div>
                        ) : (
                          <div style={cardDetailRow}>
                            <span>Track record</span>
                            <span>too few outcomes to judge</span>
                          </div>
                        )}
                        {action.proposal.blocking_conditions.length > 0 ? (
                          <div style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>
                            {action.proposal.blocking_conditions[0].detail}
                          </div>
                        ) : null}
                      </div>
                    ) : null}

                    <div style={cardActions}>
                      <button
                        className="btn btn-primary"
                        style={{ flex: 1, fontSize: "0.8125rem" }}
                        onClick={() => handleDecision(action.action_id, "approve")}
                        disabled={actionLoading === action.action_id}
                      >
                        {actionLoading === action.action_id ? <Spinner size="sm" /> : null}
                        Approve
                      </button>
                      <button
                        className="btn btn-danger"
                        style={{ flex: 1, fontSize: "0.8125rem" }}
                        onClick={() => handleDecision(action.action_id, "deny")}
                        disabled={actionLoading === action.action_id}
                      >
                        {actionLoading === action.action_id ? <Spinner size="sm" /> : null}
                        Deny
                      </button>
                    </div>
                  </div>
                ))}
              </div>

              {/* Batch action bar */}
              {selected.size > 0 && (
                <div style={batchBarStyle}>
                  <span style={{ fontSize: "0.875rem", fontWeight: 600 }}>
                    {selected.size} selected
                  </span>
                  <div style={{ display: "flex", gap: "0.5rem" }}>
                    <button
                      className="btn btn-primary"
                      onClick={() => setBatchConfirm({ action: "approve" })}
                      disabled={batchLoading}
                    >
                      Batch Approve
                    </button>
                    <button
                      className="btn btn-danger"
                      onClick={() => setBatchConfirm({ action: "deny" })}
                      disabled={batchLoading}
                    >
                      Batch Deny
                    </button>
                  </div>
                </div>
              )}

              {/* Batch confirm dialog */}
              <ConfirmDialog
                open={batchConfirm !== null}
                title={batchConfirm?.action === "approve" ? "Batch Approve" : "Batch Deny"}
                message={`${batchConfirm?.action === "approve" ? "Approve" : "Deny"} ${selected.size} selected action(s)? This cannot be undone.`}
                confirmLabel={batchConfirm?.action === "approve" ? "Approve All" : "Deny All"}
                variant={batchConfirm?.action === "deny" ? "danger" : "default"}
                onConfirm={() => {
                  if (batchConfirm) void handleBatch(batchConfirm.action);
                }}
                onCancel={() => setBatchConfirm(null)}
                loading={batchLoading}
              />
            </>
          )}

          {/* spacer for batch bar */}
          {selected.size > 0 && <div style={{ height: "3.5rem" }} />}
        </>
      )}

      {/* ── History Tab ──────────────────────────── */}
      {activeTab === "history" && (
        <>
          <FilterBar
            filters={historyFilterDefs}
            values={historyFilters}
            onChange={handleHistoryFilterChange}
            onClear={handleHistoryFilterClear}
          />

          <DataTable<ApprovalAction>
            columns={historyColumns}
            data={historyActions}
            loading={historyLoading}
            emptyMessage="No approval history matches your filters"
            page={historyPage}
            pageSize={PAGE_SIZE}
            total={historyTotal}
            onPageChange={setHistoryPage}
            striped
          />
        </>
      )}
    </div>
  );
}
