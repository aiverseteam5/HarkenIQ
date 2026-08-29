import { useParams } from "react-router-dom";
import { type CSSProperties, useCallback, useEffect, useMemo, useState } from "react";
import PageHeader from "../components/PageHeader";
import FilterBar, { type FilterDef } from "../components/FilterBar";
import DataTable, { type Column } from "../components/DataTable";
import DetailPanel from "../components/DetailPanel";
import StatusBadge from "../components/StatusBadge";
import EmptyState from "../components/EmptyState";
import Toast from "../components/Toast";
import Spinner from "../components/Spinner";
import { useToast } from "../components/useToast";
import { getJson } from "../api";

/* ── Types ────────────────────────────────────────────
   P0 2026-08-29 (final assessment §7): this page used to declare
   version/status/device/last_seen/enabled — fields Central Command never
   sends — so every column rendered blank, the detail drawer 404ed
   (agent.id was undefined), and the Enable/Disable buttons drove
   backend placebos. The interface now IS the CC contract
   (harkeniq_cc/api/agents.py::_agent_dict), nothing more. */

interface Agent {
  agent_id: string;
  agent_name: string;
  vendor: string;
  model: string;
  observation: string;
  health: string;
  site_id: string;
  snapshot_at: string | null;
}

interface AgentDetail extends Agent {
  site_name?: string;
  subsystems?: Record<string, string> | null;
}

interface SiteRow {
  id: string;
  site_name: string;
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

const OBSERVATION_VARIANT: Record<string, "success" | "neutral"> = {
  observed: "success",
  unobserved: "neutral",
};

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

const framingNote: CSSProperties = {
  padding: "0.75rem 1rem",
  marginBottom: "1rem",
  background: "var(--bg-card)",
  border: "1px solid var(--border-light)",
  borderRadius: "var(--radius-md)",
  fontSize: "0.8125rem",
  color: "var(--text-secondary)",
  lineHeight: 1.5,
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

function formatDate(iso: string | null | undefined): string {
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
  const { tenantId = "" } = useParams<{ tenantId: string }>();
  const { toasts, toast, dismiss } = useToast();

  /* ── List state ────────────────────────────────── */
  const [agents, setAgents] = useState<Agent[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [sites, setSites] = useState<SiteRow[]>([]);
  const [filters, setFilters] = useState<Record<string, string>>({
    site_id: "",
    search: "",
  });

  /* ── Detail state ──────────────────────────────── */
  const [selectedAgent, setSelectedAgent] = useState<AgentDetail | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);

  /* ── Filter definitions ────────────────────────── */
  const filterDefs = useMemo<FilterDef[]>(() => [
    {
      key: "site_id",
      label: "Site",
      type: "select",
      // Populated from the real sites list — the old page shipped a
      // permanently empty dropdown.
      options: sites.map((s) => ({ value: s.id, label: s.site_name })),
    },
    {
      key: "search",
      label: "Search",
      type: "text",
      placeholder: "Search agents...",
    },
  ], [sites]);

  /* ── Fetch sites for the filter ────────────────── */
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const res = await getJson<{ sites: SiteRow[] }>(
          `/api/t/${tenantId}/sites?page_size=200`,
        );
        if (!cancelled) setSites(res.sites ?? []);
      } catch {
        // The filter degrades to search-only; the table still loads.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [tenantId]);

  /* ── Fetch list ────────────────────────────────── */
  const fetchAgents = useCallback(async () => {
    try {
      const params = new URLSearchParams();
      if (filters.site_id) params.set("site_id", filters.site_id);
      if (filters.search) params.set("search", filters.search);
      params.set("page", String(page));
      params.set("page_size", String(PAGE_SIZE));
      const res = await getJson<{ agents: Agent[]; total: number }>(
        `/api/t/${tenantId}/agents?${params.toString()}`,
      );
      setAgents(res.agents ?? []);
      setTotal(res.total ?? 0);
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to load agents", "error");
    } finally {
      setLoading(false);
    }
  }, [filters, page, toast, tenantId]);

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
        const detail = await getJson<AgentDetail>(
          `/api/t/${tenantId}/agents/${agent.agent_id}`,
        );
        setSelectedAgent(detail);
      } catch (err) {
        toast(err instanceof Error ? err.message : "Failed to load agent detail", "error");
        setDetailOpen(false);
      } finally {
        setDetailLoading(false);
      }
    },
    [toast, tenantId],
  );

  /* ── Filter handlers ───────────────────────────── */
  const handleFilterChange = useCallback((key: string, value: string) => {
    setFilters((prev) => ({ ...prev, [key]: value }));
    setPage(1);
  }, []);

  const handleFilterClear = useCallback(() => {
    setFilters({ site_id: "", search: "" });
    setPage(1);
  }, []);

  /* ── Table columns — every column is a field CC sends ── */
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
      { key: "agent_name", header: "Name" },
      { key: "vendor", header: "Vendor" },
      { key: "model", header: "Model" },
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
      {
        key: "observation",
        header: "Observation",
        render: (r) => (
          <StatusBadge
            status={r.observation || "unknown"}
            variant={OBSERVATION_VARIANT[r.observation] ?? "neutral"}
            size="sm"
          />
        ),
      },
      {
        key: "snapshot_at",
        header: "Snapshot At",
        render: (r) => formatDate(r.snapshot_at),
      },
    ],
    [],
  );

  /* ── Render ─────────────────────────────────────── */
  return (
    <div>
      <Toast toasts={toasts} onDismiss={dismiss} />

      <PageHeader
        title="Harken Nodes"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Fleet" }, { label: "Harken Nodes" }]}
      />

      {/* S1 2026-08-29: "agent" means three distinct things in HarkenIQ and
          conflating them is how a product grows a second runtime. Say which
          one this page is (p1-agentic-product.md §2). */}
      <div style={framingNote}>
        <strong>Harken Nodes</strong> are the deployed agents: one per device,
        each with its own cryptographic identity, and the final safety gate on
        every action. They are not the same as <em>operational agents</em>
        (named, scoped bundles a tenant configures) or{" "}
        <em>external agents</em> (your own software calling HarkenIQ with its
        own credential) — both arrive in later releases.
      </div>

      <FilterBar
        filters={filterDefs}
        values={filters}
        onChange={handleFilterChange}
        onClear={handleFilterClear}
      />

      {!loading && agents.length === 0 && !filters.search && !filters.site_id ? (
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
        title={selectedAgent?.agent_name || selectedAgent?.agent_id || "Agent Details"}
        subtitle={selectedAgent ? `${selectedAgent.vendor} ${selectedAgent.model}` : undefined}
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
              <span style={detailLabel}>Name</span>
              <span style={detailValue}>{selectedAgent.agent_name || "--"}</span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Vendor / Model</span>
              <span style={detailValue}>
                {selectedAgent.vendor || "--"} {selectedAgent.model}
              </span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Site</span>
              <span style={detailValue}>{selectedAgent.site_name ?? selectedAgent.site_id}</span>
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
              <span style={detailValue}>
                <StatusBadge
                  status={selectedAgent.observation || "unknown"}
                  variant={OBSERVATION_VARIANT[selectedAgent.observation] ?? "neutral"}
                  size="sm"
                />
              </span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Snapshot At</span>
              <span style={detailValue}>{formatDate(selectedAgent.snapshot_at)}</span>
            </div>

            {selectedAgent.subsystems &&
              Object.keys(selectedAgent.subsystems).length > 0 && (
                <>
                  <div style={sectionTitle}>Subsystem States</div>
                  {Object.entries(selectedAgent.subsystems).map(([subsystem, severity]) => (
                    <div key={subsystem} style={detailRow}>
                      <span style={detailLabel}>{subsystem}</span>
                      <span style={detailValue}>
                        <StatusBadge
                          status={String(severity)}
                          variant={HEALTH_VARIANT[String(severity)] ?? "neutral"}
                          size="sm"
                        />
                      </span>
                    </div>
                  ))}
                </>
              )}

            <div style={noteStyle}>
              Detailed telemetry and action history live on the Site Manager
            </div>
          </>
        ) : null}
      </DetailPanel>
    </div>
  );
}
