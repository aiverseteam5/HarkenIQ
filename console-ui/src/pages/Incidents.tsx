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
import Spinner from "../components/Spinner";
import { useToast } from "../components/useToast";
import { getJson } from "../api";

/* S4 — Incidents & Diagnosis.
 *
 * Real incidents from the Site Manager that consolidated them, with the
 * diagnosis attached. The page renders the capability; it derives nothing.
 *
 * The diagnosis is presented WITH its provenance. When it came from the
 * language model it is shown as an interpretation of evidence, not as
 * fact, because that text was generated from device telemetry. */

interface Diagnosis {
  origin: string;
  trust: string;
  confidence: number;
  generated: {
    summary: string;
    suggested_action: string;
    reasoning_steps: string[];
  };
  evidence_cited: string[];
  similar_past_incidents: { title?: string; resolution?: string }[];
}

interface Incident {
  incident_id: string;
  kind: string;
  status: string;
  title: string;
  device_agent_id: string;
  subsystem: string;
  site_id: string;
  site_name: string;
  parent_incident_id: string | null;
  is_parent: boolean;
  confidence: number;
  inferred: boolean;
  correlation: Record<string, unknown>;
  diagnosis: Diagnosis | null;
  opened_at: string | null;
  resolved_at: string | null;
  children: Incident[];
  child_count: number;
}

interface LearnedSignal {
  statement: string;
  scope_type: string;
  confidence: number;
  observation_count: number;
}

interface IncidentDetail extends Incident {
  prior_learning: LearnedSignal[];
  current_state: {
    pending_approvals: { action_id: string; action_type: string }[];
    open_action_count: number;
  };
  recommended_next: {
    capability: string;
    summary: string;
    requires_approval: boolean;
    available: boolean;
    unavailable_reason?: string;
    refs: string[];
  };
}

const KIND_LABEL: Record<string, string> = {
  device: "Single device",
  shared_power: "Shared power",
  rack_thermal: "Rack thermal",
  batch_component: "Batch component",
  network_ambiguity: "Network ambiguity",
  tor_connectivity: "Top-of-rack connectivity",
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
const metricsRow: CSSProperties = {
  display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(190px, 1fr))",
  gap: "1rem", marginBottom: "1.5rem",
};
const diagnosisBox: CSSProperties = {
  padding: "0.875rem 1rem", background: "var(--bg-primary)",
  borderRadius: "var(--radius-md)", borderLeft: "3px solid var(--color-primary, #0E7A73)",
  marginTop: "0.5rem", fontSize: "0.875rem", lineHeight: 1.6,
};
const provenanceNote: CSSProperties = {
  fontSize: "0.72rem", color: "var(--text-muted)", marginTop: "0.5rem",
  fontStyle: "italic",
};
const childRow: CSSProperties = {
  padding: "0.5rem 0.75rem", borderLeft: "2px solid var(--border-light)",
  marginLeft: "0.5rem", fontSize: "0.8125rem",
};

function fmtDate(iso: string | null): string {
  if (!iso) return "--";
  return new Date(iso).toLocaleString("en-US", {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

export default function Incidents() {
  const { tenantId = "" } = useParams<{ tenantId: string }>();
  const { toasts, toast, dismiss } = useToast();

  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [diagnosedCount, setDiagnosedCount] = useState(0);
  const [loading, setLoading] = useState(true);
  const [filters, setFilters] = useState<Record<string, string>>({ status: "open" });
  const [detail, setDetail] = useState<IncidentDetail | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);

  const fetchIncidents = useCallback(async () => {
    try {
      const params = new URLSearchParams();
      params.set("status", filters.status || "open");
      const res = await getJson<{ incidents: Incident[]; diagnosed: number }>(
        `/api/t/${tenantId}/incidents?${params.toString()}`,
      );
      setIncidents(res.incidents ?? []);
      setDiagnosedCount(res.diagnosed ?? 0);
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to load incidents", "error");
    } finally {
      setLoading(false);
    }
  }, [tenantId, filters.status, toast]);

  useEffect(() => { setLoading(true); void fetchIncidents(); }, [fetchIncidents]);
  useEffect(() => {
    const t = setInterval(() => void fetchIncidents(), 30000);
    return () => clearInterval(t);
  }, [fetchIncidents]);

  const openDetail = useCallback(async (inc: Incident) => {
    setDetailOpen(true);
    setDetailLoading(true);
    try {
      setDetail(await getJson<IncidentDetail>(
        `/api/t/${tenantId}/incidents/${inc.incident_id}`,
      ));
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to load incident", "error");
      setDetailOpen(false);
    } finally {
      setDetailLoading(false);
    }
  }, [tenantId, toast]);

  const filterDefs = useMemo<FilterDef[]>(() => [
    {
      key: "status", label: "Status", type: "select",
      options: [
        { value: "open", label: "Open" },
        { value: "resolved", label: "Resolved" },
        { value: "all", label: "All" },
      ],
    },
  ], []);

  const columns = useMemo<Column<Incident>[]>(() => [
    {
      key: "title",
      header: "Incident",
      render: (r) => (
        <span>
          {r.title}
          <div style={{ fontSize: "0.72rem", color: "var(--text-muted)" }}>
            {KIND_LABEL[r.kind] ?? r.kind}
            {r.child_count > 0 && ` · ${r.child_count} affected devices`}
            {r.inferred && " · inferred domain"}
          </div>
        </span>
      ),
    },
    { key: "site_name", header: "Site", render: (r) => r.site_name || "--" },
    {
      key: "diagnosis",
      header: "Diagnosis",
      render: (r) =>
        r.diagnosis ? (
          <StatusBadge status="explained" variant="info" size="sm" />
        ) : (
          <span style={{ fontSize: "0.8125rem", color: "var(--text-muted)" }}>
            gathering evidence
          </span>
        ),
    },
    {
      key: "status",
      header: "Status",
      render: (r) => (
        <StatusBadge
          status={r.status}
          variant={r.status === "open" ? "warning" : "success"}
          size="sm"
        />
      ),
    },
    { key: "opened_at", header: "Opened", render: (r) => fmtDate(r.opened_at) },
  ], []);

  return (
    <div>
      <Toast toasts={toasts} onDismiss={dismiss} />
      <PageHeader
        title="Incidents & Diagnosis"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Command" }, { label: "Incidents" }]}
      />

      <div style={metricsRow}>
        <MetricCard title="Incidents" value={incidents.length} />
        <MetricCard title="Explained" value={diagnosedCount} />
        <MetricCard
          title="Correlated"
          value={incidents.filter((i) => i.child_count > 0).length}
        />
      </div>

      <FilterBar
        filters={filterDefs}
        values={filters}
        onChange={(k, v) => setFilters((p) => ({ ...p, [k]: v }))}
        onClear={() => setFilters({ status: "open" })}
      />

      {!loading && incidents.length === 0 ? (
        <EmptyState
          title={filters.status === "open" ? "No open incidents" : "No incidents"}
          description="Incidents appear when a Harken Node detects a fault and the Site Manager correlates it."
          icon="&#x26A1;"
        />
      ) : (
        <DataTable<Incident>
          columns={columns} data={incidents} loading={loading}
          emptyMessage="No incidents" onRowClick={openDetail} striped
        />
      )}

      <DetailPanel
        open={detailOpen}
        onClose={() => { setDetailOpen(false); setDetail(null); }}
        title={detail?.title ?? "Incident"}
        subtitle={detail ? `${KIND_LABEL[detail.kind] ?? detail.kind} · ${detail.site_name}` : undefined}
        width={560}
      >
        {detailLoading ? (
          <div style={{ display: "flex", justifyContent: "center", padding: "2rem" }}>
            <Spinner size="md" />
          </div>
        ) : detail ? (
          <>
            <div style={sectionTitle}>What happened</div>
            <div style={detailRow}>
              <span style={detailLabel}>Status</span>
              <span style={detailValue}>
                <StatusBadge
                  status={detail.status}
                  variant={detail.status === "open" ? "warning" : "success"}
                  size="sm"
                />
              </span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Device</span>
              <span style={detailValue}>{detail.device_agent_id || "multiple"}</span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Subsystem</span>
              <span style={detailValue}>{detail.subsystem || "--"}</span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Opened</span>
              <span style={detailValue}>{fmtDate(detail.opened_at)}</span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Correlation confidence</span>
              <span style={detailValue}>
                {Math.round(detail.confidence * 100)}%
                {detail.inferred && (
                  <div style={{ fontSize: "0.72rem", color: "var(--text-muted)" }}>
                    fault domain inferred, not confirmed
                  </div>
                )}
              </span>
            </div>

            {detail.child_count > 0 && (
              <>
                <div style={sectionTitle}>Affected devices ({detail.child_count})</div>
                <p style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>
                  These are one incident, not {detail.child_count}. The Site
                  Manager correlated them to a shared cause.
                </p>
                {detail.children.map((c) => (
                  <div key={c.incident_id} style={childRow}>
                    <strong>{c.device_agent_id}</strong> — {c.title}
                  </div>
                ))}
              </>
            )}

            <div style={sectionTitle}>Why</div>
            {detail.diagnosis ? (
              <>
                <div style={diagnosisBox}>
                  {detail.diagnosis.generated.summary || "No summary produced."}
                </div>
                {detail.diagnosis.trust === "untrusted_generated" && (
                  <div style={provenanceNote}>
                    Written by the reasoning model from this device's telemetry
                    ({Math.round(detail.diagnosis.confidence * 100)}% confidence).
                    Treat it as an interpretation of the evidence below, not as
                    established fact.
                  </div>
                )}

                {detail.diagnosis.evidence_cited.length > 0 && (
                  <>
                    <div style={sectionTitle}>Evidence cited</div>
                    <ul style={{ fontSize: "0.8125rem", paddingLeft: "1.1rem", lineHeight: 1.6 }}>
                      {detail.diagnosis.evidence_cited.map((e, i) => <li key={i}>{e}</li>)}
                    </ul>
                  </>
                )}

                {detail.diagnosis.generated.reasoning_steps.length > 0 && (
                  <>
                    <div style={sectionTitle}>Reasoning</div>
                    <ol style={{ fontSize: "0.8125rem", paddingLeft: "1.2rem", lineHeight: 1.6 }}>
                      {detail.diagnosis.generated.reasoning_steps.map((s, i) => (
                        <li key={i}>{s}</li>
                      ))}
                    </ol>
                  </>
                )}
              </>
            ) : (
              <p style={{ fontSize: "0.8125rem", color: "var(--text-secondary)" }}>
                No diagnosis yet. The Site Manager explains an incident once it
                has enough evidence; until then this records what was detected.
              </p>
            )}

            {detail.prior_learning.length > 0 && (
              <>
                <div style={sectionTitle}>What the fleet already knows</div>
                {detail.prior_learning.map((s, i) => (
                  <div key={i} style={detailRow}>
                    <span style={{ ...detailLabel, maxWidth: "70%" }}>{s.statement}</span>
                    <span style={detailValue}>
                      {Math.round(s.confidence * 100)}%
                      <div style={{ fontSize: "0.72rem", color: "var(--text-muted)" }}>
                        seen {s.observation_count}x
                      </div>
                    </span>
                  </div>
                ))}
              </>
            )}

            <div style={sectionTitle}>What should happen next</div>
            <div style={diagnosisBox}>
              <strong>{detail.recommended_next.summary}</strong>
              {detail.recommended_next.requires_approval && (
                <div style={{ marginTop: "0.375rem", fontSize: "0.8125rem" }}>
                  A named human must approve this before anything runs.
                </div>
              )}
              {!detail.recommended_next.available &&
                detail.recommended_next.unavailable_reason && (
                  <div style={provenanceNote}>
                    {detail.recommended_next.unavailable_reason}
                  </div>
                )}
              {detail.current_state.open_action_count > 0 && (
                <div style={{ marginTop: "0.5rem" }}>
                  <Link to={`/t/${tenantId}/approvals`}>Go to the approval queue</Link>
                </div>
              )}
            </div>
          </>
        ) : null}
      </DetailPanel>
    </div>
  );
}
