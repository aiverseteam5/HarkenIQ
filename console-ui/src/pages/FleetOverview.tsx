import { useParams } from "react-router-dom";
import { type CSSProperties, useCallback, useEffect, useMemo, useState } from "react";
import PageHeader from "../components/PageHeader";
import FilterBar, { type FilterDef } from "../components/FilterBar";
import DataTable, { type Column } from "../components/DataTable";
import DetailPanel from "../components/DetailPanel";
import MetricCard from "../components/MetricCard";
import StatusBadge from "../components/StatusBadge";
import EmptyState from "../components/EmptyState";
import Toast from "../components/Toast";
import Spinner from "../components/Spinner";
import { useToast } from "../components/useToast";
import { getJson } from "../api";
import type { PaginatedResponse, FleetDevice } from "../types";

/* ── Extended types ───────────────────────────────── */

// Matches GET /api/t/{tenantId}/fleet/summary (QA ISSUE-003/004: the UI previously
// read healthy_pct/open_incidents/sites, which the API never sent —
// cards rendered "undefined %" and "--").
interface FleetSummary {
  total_nodes: number;
  by_health: Record<string, number>;
  incidents_open: number;
  sites_count: number;
}

function healthyPct(s: FleetSummary): number {
  if (!s.total_nodes) return 100;
  return Math.round(((s.by_health?.ok ?? 0) / s.total_nodes) * 100);
}

interface WarrantyInfo {
  service_level: string;
  start_date: string;
  end_date: string;
  status: string;      // active | expiring | expired | unknown
  source: string;
}

interface FleetDeviceDetail extends FleetDevice {
  name: string;
  observation: string;
  subsystems_json: Record<string, string> | null;
  warranty?: WarrantyInfo | null;
  firmware?: { component: string; name: string; version: string }[];
}

interface FleetRow extends FleetDevice {
  name: string;
  observation: string;
  subsystems_json: Record<string, string> | null;
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

const OBSERVATION_VARIANT: Record<string, "success" | "warning" | "critical" | "info" | "neutral"> = {
  normal: "success",
  degraded: "warning",
  faulted: "critical",
  unknown: "neutral",
};

const WARRANTY_VARIANT: Record<string, "success" | "warning" | "critical" | "neutral"> = {
  active: "success",
  expiring: "warning",
  expired: "critical",
  unknown: "neutral",
};

const HEALTH_SORT_ORDER: Record<string, number> = {
  critical: 0,
  warning: 1,
  unknown: 2,
  ok: 3,
};

/* ── Styles ───────────────────────────────────────── */

const metricsRow: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))",
  gap: "1rem",
  marginBottom: "1.5rem",
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

/* ── Helpers ──────────────────────────────────────── */

function formatDate(iso: string | undefined): string {
  if (!iso) return "--";
  return new Date(iso).toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/* ── Component ────────────────────────────────────── */

export default function FleetOverview() {
  const { tenantId = "" } = useParams<{ tenantId: string }>();
  const { toasts, toast, dismiss } = useToast();

  /* ── Summary state ─────────────────────────────── */
  const [summary, setSummary] = useState<FleetSummary | null>(null);

  /* ── List state ────────────────────────────────── */
  const [devices, setDevices] = useState<FleetRow[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [sortColumn, setSortColumn] = useState("health");
  const [sortDirection, setSortDirection] = useState<"asc" | "desc">("asc");
  const [filters, setFilters] = useState<Record<string, string>>({
    site_id: "",
    vendor: "",
    health: "",
    observation: "",
    search: "",
  });

  /* ── Detail state ──────────────────────────────── */
  const [selectedDevice, setSelectedDevice] = useState<FleetDeviceDetail | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);

  /* ── Filter definitions ────────────────────────── */
  const filterDefs = useMemo<FilterDef[]>(() => [
    {
      key: "site_id",
      label: "Site",
      type: "select",
      options: [], // populated dynamically if needed
    },
    {
      key: "vendor",
      label: "Vendor",
      type: "select",
      options: [
        { value: "Dell", label: "Dell" },
        { value: "HPE", label: "HPE" },
        { value: "Lenovo", label: "Lenovo" },
        { value: "Supermicro", label: "Supermicro" },
      ],
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
      key: "observation",
      label: "Observation",
      type: "select",
      options: [
        { value: "normal", label: "Normal" },
        { value: "degraded", label: "Degraded" },
        { value: "faulted", label: "Faulted" },
        { value: "unknown", label: "Unknown" },
      ],
    },
    { key: "search", label: "Search", type: "text", placeholder: "Search devices..." },
  ], []);

  /* ── Fetch summary ─────────────────────────────── */
  const fetchSummary = useCallback(async () => {
    try {
      const res = await getJson<FleetSummary>(`/api/t/${tenantId}/fleet/summary`);
      setSummary(res);
    } catch {
      // summary fetch failures are non-fatal
    }
  }, []);

  /* ── Fetch list ────────────────────────────────── */
  const fetchDevices = useCallback(async () => {
    try {
      const params = new URLSearchParams();
      if (filters.site_id) params.set("site_id", filters.site_id);
      if (filters.vendor) params.set("vendor", filters.vendor);
      if (filters.health) params.set("health", filters.health);
      if (filters.observation) params.set("observation", filters.observation);
      if (filters.search) params.set("search", filters.search);
      params.set("page", String(page));
      params.set("page_size", String(PAGE_SIZE));
      const res = await getJson<PaginatedResponse<FleetRow>>(
        `/api/t/${tenantId}/fleet?${params.toString()}`,
      );
      // Client-side sort by health (critical first)
      const sorted = [...res.items].sort((a, b) => {
        const aOrder = HEALTH_SORT_ORDER[a.health] ?? 4;
        const bOrder = HEALTH_SORT_ORDER[b.health] ?? 4;
        return sortDirection === "asc" ? aOrder - bOrder : bOrder - aOrder;
      });
      setDevices(sorted);
      setTotal(res.total);
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to load fleet data", "error");
    } finally {
      setLoading(false);
    }
  }, [filters, page, sortDirection, toast]);

  useEffect(() => {
    setLoading(true);
    void fetchDevices();
    void fetchSummary();
  }, [fetchDevices, fetchSummary]);

  /* ── Polling ────────────────────────────────────── */
  useEffect(() => {
    const timer = setInterval(() => {
      void fetchDevices();
      void fetchSummary();
    }, POLL_INTERVAL);
    return () => clearInterval(timer);
  }, [fetchDevices, fetchSummary]);

  /* ── Fetch detail ──────────────────────────────── */
  const openDetail = useCallback(
    async (device: FleetRow) => {
      setDetailOpen(true);
      setDetailLoading(true);
      try {
        const detail = await getJson<FleetDeviceDetail>(`/api/t/${tenantId}/fleet/${device.id}`);
        setSelectedDevice(detail);
      } catch (err) {
        toast(err instanceof Error ? err.message : "Failed to load device detail", "error");
        setDetailOpen(false);
      } finally {
        setDetailLoading(false);
      }
    },
    [toast],
  );

  /* ── Sort handler ──────────────────────────────── */
  const handleSort = useCallback(
    (column: string) => {
      if (sortColumn === column) {
        setSortDirection((prev) => (prev === "asc" ? "desc" : "asc"));
      } else {
        setSortColumn(column);
        setSortDirection("asc");
      }
    },
    [sortColumn],
  );

  /* ── Table columns ─────────────────────────────── */
  const columns = useMemo<Column<FleetRow>[]>(
    () => [
      {
        key: "agent_id",
        header: "Agent ID",
        sortKey: "agent_id",
        render: (r) => (
          <code style={{ fontSize: "0.8125rem", fontFamily: "var(--font-mono, monospace)" }}>
            {r.agent_id}
          </code>
        ),
      },
      { key: "name", header: "Name", sortKey: "name", render: (r) => r.name || r.service_tag || "--" },
      { key: "vendor", header: "Vendor", sortKey: "vendor" },
      { key: "model", header: "Model" },
      {
        key: "device_class",
        header: "Class",
        render: (r) => (r.device_class === "switch" ? "Switch" : "Server"),
      },
      { key: "site_name", header: "Site", sortKey: "site_name" },
      {
        key: "health",
        header: "Health",
        sortKey: "health",
        render: (r) => (
          <StatusBadge
            status={r.health}
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
        key: "last_seen_at",
        header: "Last Seen",
        sortKey: "last_seen_at",
        render: (r) => formatDate(r.last_seen_at ?? r.snapshot_at),
      },
    ],
    [],
  );

  /* ── Filter handlers ───────────────────────────── */
  const handleFilterChange = useCallback((key: string, value: string) => {
    setFilters((prev) => ({ ...prev, [key]: value }));
    setPage(1);
  }, []);

  const handleFilterClear = useCallback(() => {
    setFilters({ site_id: "", vendor: "", health: "", observation: "", search: "" });
    setPage(1);
  }, []);

  /* ── Render ─────────────────────────────────────── */
  return (
    <div>
      <Toast toasts={toasts} onDismiss={dismiss} />

      <PageHeader
        title="Fleet Overview"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Fleet" }, { label: "Overview" }]}
      />

      {/* Summary cards */}
      <div style={metricsRow}>
        <MetricCard
          title="Total Nodes"
          value={summary?.total_nodes ?? "--"}
        />
        <MetricCard
          title="Healthy"
          value={summary ? `${healthyPct(summary)}` : "--"}
          unit="%"
          trend={summary && healthyPct(summary) >= 95 ? "up" : summary && healthyPct(summary) < 80 ? "down" : "flat"}
        />
        <MetricCard
          title="Open Incidents"
          value={summary?.incidents_open ?? "--"}
          trend={summary && summary.incidents_open > 0 ? "down" : "flat"}
        />
        <MetricCard
          title="Sites"
          value={summary?.sites_count ?? "--"}
        />
      </div>

      <FilterBar
        filters={filterDefs}
        values={filters}
        onChange={handleFilterChange}
        onClear={handleFilterClear}
      />

      {!loading && devices.length === 0 && !filters.search && !filters.health && !filters.vendor && !filters.observation ? (
        <EmptyState
          title="No devices in fleet"
          description="Devices will appear here once agents are registered and reporting."
          icon="&#x2318;"
        />
      ) : (
        <DataTable<FleetRow>
          columns={columns}
          data={devices}
          loading={loading}
          emptyMessage="No devices match your filters"
          sortColumn={sortColumn}
          sortDirection={sortDirection}
          onSort={handleSort}
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
          setSelectedDevice(null);
        }}
        title={selectedDevice?.name || selectedDevice?.agent_id || "Device Details"}
        subtitle={selectedDevice?.agent_id}
        width={520}
      >
        {detailLoading ? (
          <div style={{ display: "flex", justifyContent: "center", padding: "2rem" }}>
            <Spinner size="md" />
          </div>
        ) : selectedDevice ? (
          <>
            <div style={sectionTitle}>Device Info</div>
            <div style={detailRow}>
              <span style={detailLabel}>Vendor</span>
              <span style={detailValue}>{selectedDevice.vendor}</span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Model</span>
              <span style={detailValue}>{selectedDevice.model}</span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Service Tag</span>
              <span style={detailValue}>
                <code>{selectedDevice.service_tag}</code>
              </span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Site</span>
              <span style={detailValue}>{selectedDevice.site_name}</span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Health</span>
              <span style={detailValue}>
                <StatusBadge
                  status={selectedDevice.health}
                  variant={HEALTH_VARIANT[selectedDevice.health] ?? "neutral"}
                  size="sm"
                />
              </span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Last Seen</span>
              <span style={detailValue}>{formatDate(selectedDevice.last_seen_at ?? selectedDevice.snapshot_at)}</span>
            </div>

            <div style={sectionTitle}>Warranty & Lifecycle</div>
            {selectedDevice.warranty ? (
              <>
                <div style={detailRow}>
                  <span style={detailLabel}>Status</span>
                  <span style={detailValue}>
                    <StatusBadge
                      status={selectedDevice.warranty.status}
                      variant={WARRANTY_VARIANT[selectedDevice.warranty.status] ?? "neutral"}
                      size="sm"
                    />
                  </span>
                </div>
                <div style={detailRow}>
                  <span style={detailLabel}>Service Level</span>
                  <span style={detailValue}>{selectedDevice.warranty.service_level || "--"}</span>
                </div>
                <div style={detailRow}>
                  <span style={detailLabel}>Expires</span>
                  <span style={detailValue}>{selectedDevice.warranty.end_date || "--"}</span>
                </div>
              </>
            ) : (
              <div style={detailRow}>
                <span style={detailLabel}>Status</span>
                <span style={detailValue}>
                  <StatusBadge status="unknown" variant="neutral" size="sm" />
                </span>
              </div>
            )}

            {selectedDevice.firmware && selectedDevice.firmware.length > 0 && (
              <>
                <div style={sectionTitle}>Firmware</div>
                {selectedDevice.firmware.map((fw, i) => (
                  <div key={`${fw.component}-${i}`} style={detailRow}>
                    <span style={detailLabel}>{fw.name || fw.component}</span>
                    <span style={detailValue}><code>{fw.version}</code></span>
                  </div>
                ))}
              </>
            )}

            {selectedDevice.subsystems_json && Object.keys(selectedDevice.subsystems_json).length > 0 && (
              <>
                <div style={sectionTitle}>Subsystem States</div>
                {Object.entries(selectedDevice.subsystems_json).map(([subsystem, severity]) => (
                  <div key={subsystem} style={detailRow}>
                    <span style={detailLabel}>{subsystem}</span>
                    <span style={detailValue}>
                      <StatusBadge
                        status={severity}
                        variant={HEALTH_VARIANT[severity] ?? "neutral"}
                        size="sm"
                      />
                    </span>
                  </div>
                ))}
              </>
            )}

          </>
        ) : null}
      </DetailPanel>
    </div>
  );
}
