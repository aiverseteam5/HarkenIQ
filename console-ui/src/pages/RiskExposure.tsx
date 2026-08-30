import { Link, useParams } from "react-router-dom";
import { type CSSProperties, useCallback, useEffect, useMemo, useState } from "react";
import PageHeader from "../components/PageHeader";
import FilterBar, { type FilterDef } from "../components/FilterBar";
import DataTable, { type Column } from "../components/DataTable";
import DetailPanel from "../components/DetailPanel";
import MetricCard from "../components/MetricCard";
import StatusBadge from "../components/StatusBadge";
import EmptyState from "../components/EmptyState";
import Toast from "../components/Toast";
import { useToast } from "../components/useToast";
import { getJson } from "../api";

/* S2 — Risk & Exposure: the decision surface.
 *
 * This page RENDERS the attention capability (GET /api/attention); it does
 * not compute it. Ranking, explanation, evidence and the recommended next
 * capability all arrive from Central Command, so a named agent or an MCP
 * tool reading the same contract sees exactly what the operator sees.
 * Adding judgement here would fork the two. */

/* ── Contract (mirrors harkeniq_cc/attention.py) ──── */

interface Confidence {
  basis: string;
  sample_count: number;
  sufficient: boolean;
  explanation: string;
}

interface CveEvidence {
  cve_id: string;
  severity: string;
  component: string;
  version: string;
  fixed_version: string;
}

interface PatternEvidence {
  pattern_id: string;
  pattern_type: string;
  description: string;
  confidence: number;
}

interface PendingApproval {
  action_id: string;
  action_type: string;
}

interface RecommendedNext {
  capability: string;
  summary: string;
  requires_approval: boolean;
  available: boolean;
  unavailable_reason?: string;
  refs: string[];
}

interface AttentionItem {
  rank: number;
  agent_id: string;
  agent_name: string;
  device_id: string | null;
  site_id: string;
  site_name: string;
  vendor: string;
  model: string;
  device_class: string;
  health: string;
  observation: string;
  risk_score: number;
  band: string;
  attention_driver: string;
  attention_driver_label: string;
  confidence: Confidence;
  factors: Record<string, unknown>;
  reasons: string[];
  evidence: {
    cves: CveEvidence[];
    warranty: { end_date: string } | null;
    fleet_patterns: PatternEvidence[];
  };
  current_state: {
    pending_approvals: PendingApproval[];
    open_action_count: number;
  };
  recommended_next: RecommendedNext;
}

interface SiteRollup {
  site_id: string;
  site_name: string;
  device_count: number;
  needs_attention: number;
  by_band: Record<string, number>;
  top_risk_score: number;
  top_device: string | null;
  cve_count: number;
  pending_approvals: number;
}

interface AttentionResponse {
  tenant_id: string;
  generated_at: string;
  sites: SiteRollup[];
  items: AttentionItem[];
  summary: {
    devices_scored: number;
    attention_required: number;
    insufficient_data_count: number;
    devices_with_cves: number;
    actions_awaiting_approval: number;
  };
}

/* ── Presentation ─────────────────────────────────── */

const BAND_VARIANT: Record<string, "success" | "warning" | "critical" | "neutral"> = {
  high: "critical",
  medium: "warning",
  low: "success",
  insufficient_data: "neutral",
};

const BAND_LABEL: Record<string, string> = {
  high: "high",
  medium: "medium",
  low: "low",
  insufficient_data: "insufficient data",
};

const CVE_VARIANT: Record<string, "warning" | "critical" | "info" | "neutral"> = {
  critical: "critical",
  high: "critical",
  medium: "warning",
  low: "info",
};

/* Why a row sits where it sits. Ranking is not the risk band alone: a
   device failing NOW outranks a healthy one with worse predicted risk. */
const DRIVER_VARIANT: Record<string, "critical" | "warning" | "info" | "neutral"> = {
  current_failure: "critical",
  awaiting_approval: "info",
  degraded_now: "warning",
  predicted_risk: "neutral",
  insufficient_evidence: "neutral",
};

/** What each recommended capability means to a human, and where it leads.
 *  `to` is null when the capability is not reachable from this console yet;
 *  the contract tells us that via `available`. */
const CAPABILITY_LABEL: Record<string, string> = {
  review_pending_approval: "Review pending approval",
  plan_firmware_remediation: "Plan firmware remediation",
  investigate_device: "Investigate device",
  collect_evidence: "Keep observing",
  monitor: "Monitor",
};

const metricsRow: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fit, minmax(190px, 1fr))",
  gap: "1rem",
  marginBottom: "1.5rem",
};

const siteGrid: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fill, minmax(240px, 1fr))",
  gap: "0.75rem",
  marginBottom: "1.5rem",
};

const siteCard = (active: boolean): CSSProperties => ({
  background: "var(--bg-card)",
  border: `1px solid ${active ? "var(--color-primary, #0E7A73)" : "var(--border-light)"}`,
  borderRadius: "var(--radius-md)",
  padding: "0.875rem 1rem",
  cursor: "pointer",
  textAlign: "left",
  width: "100%",
  font: "inherit",
  color: "inherit",
});

const siteName: CSSProperties = { fontWeight: 600, marginBottom: "0.375rem" };
const siteMeta: CSSProperties = {
  fontSize: "0.75rem", color: "var(--text-secondary)", display: "flex",
  gap: "0.75rem", flexWrap: "wrap", marginTop: "0.5rem",
};

const sectionTitle: CSSProperties = {
  fontSize: "0.8125rem", fontWeight: 600, color: "var(--text-secondary)",
  textTransform: "uppercase", letterSpacing: "0.04em", marginBottom: "0.75rem",
  marginTop: "1.25rem", borderBottom: "1px solid var(--border-light)",
  paddingBottom: "0.375rem",
};

const detailRow: CSSProperties = {
  display: "flex", justifyContent: "space-between", padding: "0.375rem 0",
  fontSize: "0.8125rem", borderBottom: "1px solid var(--border-light)",
};
const detailLabel: CSSProperties = { color: "var(--text-secondary)", fontWeight: 500 };
const detailValue: CSSProperties = {
  color: "var(--text-primary)", fontWeight: 500, textAlign: "right",
};

const reasonList: CSSProperties = {
  margin: "0.5rem 0 0", paddingLeft: "1.1rem", fontSize: "0.8125rem",
  lineHeight: 1.6,
};

const recBox = (available: boolean): CSSProperties => ({
  marginTop: "0.75rem", padding: "0.75rem 0.875rem",
  background: "var(--bg-primary)", borderRadius: "var(--radius-sm)",
  borderLeft: `3px solid ${available ? "var(--color-primary, #0E7A73)" : "var(--border-light)"}`,
  fontSize: "0.8125rem",
});

/* ── Component ────────────────────────────────────── */

const PAGE_SIZE = 25;
const POLL_INTERVAL = 60000;

export default function RiskExposure() {
  const { tenantId = "" } = useParams<{ tenantId: string }>();
  const { toasts, toast, dismiss } = useToast();

  const [data, setData] = useState<AttentionResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [page, setPage] = useState(1);
  const [siteFilter, setSiteFilter] = useState("");
  const [filters, setFilters] = useState<Record<string, string>>({ band: "" });
  const [selected, setSelected] = useState<AttentionItem | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);

  const fetchAttention = useCallback(async () => {
    try {
      const params = new URLSearchParams();
      if (siteFilter) params.set("site_id", siteFilter);
      if (filters.band) params.set("band", filters.band);
      const res = await getJson<AttentionResponse>(
        `/api/t/${tenantId}/attention?${params.toString()}`,
      );
      setData(res);
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to load risk data", "error");
    } finally {
      setLoading(false);
    }
  }, [tenantId, siteFilter, filters.band, toast]);

  useEffect(() => {
    setLoading(true);
    void fetchAttention();
  }, [fetchAttention]);

  useEffect(() => {
    const timer = setInterval(() => void fetchAttention(), POLL_INTERVAL);
    return () => clearInterval(timer);
  }, [fetchAttention]);

  const filterDefs = useMemo<FilterDef[]>(() => [
    {
      key: "band",
      label: "Risk band",
      type: "select",
      options: [
        { value: "high", label: "High" },
        { value: "medium", label: "Medium" },
        { value: "low", label: "Low" },
        { value: "insufficient_data", label: "Insufficient data" },
      ],
    },
  ], []);

  const items = data?.items ?? [];
  const paged = items.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  const columns = useMemo<Column<AttentionItem>[]>(() => [
    { key: "rank", header: "#", render: (r) => r.rank },
    {
      key: "agent_name",
      header: "Device",
      render: (r) => (
        <span>
          {r.agent_name || r.agent_id}
          <div style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>
            {r.vendor} {r.model}
          </div>
        </span>
      ),
    },
    { key: "site_name", header: "Site", render: (r) => r.site_name || "--" },
    {
      key: "attention_driver",
      header: "Why now",
      render: (r) => (
        <StatusBadge
          status={r.attention_driver_label}
          variant={DRIVER_VARIANT[r.attention_driver] ?? "neutral"}
          size="sm"
        />
      ),
    },
    {
      key: "band",
      header: "Predicted risk",
      render: (r) => (
        <StatusBadge
          status={BAND_LABEL[r.band] ?? r.band}
          variant={BAND_VARIANT[r.band] ?? "neutral"}
          size="sm"
        />
      ),
    },
    {
      key: "reasons",
      header: "Why",
      render: (r) => (
        <span style={{ fontSize: "0.8125rem" }}>
          {r.reasons[0] ?? "No evidence recorded."}
          {r.reasons.length > 1 && (
            <span style={{ color: "var(--text-muted)" }}>
              {" "}+{r.reasons.length - 1} more
            </span>
          )}
        </span>
      ),
    },
    {
      key: "recommended_next",
      header: "Next",
      render: (r) => (
        <span style={{ fontSize: "0.8125rem" }}>
          {CAPABILITY_LABEL[r.recommended_next.capability] ?? r.recommended_next.capability}
          {r.recommended_next.requires_approval && (
            <div style={{ fontSize: "0.7rem", color: "var(--text-muted)" }}>
              needs approval
            </div>
          )}
        </span>
      ),
    },
  ], []);

  const openDetail = useCallback((item: AttentionItem) => {
    setSelected(item);
    setDetailOpen(true);
  }, []);

  return (
    <div>
      <Toast toasts={toasts} onDismiss={dismiss} />
      <PageHeader
        title="Risk & Exposure"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Intelligence" }, { label: "Risk & Exposure" }]}
      />

      <div style={metricsRow}>
        <MetricCard title="Devices Scored" value={data?.summary.devices_scored ?? "--"} />
        <MetricCard
          title="Need Attention"
          value={data?.summary.attention_required ?? "--"}
          trend={data && data.summary.attention_required > 0 ? "down" : "flat"}
        />
        <MetricCard title="With CVEs" value={data?.summary.devices_with_cves ?? "--"} />
        <MetricCard
          title="Awaiting Approval"
          value={data?.summary.actions_awaiting_approval ?? "--"}
        />
        <MetricCard
          title="Insufficient Data"
          value={data?.summary.insufficient_data_count ?? "--"}
        />
      </div>

      {/* Site rollup: where to look first. Clicking scopes the list, which
          is the same narrowing a site-scoped agent gets from ?site_id=. */}
      {data && data.sites.length > 0 && (
        <>
          <div style={sectionTitle}>Sites</div>
          <div style={siteGrid}>
            {data.sites.map((s) => (
              <button
                key={s.site_id}
                style={siteCard(siteFilter === s.site_id)}
                onClick={() => {
                  setSiteFilter(siteFilter === s.site_id ? "" : s.site_id);
                  setPage(1);
                }}
              >
                <div style={siteName}>{s.site_name || s.site_id || "Unassigned"}</div>
                <div style={{ display: "flex", gap: "0.375rem", flexWrap: "wrap" }}>
                  {s.needs_attention > 0 ? (
                    <StatusBadge
                      status={`${s.needs_attention} need attention`}
                      variant="critical"
                      size="sm"
                    />
                  ) : (
                    <StatusBadge status="nothing needs attention" variant="success" size="sm" />
                  )}
                  {s.by_band.high > 0 && (
                    <StatusBadge status={`${s.by_band.high} high risk`} variant="warning" size="sm" />
                  )}
                </div>
                <div style={siteMeta}>
                  <span>{s.device_count} devices</span>
                  {s.cve_count > 0 && <span>{s.cve_count} CVE matches</span>}
                  {s.pending_approvals > 0 && <span>{s.pending_approvals} awaiting approval</span>}
                </div>
              </button>
            ))}
          </div>
        </>
      )}

      <FilterBar
        filters={filterDefs}
        values={filters}
        onChange={(k, v) => { setFilters((p) => ({ ...p, [k]: v })); setPage(1); }}
        onClear={() => { setFilters({ band: "" }); setSiteFilter(""); setPage(1); }}
      />

      {!loading && items.length === 0 ? (
        <EmptyState
          title={siteFilter || filters.band ? "Nothing matches this filter" : "Nothing needs attention"}
          description={
            siteFilter || filters.band
              ? "Clear the filters to see the whole fleet."
              : "Devices appear here once agents report outcomes or a CVE feed is loaded."
          }
          icon="&#x26A0;"
        />
      ) : (
        <DataTable<AttentionItem>
          columns={columns}
          data={paged}
          loading={loading}
          emptyMessage="No devices match"
          page={page}
          pageSize={PAGE_SIZE}
          total={items.length}
          onPageChange={setPage}
          onRowClick={openDetail}
          striped
        />
      )}

      {/* ── Why / evidence / what next ─────────────── */}
      <DetailPanel
        open={detailOpen}
        onClose={() => { setDetailOpen(false); setSelected(null); }}
        title={selected?.agent_name || selected?.agent_id || "Device"}
        subtitle={selected ? `${selected.vendor} ${selected.model} · ${selected.site_name}` : undefined}
        width={520}
      >
        {selected && (
          <>
            <div style={sectionTitle}>Assessment</div>
            <div style={detailRow}>
              <span style={detailLabel}>Why it ranks here</span>
              <span style={detailValue}>
                <StatusBadge
                  status={selected.attention_driver_label}
                  variant={DRIVER_VARIANT[selected.attention_driver] ?? "neutral"}
                  size="sm"
                />
              </span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Predicted risk band</span>
              <span style={detailValue}>
                <StatusBadge
                  status={BAND_LABEL[selected.band] ?? selected.band}
                  variant={BAND_VARIANT[selected.band] ?? "neutral"}
                  size="sm"
                />
              </span>
            </div>
            {selected.band !== "insufficient_data" && (
              <div style={detailRow}>
                <span style={detailLabel}>Score</span>
                <span style={detailValue}>{Math.round(selected.risk_score * 100)}%</span>
              </div>
            )}
            <div style={detailRow}>
              <span style={detailLabel}>Current health</span>
              <span style={detailValue}>{selected.health}</span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Evidence basis</span>
              <span style={detailValue}>
                {selected.confidence.basis.replace(/_/g, " ")}
                {" "}({selected.confidence.sample_count} outcomes)
              </span>
            </div>
            <p style={{ fontSize: "0.8125rem", color: "var(--text-secondary)", marginTop: "0.5rem" }}>
              {selected.confidence.explanation}
            </p>

            <div style={sectionTitle}>Why this matters</div>
            <ul style={reasonList}>
              {selected.reasons.map((r, i) => <li key={i}>{r}</li>)}
            </ul>

            {selected.evidence.cves.length > 0 && (
              <>
                <div style={sectionTitle}>CVE Exposure ({selected.evidence.cves.length})</div>
                {selected.evidence.cves.map((c, i) => (
                  <div key={`${c.cve_id}-${i}`} style={detailRow}>
                    <span style={detailLabel}>
                      <code>{c.cve_id}</code>
                      <span style={{ color: "var(--text-muted)", marginLeft: "0.5rem" }}>
                        {c.component} {c.version}
                      </span>
                    </span>
                    <span style={detailValue}>
                      <StatusBadge
                        status={c.severity || "unknown"}
                        variant={CVE_VARIANT[(c.severity || "").toLowerCase()] ?? "neutral"}
                        size="sm"
                      />
                      {c.fixed_version && (
                        <div style={{ fontSize: "0.72rem", color: "var(--text-muted)" }}>
                          fixed in {c.fixed_version}
                        </div>
                      )}
                    </span>
                  </div>
                ))}
              </>
            )}

            {selected.evidence.fleet_patterns.length > 0 && (
              <>
                <div style={sectionTitle}>Fleet Signals</div>
                {selected.evidence.fleet_patterns.map((p) => (
                  <div key={p.pattern_id} style={detailRow}>
                    <span style={detailLabel}>{p.pattern_type.replace(/_/g, " ")}</span>
                    <span style={detailValue}>{p.description}</span>
                  </div>
                ))}
              </>
            )}

            <div style={sectionTitle}>What should happen next</div>
            <div style={recBox(selected.recommended_next.available)}>
              <strong>
                {CAPABILITY_LABEL[selected.recommended_next.capability]
                  ?? selected.recommended_next.capability}
              </strong>
              <div style={{ marginTop: "0.25rem", color: "var(--text-secondary)" }}>
                {selected.recommended_next.summary}
              </div>
              {selected.recommended_next.requires_approval && (
                <div style={{ marginTop: "0.375rem", fontSize: "0.75rem" }}>
                  A named human must approve this before anything runs.
                </div>
              )}
              {!selected.recommended_next.available &&
                selected.recommended_next.unavailable_reason && (
                  <div style={{ marginTop: "0.375rem", fontSize: "0.75rem", color: "var(--text-muted)" }}>
                    {selected.recommended_next.unavailable_reason}
                  </div>
                )}
              {selected.current_state.pending_approvals.length > 0 && (
                <div style={{ marginTop: "0.5rem" }}>
                  <Link to={`/t/${tenantId}/approvals`}>
                    Go to the approval queue
                  </Link>
                </div>
              )}
              {selected.recommended_next.capability === "investigate_device" &&
                selected.device_id && (
                  <div style={{ marginTop: "0.5rem" }}>
                    <Link to={`/t/${tenantId}/fleet`}>Open in fleet</Link>
                  </div>
                )}
            </div>
          </>
        )}
      </DetailPanel>
    </div>
  );
}
