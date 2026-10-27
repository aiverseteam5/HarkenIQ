import { Link, useLocation, useParams } from "react-router-dom";
import { type CSSProperties, useCallback, useEffect, useMemo, useState } from "react";
import PageHeader from "../components/PageHeader";
import FilterBar, { type FilterDef } from "../components/FilterBar";
import DataTable, { type Column } from "../components/DataTable";
import DetailPanel from "../components/DetailPanel";
import MetricCard from "../components/MetricCard";
import StatusBadge from "../components/StatusBadge";
import { statusTone } from "../viewModel/status";
import EmptyState from "../components/EmptyState";
import Toast from "../components/Toast";
import DataState from "../components/DataState";
import { useToast } from "../components/useToast";
import { ApiError, getJson } from "../api";
import { links, queryString } from "../routes";
import { useObjectRoute, useQueryState } from "../useUrlState";
import { classifyFailure, type Failure } from "../viewModel/dataState";
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

/* S1 2026-08-29: risk + CVE exposure in the drawer — both endpoints
   existed with zero consumers (p1-agentic-product.md §3). */
interface DeviceRisk {
  agent_id: string;
  risk_score: number;
  band: string; // low | medium | high | insufficient_data
  factors: Record<string, unknown>;
}

interface CveExposure {
  agent_id: string;
  component: string;
  component_name: string;
  version: string;
  cve_id: string;
  severity: string;
  fixed_version: string;
}

interface SiteRow {
  id: string;
  site_name: string;
}



interface FleetRow extends FleetDevice {
  name: string;
  observation: string;
  subsystems_json: Record<string, string> | null;
}

/* ── Constants ────────────────────────────────────── */

const PAGE_SIZE = 20;
const POLL_INTERVAL = 30000;

/** A34.4: the list's collection state, in the query string. */
const FLEET_QUERY = {
  site_id: "",
  vendor: "",
  health: "",
  observation: "",
  search: "",
  page: "1",
} as const satisfies Record<string, string>;

type Resolved =
  | { kind: "found"; device: FleetDeviceDetail }
  | { kind: "ambiguous"; rows: FleetRow[] }
  | { kind: "missing" };

/** A34.4: a device link names its Harken Node `agent_id`, because the
 *  `cc_fleet_cache` row id it used to name is deleted and re-inserted on
 *  every poll (`clear_site`). The id is resolved to the CURRENT row through
 *  the same scoped fleet read the list makes -- a lookup, not a decision: a
 *  device the reader cannot see is simply not returned.
 *
 *  `atSiteId` picks one site's report when a node is reported at two sites
 *  at once (a transient ambiguity B2-2 records); without it, two rows are
 *  stated as two rows rather than one being chosen silently. */
async function resolveDevice(
  tenantId: string,
  agentId: string,
  atSiteId: string,
): Promise<Resolved> {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const res = await getJson<PaginatedResponse<FleetRow>>(
      `/api/t/${tenantId}/fleet${queryString({ search: agentId, page_size: 200 })}`,
    );
    const rows = (res.items ?? []).filter((r) => r.agent_id === agentId);
    if (rows.length === 0) return { kind: "missing" };
    const here = atSiteId ? rows.filter((r) => r.site_id === atSiteId) : rows;
    if (here.length !== 1) return { kind: "ambiguous", rows };
    try {
      const device = await getJson<FleetDeviceDetail>(
        `/api/t/${tenantId}/fleet/${encodeURIComponent(here[0].id)}`,
      );
      return { kind: "found", device };
    } catch (err) {
      // The poll may have replaced the row between the two reads: read the
      // list once more rather than report a device that is there as gone.
      if (attempt === 0 && err instanceof ApiError && err.status === 404) continue;
      throw err;
    }
  }
  return { kind: "missing" };
}




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

export default function FleetOverview() {
  // A34.4: the open device is the URL's optional segment (its Harken Node
  // id), and the filters and page are its query string.
  const { tenantId = "", agentId } = useParams<{ tenantId: string; agentId?: string }>();
  const [query, setQuery, resetQuery] = useQueryState(FLEET_QUERY);
  const filters = query;
  const page = Math.max(1, Number.parseInt(query.page, 10) || 1);
  const atSiteId = new URLSearchParams(useLocation().search).get("at") ?? "";
  const { open, close } = useObjectRoute(links.fleet(tenantId));
  const { toasts, toast, dismiss } = useToast();

  /* ── Summary state ─────────────────────────────── */
  const [summary, setSummary] = useState<FleetSummary | null>(null);

  /* ── List state ────────────────────────────────── */
  const [devices, setDevices] = useState<FleetRow[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [sortColumn, setSortColumn] = useState("health");
  const [sortDirection, setSortDirection] = useState<"asc" | "desc">("asc");

  /* ── Detail state ──────────────────────────────── */
  const [selectedDevice, setSelectedDevice] = useState<FleetDeviceDetail | null>(null);
  const [deviceFailure, setDeviceFailure] = useState<Failure | null>(null);
  /** The rows reporting this Harken Node when it is at more than one site. */
  const [atSites, setAtSites] = useState<FleetRow[]>([]);
  const [deviceRisk, setDeviceRisk] = useState<DeviceRisk | null>(null);
  const [deviceCves, setDeviceCves] = useState<CveExposure[]>([]);
  const [sites, setSites] = useState<SiteRow[]>([]);

  /* ── Sites for the filter (was a permanently empty dropdown) ── */
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const res = await getJson<{ sites: SiteRow[] }>(
          `/api/t/${tenantId}/sites?page_size=200`,
        );
        if (!cancelled) setSites(res.sites ?? []);
      } catch {
        // filter degrades; the table still loads
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [tenantId]);

  /* ── Filter definitions ────────────────────────── */
  const filterDefs = useMemo<FilterDef[]>(() => [
    {
      key: "site_id",
      label: "Site",
      type: "select",
      options: sites.map((s) => ({ value: s.id, label: s.site_name })),
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

  /* ── Fetch detail: the device the URL names ─────── */
  useEffect(() => {
    setSelectedDevice(null);
    setDeviceFailure(null);
    setAtSites([]);
    setDeviceRisk(null);
    setDeviceCves([]);
    if (!agentId) return;
    let cancelled = false;
    void (async () => {
      try {
        const resolved = await resolveDevice(tenantId, agentId, atSiteId);
        if (cancelled) return;
        if (resolved.kind === "missing") {
          setDeviceFailure({ kind: "not_found", status: 404, message: "" });
          return;
        }
        if (resolved.kind === "ambiguous") {
          setAtSites(resolved.rows);
          return;
        }
        const detail = resolved.device;
        setSelectedDevice(detail);
        // S1: risk + CVE exposure for THIS device. Both are fleet-wide
        // endpoints filtered client-side by agent_id; each failure is
        // non-fatal so the drawer still renders device + warranty.
        void (async () => {
          try {
            const res = await getJson<{ risks: DeviceRisk[] }>(
              `/api/t/${tenantId}/predictive/risk`,
            );
            if (!cancelled) {
              setDeviceRisk(
                (res.risks ?? []).find((r) => r.agent_id === detail.agent_id) ?? null,
              );
            }
          } catch {
            if (!cancelled) setDeviceRisk(null);
          }
        })();
        void (async () => {
          try {
            const res = await getJson<{ exposures: CveExposure[] }>(
              `/api/t/${tenantId}/firmware/exposure`,
            );
            if (!cancelled) {
              setDeviceCves(
                (res.exposures ?? []).filter((e) => e.agent_id === detail.agent_id),
              );
            }
          } catch {
            if (!cancelled) setDeviceCves([]);
          }
        })();
      } catch (err) {
        if (!cancelled) setDeviceFailure(classifyFailure(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [tenantId, agentId, atSiteId]);

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
        header: "Harken Node ID",
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
            tone={statusTone("device.health", r.health)}
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
            tone={statusTone("device.observation", r.observation)}
            size="sm"
          />
        ),
      },
      {
        key: "last_seen_at",
        header: "Last Seen",
        sortKey: "last_seen_at",
        // No snapshot_at fallback: that is CC's cache-refresh time, and
        // showing it under "Last Seen" is what made a silent agent look
        // fresh. Never seen renders as "--".
        render: (r) => formatDate(r.last_seen_at),
      },
    ],
    [],
  );

  /* ── Filter handlers ───────────────────────────── */
  const handleFilterChange = useCallback(
    (key: string, value: string) => setQuery({ [key]: value, page: "1" }),
    [setQuery],
  );

  const handleFilterClear = resetQuery;

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
          description="Devices appear here once their Harken Nodes register and report."
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
          onPageChange={(p) => setQuery({ page: String(p) })}
          onRowClick={(r) =>
            open(
              links.device(
                tenantId,
                r.agent_id,
                // Only when this node is on the page twice: otherwise the
                // id alone names it, and the URL stays clean.
                devices.filter((d) => d.agent_id === r.agent_id).length > 1 ? r.site_id : undefined,
              ),
            )
          }
          striped
        />
      )}

      {/* ── Detail Panel ──────────────────────────── */}
      <DetailPanel
        open={Boolean(agentId)}
        onClose={close}
        title={selectedDevice?.name || selectedDevice?.agent_id || agentId || "Device Details"}
        subtitle={selectedDevice?.agent_id}
        width={520}
      >
        {!selectedDevice ? (
          atSites.length > 0 ? (
            <div role="status">
              <p style={{ fontSize: "0.875rem", marginBottom: "0.75rem" }}>
                This Harken Node is reported at more than one site right now. Each
                site's report is shown separately; choose one.
              </p>
              <ul style={{ paddingLeft: "1.1rem", lineHeight: 1.8 }}>
                {atSites.map((row) => (
                  <li key={`${row.site_id}:${row.id}`}>
                    <Link to={links.device(tenantId, row.agent_id, row.site_id)}>
                      {row.site_name || row.site_id}
                    </Link>
                  </li>
                ))}
              </ul>
            </div>
          ) : (
            <DataState
              status={deviceFailure ? deviceFailure.kind : "loading"}
              subject="this device"
              failure={deviceFailure}
            />
          )
        ) : (
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
                  tone={statusTone("device.health", selectedDevice.health)}
                  size="sm"
                />
              </span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Last Seen</span>
              <span style={detailValue}>{formatDate(selectedDevice.last_seen_at)}</span>
            </div>

            {/* S1: predictive risk — deterministic scoring that already
                existed with no consumer. "insufficient_data" is rendered
                as exactly that, never as a fabricated number. */}
            <div style={sectionTitle}>Failure Risk</div>
            {deviceRisk ? (
              <>
                <div style={detailRow}>
                  <span style={detailLabel}>Band</span>
                  <span style={detailValue}>
                    <StatusBadge
                      status={
                        deviceRisk.band === "insufficient_data"
                          ? "insufficient data"
                          : deviceRisk.band
                      }
                      tone={statusTone("risk.band", deviceRisk.band)}
                      size="sm"
                    />
                  </span>
                </div>
                {deviceRisk.band !== "insufficient_data" && (
                  <div style={detailRow}>
                    <span style={detailLabel}>Score</span>
                    <span style={detailValue}>
                      {Math.round(deviceRisk.risk_score * 100)}%
                    </span>
                  </div>
                )}
                {Object.entries(deviceRisk.factors ?? {}).map(([key, value]) => (
                  <div key={key} style={detailRow}>
                    <span style={detailLabel}>{key.replace(/_/g, " ")}</span>
                    <span style={detailValue}>
                      {typeof value === "number"
                        ? Number.isInteger(value)
                          ? value
                          : value.toFixed(3)
                        : String(value)}
                    </span>
                  </div>
                ))}
              </>
            ) : (
              <div style={detailRow}>
                <span style={detailLabel}>Band</span>
                <span style={detailValue}>
                  <StatusBadge status="unavailable" tone={statusTone("risk.band", "unavailable")} size="sm" />
                </span>
              </div>
            )}

            <div style={sectionTitle}>Warranty & Lifecycle</div>
            {selectedDevice.warranty ? (
              <>
                <div style={detailRow}>
                  <span style={detailLabel}>Status</span>
                  <span style={detailValue}>
                    <StatusBadge
                      status={selectedDevice.warranty.status}
                      tone={statusTone("device.warranty", selectedDevice.warranty.status)}
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
                  <StatusBadge status="unknown" tone={statusTone("device.warranty", "unknown")} size="sm" />
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

            {/* S1: CVE exposure from the local, air-gap-safe feed. Silence
                here means "no feed match", which is only meaningful if a
                feed is loaded — so say which. */}
            {deviceCves.length > 0 && (
              <>
                <div style={sectionTitle}>CVE Exposure ({deviceCves.length})</div>
                {deviceCves.map((cve, i) => (
                  <div key={`${cve.cve_id}-${i}`} style={detailRow}>
                    <span style={detailLabel}>
                      <code>{cve.cve_id}</code>
                      <span style={{ color: "var(--text-muted)", marginLeft: "0.5rem" }}>
                        {cve.component_name || cve.component} {cve.version}
                      </span>
                    </span>
                    <span style={detailValue}>
                      <StatusBadge
                        status={cve.severity || "unknown"}
                        tone={statusTone("cve.severity", (cve.severity || "").toLowerCase())}
                        size="sm"
                      />
                      {cve.fixed_version && (
                        <div style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>
                          fixed in {cve.fixed_version}
                        </div>
                      )}
                    </span>
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
                        tone={statusTone("device.health", severity)}
                        size="sm"
                      />
                    </span>
                  </div>
                ))}
              </>
            )}

          </>
        )}
      </DetailPanel>
    </div>
  );
}
