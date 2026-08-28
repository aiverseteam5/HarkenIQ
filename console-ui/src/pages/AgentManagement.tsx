import { type CSSProperties, useCallback, useEffect, useMemo, useState } from "react";
import PageHeader from "../components/PageHeader";
import FilterBar, { type FilterDef } from "../components/FilterBar";
import DataTable, { type Column } from "../components/DataTable";
import DetailPanel from "../components/DetailPanel";
import ConfirmDialog from "../components/ConfirmDialog";
import StatusBadge from "../components/StatusBadge";
import EmptyState from "../components/EmptyState";
import Toast from "../components/Toast";
import Spinner from "../components/Spinner";
import { useToast } from "../components/useToast";
import { getJson, postJson } from "../api";

/* ── Types ────────────────────────────────────────── */

/* Mirrors CC's /api/agents payload (see cc api/agents.py:_agent_dict).
   The previous shape was invented: `version`, `status`, `device`,
   `enabled` and `last_heartbeat_at` are fields CC has never sent, so
   those columns rendered empty on every row. */
interface Agent {
  agent_id: string;
  agent_name: string;
  vendor: string;
  model: string;
  device_class: string;
  observation: string;
  health: "ok" | "warning" | "critical" | "unknown" | string;
  site_id: string;
  site_name: string;
  last_seen_at: string | null;
  snapshot_at: string | null;
}

/* ── Constants ────────────────────────────────────── */

const PAGE_SIZE = 20;
const POLL_INTERVAL = 30000;

const HEALTH_VARIANT: Record<string, "success" | "warning" | "critical" | "neutral"> = {
  ok: "success",
  warning: "warning",
  critical: "critical",
  unknown: "neutral",
};

/** vendor + model, the operator-facing device name CC sends in parts. */
function deviceLabel(a: Agent): string {
  return [a.vendor, a.model].filter(Boolean).join(" ") || "—";
}

/* ── Styles ───────────────────────────────────────── */

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
  padding: "0.375rem 0",
  fontSize: "0.8125rem",
  borderBottom: "1px solid var(--border-light)",
};

const detailLabel: CSSProperties = {
  color: "var(--text-secondary)",
  fontWeight: 500,
};

const detailValue: CSSProperties = {
  color: "var(--text-primary)",
  fontWeight: 500,
  textAlign: "right",
};

const noteStyle: CSSProperties = {
  marginTop: "1.25rem",
  padding: "0.75rem",
  background: "var(--bg-primary)",
  borderRadius: "var(--radius-md)",
  fontSize: "0.75rem",
  color: "var(--text-muted)",
  textAlign: "center",
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

export default function AgentManagement() {
  const { toasts, toast, dismiss } = useToast();

  /* ── List state ────────────────────────────────── */
  const [agents, setAgents] = useState<Agent[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [filters, setFilters] = useState<Record<string, string>>({
    site_id: "",
    health: "",
    search: "",
  });

  /* ── Detail state ──────────────────────────────── */
  const [selectedAgent, setSelectedAgent] = useState<Agent | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);

  /* ── Enable/Disable confirm ────────────────────── */
  const [toggleConfirm, setToggleConfirm] = useState<{ agent: Agent; action: "enable" | "disable" } | null>(null);
  const [toggleLoading, setToggleLoading] = useState(false);

  /* ── Filter definitions ────────────────────────── */
  const filterDefs = useMemo<FilterDef[]>(() => [
    {
      key: "site_id",
      label: "Site",
      type: "select",
      options: [],
    },
    {
      key: "health",
      label: "Health",
      type: "select",
      options: [
        { value: "ok", label: "OK" },
        { value: "warning", label: "Warning" },
        { value: "critical", label: "Critical" },
        { value: "unknown", label: "Unknown" },
      ],
    },
    {
      key: "search",
      label: "Search",
      type: "text",
      placeholder: "Search agents...",
    },
  ], []);

  /* ── Fetch list ────────────────────────────────── */
  const fetchAgents = useCallback(async () => {
    try {
      const params = new URLSearchParams();
      if (filters.site_id) params.set("site_id", filters.site_id);
      if (filters.health) params.set("health", filters.health);
      if (filters.search) params.set("search", filters.search);
      params.set("page", String(page));
      params.set("page_size", String(PAGE_SIZE));
      // QA ISSUE-008: CC returns {agents,...}, not {items,...} — the
      // undefined read white-screened the page.
      const res = await getJson<{ agents: Agent[]; total: number }>(
        `/api/agents?${params.toString()}`,
      );
      setAgents(res.agents ?? []);
      setTotal(res.total ?? 0);
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to load agents", "error");
    } finally {
      setLoading(false);
    }
  }, [filters, page, toast]);

  useEffect(() => {
    setLoading(true);
    void fetchAgents();
  }, [fetchAgents]);

  /* ── Polling ────────────────────────────────────── */
  useEffect(() => {
    const timer = setInterval(() => void fetchAgents(), POLL_INTERVAL);
    return () => clearInterval(timer);
  }, [fetchAgents]);

  /* ── Fetch detail ──────────────────────────────── */
  const openDetail = useCallback(
    async (agent: Agent) => {
      setDetailOpen(true);
      setDetailLoading(true);
      try {
        const detail = await getJson<Agent>(`/api/agents/${agent.agent_id}`);
        setSelectedAgent(detail);
      } catch (err) {
        toast(err instanceof Error ? err.message : "Failed to load agent detail", "error");
        setDetailOpen(false);
      } finally {
        setDetailLoading(false);
      }
    },
    [toast],
  );

  /* ── Enable/Disable ────────────────────────────── */
  const handleToggle = useCallback(async () => {
    if (!toggleConfirm) return;
    setToggleLoading(true);
    try {
      await postJson(`/api/agents/${toggleConfirm.agent.agent_id}/${toggleConfirm.action}`, {});
      toast(
        `Agent ${toggleConfirm.action === "enable" ? "enabled" : "disabled"}`,
        "success",
      );
      setToggleConfirm(null);
      void fetchAgents();
      // If the detail panel is showing this agent, close it
      if (selectedAgent?.agent_id === toggleConfirm.agent.agent_id) {
        setDetailOpen(false);
        setSelectedAgent(null);
      }
    } catch (err) {
      toast(err instanceof Error ? err.message : `Failed to ${toggleConfirm.action} agent`, "error");
    } finally {
      setToggleLoading(false);
    }
  }, [toggleConfirm, toast, fetchAgents, selectedAgent]);

  /* ── Table columns ─────────────────────────────── */
  const columns = useMemo<Column<Agent>[]>(
    () => [
      {
        key: "agent_id",
        header: "Agent ID",
        render: (r) => (
          <code style={{ fontSize: "0.8125rem", fontFamily: "var(--font-mono, monospace)" }}>
            {r.agent_id}
          </code>
        ),
      },
      {
        key: "health",
        header: "Health",
        render: (r) => (
          <StatusBadge
            status={r.health || "unknown"}
            variant={HEALTH_VARIANT[r.health] ?? "neutral"}
            size="sm"
          />
        ),
      },
      { key: "observation", header: "Observation" },
      { key: "device", header: "Device", render: (r) => deviceLabel(r) },
      {
        key: "site_name",
        header: "Site",
        render: (r) => r.site_name || r.site_id,
      },
      {
        key: "last_seen_at",
        header: "Last Seen",
        // null when the site has never reported a reading. Rendering CC's
        // own cache time here (the old behaviour) made every agent look
        // fresh on each poll.
        render: (r) => (r.last_seen_at ? formatDate(r.last_seen_at) : "never"),
      },
    ],
    [],
  );

  /* ── Row actions ───────────────────────────────── */
  const rowActions = useMemo(() => [
    {
      label: "Enable",
      onClick: (r: Agent) => setToggleConfirm({ agent: r, action: "enable" as const }),
    },
    {
      label: "Disable",
      variant: "danger" as const,
      onClick: (r: Agent) => setToggleConfirm({ agent: r, action: "disable" as const }),
    },
  ], []);

  /* ── Filter handlers ───────────────────────────── */
  const handleFilterChange = useCallback((key: string, value: string) => {
    setFilters((prev) => ({ ...prev, [key]: value }));
    setPage(1);
  }, []);

  const handleFilterClear = useCallback(() => {
    setFilters({ site_id: "", health: "", search: "" });
    setPage(1);
  }, []);

  /* ── Render ─────────────────────────────────────── */
  return (
    <div>
      <Toast toasts={toasts} onDismiss={dismiss} />

      <PageHeader
        title="Agent Management"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Fleet" }, { label: "Agents" }]}
      />

      <FilterBar
        filters={filterDefs}
        values={filters}
        onChange={handleFilterChange}
        onClear={handleFilterClear}
      />

      {!loading && agents.length === 0 && !filters.search && !filters.health ? (
        <EmptyState
          title="No agents registered"
          description="Agents will appear here once they are deployed and connected."
          icon="&#x2699;"
        />
      ) : (
        <DataTable<Agent>
          columns={columns}
          data={agents}
          loading={loading}
          emptyMessage="No agents match your filters"
          page={page}
          pageSize={PAGE_SIZE}
          total={total}
          onPageChange={setPage}
          onRowClick={openDetail}
          rowActions={rowActions}
          striped
        />
      )}

      {/* ── Detail Panel ──────────────────────────── */}
      <DetailPanel
        open={detailOpen}
        onClose={() => {
          setDetailOpen(false);
          setSelectedAgent(null);
        }}
        title={selectedAgent?.agent_id ?? "Agent Details"}
        subtitle={selectedAgent ? deviceLabel(selectedAgent) : undefined}
        width={480}
      >
        {detailLoading ? (
          <div style={{ display: "flex", justifyContent: "center", padding: "2rem" }}>
            <Spinner size="md" />
          </div>
        ) : selectedAgent ? (
          <>
            <div style={sectionTitle}>Agent Info</div>
            <div style={detailRow}>
              <span style={detailLabel}>Agent ID</span>
              <span style={detailValue}>
                <code style={{ fontFamily: "var(--font-mono, monospace)" }}>{selectedAgent.agent_id}</code>
              </span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Health</span>
              <span style={detailValue}>
                <StatusBadge
                  status={selectedAgent.health || "unknown"}
                  variant={HEALTH_VARIANT[selectedAgent.health] ?? "neutral"}
                  size="sm"
                />
              </span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Observation</span>
              <span style={detailValue}>{selectedAgent.observation || "—"}</span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Device</span>
              <span style={detailValue}>{deviceLabel(selectedAgent)}</span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Class</span>
              <span style={detailValue}>{selectedAgent.device_class || "server"}</span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Site</span>
              <span style={detailValue}>{selectedAgent.site_name || selectedAgent.site_id}</span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Last Seen</span>
              <span style={detailValue}>
                {selectedAgent.last_seen_at ? formatDate(selectedAgent.last_seen_at) : "never"}
              </span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Cache Refreshed</span>
              <span style={detailValue}>
                {selectedAgent.snapshot_at ? formatDate(selectedAgent.snapshot_at) : "—"}
              </span>
            </div>

            <div style={noteStyle}>
              View detailed logs on Site Manager
            </div>

            <div style={{ marginTop: "1rem", display: "flex", gap: "0.5rem" }}>
              {/* CC exposes enable and disable as separate endpoints and
                  reports no enabled state, so both stay available rather
                  than branching on a field that does not exist. */}
              <button
                className="btn btn-danger"
                onClick={() => setToggleConfirm({ agent: selectedAgent, action: "disable" })}
              >
                Disable Agent
              </button>
              {(
                <button
                  className="btn btn-primary"
                  onClick={() => setToggleConfirm({ agent: selectedAgent, action: "enable" })}
                >
                  Enable Agent
                </button>
              )}
            </div>
          </>
        ) : null}
      </DetailPanel>

      {/* ── Toggle confirm dialog ─────────────────── */}
      <ConfirmDialog
        open={toggleConfirm !== null}
        title={toggleConfirm?.action === "enable" ? "Enable Agent" : "Disable Agent"}
        message={
          toggleConfirm?.action === "enable"
            ? `Enable agent "${toggleConfirm?.agent.agent_id}"? It will resume normal operations.`
            : `Disable agent "${toggleConfirm?.agent.agent_id}"? It will stop executing actions but continue basic monitoring.`
        }
        confirmLabel={toggleConfirm?.action === "enable" ? "Enable" : "Disable"}
        variant={toggleConfirm?.action === "disable" ? "danger" : "default"}
        onConfirm={handleToggle}
        onCancel={() => setToggleConfirm(null)}
        loading={toggleLoading}
      />
    </div>
  );
}
