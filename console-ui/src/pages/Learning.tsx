import { useParams } from "react-router-dom";
import { type CSSProperties, useCallback, useEffect, useState } from "react";
import PageHeader from "../components/PageHeader";
import DataTable, { type Column } from "../components/DataTable";
import DetailPanel from "../components/DetailPanel";
import MetricCard from "../components/MetricCard";
import StatusBadge from "../components/StatusBadge";
import EmptyState from "../components/EmptyState";
import Toast from "../components/Toast";
import { useToast } from "../components/useToast";
import { getJson } from "../api";

/* S3 — Learning: the governance surface over the learning substrate.
 *
 * The page is NOT the product; the durable contract is. What this makes
 * visible is the chain, with each concept kept distinct rather than
 * collapsed into "AI learning":
 *
 *   outcome  -> pattern -> learned signal -> candidate capability
 *            -> governed promotion -> influence on future decisions
 *
 * Nothing here promotes anything. Promotion stays governed by the
 * marketplace human review path. */

interface LearningCycle {
  cycle_id: string;
  pattern_id: string;
  pattern_type: string;
  skill_id: string | null;
  sites_distributed: number;
  devices_applied: number;
  outcomes_before: Record<string, number>;
  outcomes_after: Record<string, number>;
  improvement_pct: number | null;
  promotion_recommended: boolean;
  status: string;
  started_at: string | null;
  completed_at: string | null;
}

interface LearnedSignal {
  signal_key: string;
  scope_type: string;
  scope_ref: string;
  action_type: string;
  vendor: string;
  model: string;
  statement: string;
  evidence: Record<string, unknown>;
  confidence: number;
  observation_count: number;
  source_pattern_id: string;
  source_cycle_id: string | null;
  last_confirmed_at: string | null;
}

interface Candidate {
  skill_id: string;
  source_device: string;
  source_component: string;
  validation_state: string;
  warnings: string[];
  dry_run_matches: number;
  status: string;
  cycle_id: string | null;
  generated_at: string;
  yaml_text: string;
}

const STAGE_HELP: Record<string, string> = {
  signals:
    "Durable knowledge derived from real outcomes. Scoped to the site or hardware model the evidence actually supports — never assumed fleet-wide. This is what now informs Risk & Exposure.",
  cycles:
    "The learning process itself: a pattern was detected, a candidate capability may have been produced, distribution was measured, and improvement was recorded. Survives restarts.",
  candidates:
    "Proposed reusable behaviours generated from novel resolutions. Validated but unproven — a candidate is never distributed automatically.",
};

const SCOPE_LABEL: Record<string, string> = {
  cohort: "hardware model",
  site: "site",
};

const STATUS_VARIANT: Record<string, "success" | "warning" | "info" | "neutral"> = {
  open: "neutral",
  measuring: "info",
  promotion_recommended: "warning",
  closed: "success",
};

const tabRow: CSSProperties = {
  display: "flex", gap: "0.5rem", marginBottom: "0.75rem", flexWrap: "wrap",
};

const tabBtn = (active: boolean): CSSProperties => ({
  padding: "0.4rem 0.9rem",
  borderRadius: "var(--radius-md)",
  border: `1px solid ${active ? "var(--color-primary, #0E7A73)" : "var(--border-light)"}`,
  background: active ? "var(--color-primary, #0E7A73)" : "var(--bg-card)",
  color: active ? "#fff" : "inherit",
  cursor: "pointer",
  font: "inherit",
  fontSize: "0.8125rem",
  fontWeight: 600,
});

const helpText: CSSProperties = {
  fontSize: "0.8125rem", color: "var(--text-secondary)", marginBottom: "1rem",
  maxWidth: "68ch", lineHeight: 1.6,
};

const metricsRow: CSSProperties = {
  display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(190px, 1fr))",
  gap: "1rem", marginBottom: "1.5rem",
};

const detailRow: CSSProperties = {
  display: "flex", justifyContent: "space-between", padding: "0.375rem 0",
  fontSize: "0.8125rem", borderBottom: "1px solid var(--border-light)",
};
const detailLabel: CSSProperties = { color: "var(--text-secondary)", fontWeight: 500 };
const detailValue: CSSProperties = {
  color: "var(--text-primary)", fontWeight: 500, textAlign: "right",
};
const sectionTitle: CSSProperties = {
  fontSize: "0.8125rem", fontWeight: 600, color: "var(--text-secondary)",
  textTransform: "uppercase", letterSpacing: "0.04em", marginBottom: "0.75rem",
  marginTop: "1.25rem", borderBottom: "1px solid var(--border-light)",
  paddingBottom: "0.375rem",
};
const codeBlock: CSSProperties = {
  background: "var(--bg-primary)", borderRadius: "var(--radius-sm)",
  padding: "0.75rem", fontSize: "0.72rem",
  fontFamily: "var(--font-mono, monospace)", whiteSpace: "pre-wrap",
  maxHeight: 260, overflow: "auto", marginTop: "0.5rem",
};

function fmtDate(iso: string | null): string {
  if (!iso) return "--";
  return new Date(iso).toLocaleString("en-US", {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

type Tab = "signals" | "cycles" | "candidates";

export default function Learning() {
  const { tenantId = "" } = useParams<{ tenantId: string }>();
  const { toasts, toast, dismiss } = useToast();

  const [tab, setTab] = useState<Tab>("signals");
  const [signals, setSignals] = useState<LearnedSignal[]>([]);
  const [cycles, setCycles] = useState<LearningCycle[]>([]);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [loading, setLoading] = useState(true);
  const [selectedSignal, setSelectedSignal] = useState<LearnedSignal | null>(null);
  const [selectedCandidate, setSelectedCandidate] = useState<Candidate | null>(null);

  const fetchAll = useCallback(async () => {
    try {
      const [s, c, k] = await Promise.all([
        getJson<{ signals: LearnedSignal[] }>(`/api/t/${tenantId}/learning/signals`),
        getJson<{ cycles: LearningCycle[] }>(`/api/t/${tenantId}/learning/cycles`),
        getJson<{ candidates: Candidate[] }>(`/api/t/${tenantId}/learning/candidates`),
      ]);
      setSignals(s.signals ?? []);
      setCycles(c.cycles ?? []);
      setCandidates(k.candidates ?? []);
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to load learning data", "error");
    } finally {
      setLoading(false);
    }
  }, [tenantId, toast]);

  useEffect(() => { setLoading(true); void fetchAll(); }, [fetchAll]);

  const signalColumns: Column<LearnedSignal>[] = [
    {
      key: "statement",
      header: "What was learned",
      render: (r) => <span style={{ fontSize: "0.8125rem" }}>{r.statement}</span>,
    },
    {
      key: "scope_type",
      header: "Applies to",
      render: (r) => (
        <span style={{ fontSize: "0.8125rem" }}>
          {SCOPE_LABEL[r.scope_type] ?? r.scope_type}
          <div style={{ fontSize: "0.72rem", color: "var(--text-muted)" }}>
            {r.scope_type === "cohort" ? `${r.vendor} ${r.model}` : r.scope_ref}
          </div>
        </span>
      ),
    },
    {
      key: "confidence",
      header: "Confidence",
      render: (r) => `${Math.round(r.confidence * 100)}%`,
    },
    { key: "observation_count", header: "Times seen" },
    {
      key: "last_confirmed_at",
      header: "Last confirmed",
      render: (r) => fmtDate(r.last_confirmed_at),
    },
  ];

  const cycleColumns: Column<LearningCycle>[] = [
    { key: "pattern_type", header: "Pattern", render: (r) => r.pattern_type.replace(/_/g, " ") },
    {
      key: "status",
      header: "Stage",
      render: (r) => (
        <StatusBadge
          status={r.status.replace(/_/g, " ")}
          variant={STATUS_VARIANT[r.status] ?? "neutral"}
          size="sm"
        />
      ),
    },
    { key: "skill_id", header: "Candidate", render: (r) => r.skill_id ?? "none yet" },
    { key: "devices_applied", header: "Devices reached" },
    {
      key: "improvement_pct",
      header: "Measured change",
      render: (r) =>
        r.improvement_pct == null ? "not yet measured" : `${r.improvement_pct.toFixed(1)}%`,
    },
    { key: "started_at", header: "Started", render: (r) => fmtDate(r.started_at) },
  ];

  const candidateColumns: Column<Candidate>[] = [
    { key: "skill_id", header: "Candidate capability" },
    { key: "source_component", header: "Learned from" },
    {
      key: "validation_state",
      header: "Validation",
      render: (r) => (
        <StatusBadge status={r.validation_state} variant="info" size="sm" />
      ),
    },
    {
      key: "status",
      header: "Promotion",
      render: (r) => (
        <StatusBadge
          status={r.status === "promoted" ? "recommended for review" : r.status}
          variant={r.status === "promoted" ? "warning" : "neutral"}
          size="sm"
        />
      ),
    },
    { key: "generated_at", header: "Generated", render: (r) => fmtDate(r.generated_at) },
  ];

  const promotionCandidates = candidates.filter((c) => c.status === "promoted").length;

  return (
    <div>
      <Toast toasts={toasts} onDismiss={dismiss} />
      <PageHeader
        title="Learning"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Intelligence" }, { label: "Learning" }]}
      />

      <div style={metricsRow}>
        <MetricCard title="Learned Signals" value={signals.length} />
        <MetricCard title="Learning Cycles" value={cycles.length} />
        <MetricCard title="Candidate Capabilities" value={candidates.length} />
        <MetricCard title="Awaiting Review" value={promotionCandidates} />
      </div>

      <div style={tabRow}>
        <button style={tabBtn(tab === "signals")} onClick={() => setTab("signals")}>
          What was learned
        </button>
        <button style={tabBtn(tab === "cycles")} onClick={() => setTab("cycles")}>
          How it was learned
        </button>
        <button style={tabBtn(tab === "candidates")} onClick={() => setTab("candidates")}>
          Candidate capabilities
        </button>
      </div>

      <p style={helpText}>{STAGE_HELP[tab]}</p>

      {tab === "signals" && (
        signals.length === 0 && !loading ? (
          <EmptyState
            title="Nothing learned yet"
            description="Learned signals appear once actions produce enough real outcomes for a pattern to emerge."
            icon="&#x2726;"
          />
        ) : (
          <DataTable<LearnedSignal>
            columns={signalColumns} data={signals} loading={loading}
            emptyMessage="No learned signals"
            onRowClick={(r) => setSelectedSignal(r)} striped
          />
        )
      )}

      {tab === "cycles" && (
        cycles.length === 0 && !loading ? (
          <EmptyState
            title="No learning cycles yet"
            description="A cycle opens when the fleet detects a recurring pattern in real outcomes."
            icon="&#x21BB;"
          />
        ) : (
          <DataTable<LearningCycle>
            columns={cycleColumns} data={cycles} loading={loading}
            emptyMessage="No cycles" striped
          />
        )
      )}

      {tab === "candidates" && (
        candidates.length === 0 && !loading ? (
          <EmptyState
            title="No candidate capabilities"
            description="Candidates are generated when a novel resolution is diagnosed and validated."
            icon="&#x2699;"
          />
        ) : (
          <DataTable<Candidate>
            columns={candidateColumns} data={candidates} loading={loading}
            emptyMessage="No candidates"
            onRowClick={(r) => setSelectedCandidate(r)} striped
          />
        )
      )}

      {/* ── Learned signal detail ─────────────────── */}
      <DetailPanel
        open={selectedSignal !== null}
        onClose={() => setSelectedSignal(null)}
        title="Learned signal"
        subtitle={selectedSignal?.action_type}
        width={520}
      >
        {selectedSignal && (
          <>
            <p style={{ fontSize: "0.9rem", lineHeight: 1.6 }}>
              {selectedSignal.statement}
            </p>

            <div style={sectionTitle}>Scope</div>
            <div style={detailRow}>
              <span style={detailLabel}>Applies to</span>
              <span style={detailValue}>
                {SCOPE_LABEL[selectedSignal.scope_type] ?? selectedSignal.scope_type}
              </span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>
                {selectedSignal.scope_type === "cohort" ? "Hardware" : "Site"}
              </span>
              <span style={detailValue}>
                {selectedSignal.scope_type === "cohort"
                  ? `${selectedSignal.vendor} ${selectedSignal.model}`
                  : selectedSignal.scope_ref}
              </span>
            </div>
            <p style={{ fontSize: "0.75rem", color: "var(--text-muted)", marginTop: "0.5rem" }}>
              Scope is bound to the evidence. This signal is not applied to
              hardware or sites its evidence does not cover.
            </p>

            <div style={sectionTitle}>Evidence</div>
            <div style={detailRow}>
              <span style={detailLabel}>Confidence</span>
              <span style={detailValue}>
                {Math.round(selectedSignal.confidence * 100)}%
              </span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Times confirmed</span>
              <span style={detailValue}>{selectedSignal.observation_count}</span>
            </div>
            {Object.entries(selectedSignal.evidence).map(([k, v]) => (
              <div key={k} style={detailRow}>
                <span style={detailLabel}>{k.replace(/_/g, " ")}</span>
                <span style={detailValue}>
                  {typeof v === "object" ? JSON.stringify(v) : String(v)}
                </span>
              </div>
            ))}

            <div style={sectionTitle}>Provenance</div>
            <div style={detailRow}>
              <span style={detailLabel}>From pattern</span>
              <span style={detailValue}>
                <code style={{ fontSize: "0.72rem" }}>
                  {selectedSignal.source_pattern_id}
                </code>
              </span>
            </div>
            {selectedSignal.source_cycle_id && (
              <div style={detailRow}>
                <span style={detailLabel}>Learning cycle</span>
                <span style={detailValue}>
                  <code style={{ fontSize: "0.72rem" }}>
                    {selectedSignal.source_cycle_id}
                  </code>
                </span>
              </div>
            )}
            <p style={{ fontSize: "0.75rem", color: "var(--text-muted)", marginTop: "0.75rem" }}>
              This knowledge now appears as evidence on Risk &amp; Exposure for
              the devices it covers. It informs decisions; it does not
              authorise any action.
            </p>
          </>
        )}
      </DetailPanel>

      {/* ── Candidate detail ──────────────────────── */}
      <DetailPanel
        open={selectedCandidate !== null}
        onClose={() => setSelectedCandidate(null)}
        title="Candidate capability"
        subtitle={selectedCandidate?.skill_id}
        width={560}
      >
        {selectedCandidate && (
          <>
            <div style={sectionTitle}>Origin</div>
            <div style={detailRow}>
              <span style={detailLabel}>Learned from device</span>
              <span style={detailValue}>{selectedCandidate.source_device}</span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Component</span>
              <span style={detailValue}>{selectedCandidate.source_component}</span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Validation</span>
              <span style={detailValue}>{selectedCandidate.validation_state}</span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Dry-run matches</span>
              <span style={detailValue}>{selectedCandidate.dry_run_matches}</span>
            </div>

            {selectedCandidate.warnings.length > 0 && (
              <>
                <div style={sectionTitle}>Warnings</div>
                <ul style={{ fontSize: "0.8125rem", paddingLeft: "1.1rem" }}>
                  {selectedCandidate.warnings.map((w, i) => <li key={i}>{w}</li>)}
                </ul>
              </>
            )}

            <div style={sectionTitle}>Promotion</div>
            <p style={{ fontSize: "0.8125rem", lineHeight: 1.6 }}>
              {selectedCandidate.status === "promoted"
                ? "This candidate has met the evidence bar and is recommended for review. It is not distributed: a human reviews and promotes it through the Skill Marketplace."
                : "Not yet recommended. A candidate is never distributed automatically — promotion is always a governed human decision."}
            </p>

            <div style={sectionTitle}>Proposed behaviour</div>
            <div style={codeBlock}>{selectedCandidate.yaml_text}</div>
          </>
        )}
      </DetailPanel>
    </div>
  );
}
