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
import { getJson, postJson } from "../api";

/* S6 — Campaigns: governed capability orchestration across an estate.
 *
 * This page is a CONSUMER. It declares no capability (the Registry owns
 * that), plans no wave (the Site Manager owns that, from fault domains
 * Central Command never sees), and decides no approval (the one
 * /api/approvals queue owns that, per site-wave).
 *
 * The three things it must never blur:
 *   1. "Not implemented" and "not currently permitted" are different
 *      problems. The first excludes a device; the second warns and needs
 *      a named human to accept or exclude it.
 *   2. APPROVED is not EXECUTABLE. A wave is revalidated at dispatch and
 *      may be narrowed or refused outright.
 *   3. A halted site is not a halted campaign. Partial success is a real
 *      outcome and is shown as one. */

interface Progress {
  sites_total: number;
  sites_completed: number;
  sites_halted: number;
  sites_running: number;
  sites_pending: number;
  targets_total: number;
  targets_by_applicability: Record<string, number>;
  partial_success: boolean;
}

interface Campaign {
  id: string;
  name: string;
  description: string;
  action_type: string;
  status: string;
  version: number;
  actor: string;
  created_by: string;
  created_at: string | null;
  preflight_at: string | null;
  acknowledged_by: string | null;
  acknowledgement_valid: boolean;
  halt_reason: string | null;
  progress: Progress;
}

interface Target {
  device_agent_id: string;
  device_name: string;
  device_class: string;
  site_id: string;
  applicability: string;
  reason: string | null;
  status: string;
  revalidation: string | null;
  revalidation_reason: string | null;
}

interface Wave {
  site_id: string;
  wave_index: number;
  device_agent_ids: string[];
  domain_span: number;
  plan_hash: string;
  subject_ref: string | null;
  status: string;
  void_reason: string | null;
  decided_by: string | null;
}

interface Site {
  site_id: string;
  site_name: string;
  status: string;
  current_wave: number;
  wave_count: number;
  halt_reason: string | null;
}

interface Detail extends Campaign {
  sites: Site[];
  targets: Target[];
}

const STATUS_VARIANT: Record<
  string,
  "success" | "warning" | "critical" | "info" | "neutral"
> = {
  draft: "neutral",
  preflighted: "info",
  acknowledged: "info",
  awaiting_approval: "warning",
  running: "info",
  completed: "success",
  halted: "critical",
  cancelled: "neutral",
};

const APPLICABILITY_LABEL: Record<string, string> = {
  eligible: "Eligible",
  warn_not_permitted: "Not permitted by node policy",
  unknown: "Capabilities undeclared",
  excluded_unimplemented: "No executor implements it",
  excluded_by_operator: "Excluded by an operator",
};

const APPLICABILITY_VARIANT: Record<
  string,
  "success" | "warning" | "critical" | "neutral"
> = {
  eligible: "success",
  warn_not_permitted: "warning",
  unknown: "neutral",
  excluded_unimplemented: "critical",
  excluded_by_operator: "neutral",
};

const WAVE_VARIANT: Record<
  string,
  "success" | "warning" | "critical" | "info" | "neutral"
> = {
  autonomous: "info",
  pending_approval: "warning",
  approved: "success",
  denied: "critical",
  voided: "critical",
  dispatched: "info",
  completed: "success",
  failed: "critical",
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
const mono: CSSProperties = {
  fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
  fontSize: "0.72rem",
};

export default function Campaigns() {
  const { tenantId = "" } = useParams<{ tenantId: string }>();
  const { toasts, toast, dismiss } = useToast();

  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<Detail | null>(null);
  const [waves, setWaves] = useState<Wave[]>([]);
  const [busy, setBusy] = useState(false);

  const fetchAll = useCallback(async () => {
    try {
      const res = await getJson<{ campaigns: Campaign[] }>(
        `/api/t/${tenantId}/campaigns/`,
      );
      setCampaigns(res.campaigns ?? []);
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to load campaigns", "error");
    } finally {
      setLoading(false);
    }
  }, [tenantId, toast]);

  useEffect(() => {
    setLoading(true);
    void fetchAll();
  }, [fetchAll]);

  const open = async (row: Campaign) => {
    try {
      const detail = await getJson<Detail>(`/api/t/${tenantId}/campaigns/${row.id}`);
      setSelected(detail);
      const w = await getJson<{ waves: Wave[] }>(
        `/api/t/${tenantId}/campaigns/${row.id}/waves`,
      );
      setWaves(w.waves ?? []);
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to load campaign", "error");
    }
  };

  const act = async (id: string, verb: string, body: unknown = {}) => {
    setBusy(true);
    try {
      await postJson(`/api/t/${tenantId}/campaigns/${id}/${verb}`, body);
      toast(`Campaign ${verb} succeeded`, "success");
      await fetchAll();
      const row = campaigns.find((c) => c.id === id);
      if (row) await open(row);
    } catch (err) {
      toast(err instanceof Error ? err.message : `${verb} failed`, "error");
    } finally {
      setBusy(false);
    }
  };

  const columns: Column<Campaign>[] = [
    {
      key: "name",
      header: "Campaign",
      render: (r) => (
        <span style={{ fontSize: "0.8125rem", fontWeight: 600 }}>
          {r.name}
          <div style={{ fontSize: "0.72rem", color: "var(--text-muted)", fontWeight: 400 }}>
            {r.action_type.replace(/_/g, " ")} · v{r.version}
          </div>
        </span>
      ),
    },
    {
      key: "status",
      header: "Status",
      render: (r) => (
        <StatusBadge
          status={r.status.replace(/_/g, " ")}
          variant={STATUS_VARIANT[r.status] ?? "neutral"}
          size="sm"
        />
      ),
    },
    {
      key: "sites",
      header: "Sites",
      render: (r) => (
        <span style={{ fontSize: "0.8125rem" }}>
          {r.progress.sites_completed}/{r.progress.sites_total}
          {r.progress.sites_halted > 0 && (
            <div style={{ fontSize: "0.72rem", color: "var(--status-critical)" }}>
              {r.progress.sites_halted} halted
            </div>
          )}
        </span>
      ),
    },
    {
      key: "targets",
      header: "Targets",
      render: (r) => {
        const a = r.progress.targets_by_applicability ?? {};
        return (
          <span style={{ fontSize: "0.8125rem" }}>
            {a.eligible ?? 0} eligible
            {(a.warn_not_permitted ?? 0) > 0 && (
              <div style={{ fontSize: "0.72rem", color: "var(--status-warning)" }}>
                {a.warn_not_permitted} need acknowledgement
              </div>
            )}
            {(a.excluded_unimplemented ?? 0) > 0 && (
              <div style={{ fontSize: "0.72rem", color: "var(--text-muted)" }}>
                {a.excluded_unimplemented} excluded
              </div>
            )}
          </span>
        );
      },
    },
    {
      key: "outcome",
      header: "Outcome",
      render: (r) =>
        r.progress.partial_success ? (
          <StatusBadge status="partial success" variant="warning" size="sm" />
        ) : r.halt_reason ? (
          <span style={{ fontSize: "0.78rem", color: "var(--status-critical)" }}>
            {r.halt_reason}
          </span>
        ) : (
          <span style={{ fontSize: "0.78rem", color: "var(--text-muted)" }}>—</span>
        ),
    },
  ];

  const warned = selected
    ? (selected.progress.targets_by_applicability?.warn_not_permitted ?? 0) +
      (selected.progress.targets_by_applicability?.unknown ?? 0)
    : 0;

  return (
    <div>
      <Toast toasts={toasts} onDismiss={dismiss} />
      <PageHeader
        title="Campaigns"
        breadcrumbs={[
          { label: "HarkenIQ" },
          { label: "Operations" },
          { label: "Campaigns" },
        ]}
      />

      <p style={helpText}>
        One governed action, run across a scoped estate. Every device is checked against
        the Capability Registry before anything is approved, each site's waves are planned
        by that site from its own fault domains, and every wave is approved individually.
        Approval authorizes a plan — it is not a guarantee: capability and policy are
        re-checked immediately before each wave runs, and may only ever narrow it.
      </p>

      <div style={metricsRow}>
        <MetricCard title="Campaigns" value={campaigns.length} />
        <MetricCard
          title="Running"
          value={campaigns.filter((c) => c.status === "running").length}
        />
        <MetricCard
          title="Awaiting approval"
          value={campaigns.filter((c) => c.status === "awaiting_approval").length}
        />
        <MetricCard
          title="Halted"
          value={campaigns.filter((c) => c.status === "halted").length}
        />
      </div>

      {campaigns.length === 0 && !loading ? (
        <EmptyState
          title="No campaigns"
          description="A campaign runs one governed action across an org unit, site or device class."
          icon="&#x25A6;"
        />
      ) : (
        <DataTable<Campaign>
          columns={columns}
          data={campaigns}
          loading={loading}
          emptyMessage="No campaigns"
          onRowClick={(r) => void open(r)}
          striped
        />
      )}

      <DetailPanel
        open={selected !== null}
        onClose={() => setSelected(null)}
        title={selected?.name ?? ""}
        subtitle={selected ? `${selected.action_type.replace(/_/g, " ")} · v${selected.version}` : ""}
      >
        {selected && (
          <div>
            {warned > 0 && !selected.acknowledgement_valid && (
              <div
                style={{
                  ...bannerBase,
                  background: "var(--status-warning-bg)",
                  color: "var(--status-warning)",
                  border: "1px solid var(--status-warning)",
                }}
              >
                <strong>{warned} target(s) need a decision.</strong>
                <span>
                  Their executors implement this action but the node does not currently
                  permit it, or they have not declared. Exclude them or acknowledge that
                  they may be refused at execution time.
                </span>
              </div>
            )}

            {selected.progress.partial_success && (
              <div
                style={{
                  ...bannerBase,
                  background: "var(--status-warning-bg)",
                  color: "var(--status-warning)",
                  border: "1px solid var(--status-warning)",
                }}
              >
                <strong>Partial success.</strong>
                <span>
                  {selected.progress.sites_completed} site(s) completed and{" "}
                  {selected.progress.sites_halted} halted. A halted site does not halt the
                  campaign.
                </span>
              </div>
            )}

            <div style={sectionTitle}>Campaign</div>
            <div style={detailRow}>
              <span style={detailLabel}>Attribution</span>
              <span style={{ ...detailValue, ...mono }}>{selected.actor}</span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Created by</span>
              <span style={detailValue}>{selected.created_by}</span>
            </div>
            <div style={detailRow}>
              <span style={detailLabel}>Acknowledged by</span>
              <span style={detailValue}>
                {selected.acknowledged_by ?? "—"}
                {selected.acknowledged_by && !selected.acknowledgement_valid && " (stale)"}
              </span>
            </div>

            <div style={sectionTitle}>Sites</div>
            {selected.sites.map((s) => (
              <div key={s.site_id} style={detailRow}>
                <span style={detailLabel}>{s.site_name || s.site_id}</span>
                <span style={detailValue}>
                  <StatusBadge
                    status={s.status}
                    variant={STATUS_VARIANT[s.status] ?? "neutral"}
                    size="sm"
                  />
                  {s.wave_count > 0 && ` wave ${s.current_wave + 1}/${s.wave_count}`}
                  {s.halt_reason && (
                    <div style={{ fontSize: "0.72rem", color: "var(--status-critical)" }}>
                      {s.halt_reason}
                    </div>
                  )}
                </span>
              </div>
            ))}

            <div style={sectionTitle}>Site-waves — what each approval authorizes</div>
            <p style={{ fontSize: "0.78rem", color: "var(--text-secondary)", lineHeight: 1.55 }}>
              Approval is per site-wave and binds to this exact device set and plan.
              Decisions are made on the Approvals page.
            </p>
            {waves.length === 0 && (
              <p style={{ fontSize: "0.8125rem", color: "var(--text-muted)" }}>
                No waves planned yet — preflight this campaign.
              </p>
            )}
            {waves.map((w) => (
              <div key={`${w.site_id}-${w.wave_index}-${w.plan_hash}`} style={detailRow}>
                <span style={detailLabel}>
                  wave {w.wave_index}
                  <div style={{ ...mono, color: "var(--text-muted)" }}>
                    {w.device_agent_ids.join(", ")}
                  </div>
                  {w.void_reason && (
                    <div style={{ fontSize: "0.72rem", color: "var(--status-critical)" }}>
                      {w.void_reason}
                    </div>
                  )}
                </span>
                <span style={detailValue}>
                  <StatusBadge
                    status={w.status.replace(/_/g, " ")}
                    variant={WAVE_VARIANT[w.status] ?? "neutral"}
                    size="sm"
                  />
                  <div style={{ ...mono, color: "var(--text-muted)" }}>
                    {w.domain_span} domain(s) · plan {w.plan_hash.slice(0, 8)}
                  </div>
                </span>
              </div>
            ))}

            <div style={sectionTitle}>Targets</div>
            {selected.targets.map((t) => (
              <div key={t.device_agent_id} style={detailRow}>
                <span style={detailLabel}>
                  {t.device_name || t.device_agent_id}
                  {t.reason && (
                    <div style={{ fontSize: "0.72rem", color: "var(--text-muted)" }}>
                      {t.reason}
                    </div>
                  )}
                  {t.revalidation_reason && (
                    <div style={{ fontSize: "0.72rem", color: "var(--status-warning)" }}>
                      {t.revalidation_reason}
                    </div>
                  )}
                </span>
                <span style={detailValue}>
                  <StatusBadge
                    status={APPLICABILITY_LABEL[t.applicability] ?? t.applicability}
                    variant={APPLICABILITY_VARIANT[t.applicability] ?? "neutral"}
                    size="sm"
                  />
                </span>
              </div>
            ))}

            <div style={sectionTitle}>Actions</div>
            <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
              <button
                disabled={busy}
                onClick={() => void act(selected.id, "preflight")}
                style={{
                  padding: "0.45rem 0.95rem",
                  borderRadius: "var(--radius-md)",
                  border: "1px solid var(--border-color)",
                  background: "var(--bg-card)",
                  cursor: "pointer",
                  font: "inherit",
                  fontSize: "0.8125rem",
                  fontWeight: 600,
                }}
              >
                Preflight
              </button>
              {warned > 0 && (
                <button
                  disabled={busy}
                  onClick={() => void act(selected.id, "acknowledge", { confirm: true })}
                  style={{
                    padding: "0.45rem 0.95rem",
                    borderRadius: "var(--radius-md)",
                    border: "1px solid var(--status-warning)",
                    background: "var(--bg-card)",
                    cursor: "pointer",
                    font: "inherit",
                    fontSize: "0.8125rem",
                    fontWeight: 600,
                  }}
                >
                  Acknowledge {warned} warned
                </button>
              )}
              <button
                disabled={busy}
                onClick={() => void act(selected.id, "submit")}
                style={{
                  padding: "0.45rem 0.95rem",
                  borderRadius: "var(--radius-md)",
                  border: "1px solid var(--border-color)",
                  background: "var(--bg-card)",
                  cursor: "pointer",
                  font: "inherit",
                  fontSize: "0.8125rem",
                  fontWeight: 600,
                }}
              >
                Submit for approval
              </button>
              <button
                disabled={busy}
                onClick={() => void act(selected.id, "advance")}
                style={{
                  padding: "0.45rem 0.95rem",
                  borderRadius: "var(--radius-md)",
                  border: "1px solid var(--border-color)",
                  background: "var(--bg-card)",
                  cursor: "pointer",
                  font: "inherit",
                  fontSize: "0.8125rem",
                  fontWeight: 600,
                }}
              >
                Advance
              </button>
            </div>
          </div>
        )}
      </DetailPanel>
    </div>
  );
}
