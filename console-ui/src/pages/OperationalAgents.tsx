import { useParams } from "react-router-dom";
import { type CSSProperties, useCallback, useEffect, useMemo, useState } from "react";
import PageHeader from "../components/PageHeader";
import DataTable, { type Column } from "../components/DataTable";
import DetailPanel from "../components/DetailPanel";
import MetricCard from "../components/MetricCard";
import StatusBadge from "../components/StatusBadge";
import EmptyState from "../components/EmptyState";
import Toast from "../components/Toast";
import { useToast } from "../components/useToast";
import { getJson, postJson } from "../api";
import { useAuth } from "../useAuth";
import type { AgentProposal } from "../types";

/* A0+A1 — Operational Agents: the product noun, under existing governance.
 *
 * An Operational Agent is a declarative bundle over capabilities that
 * already exist: a name, an explicit scope, bindings to governed
 * capabilities, and a policy that can only ever tighten what the tenant
 * itself permits. It is configuration, not a runtime, and it holds no
 * credential of its own.
 *
 * This page is a CONSUMER of /api/operational-agents. Every disposition,
 * every blocking condition and every piece of evidence is composed at
 * Central Command from the same S5 contract the Autonomy page reads;
 * nothing here decides anything in the browser.
 *
 * It exists to answer, for one named agent:
 *   What is it? What can it see? What can it do?
 *   What may it do without me, and why? What needs my approval?
 *   What did it do? What happened? What did it learn? */

/* ── Types ────────────────────────────────────────── */

interface ScopeRule {
  scope_type: string;
  scope_ref: string;
}

interface CapabilityBinding {
  kind: string;
  capability_ref: string;
}

interface AgentRow {
  id: string;
  name: string;
  description: string;
  status: string;
  version: number;
  actor: string;
  autonomy_ceiling: number;
  require_approval_always: boolean;
  max_proposals_per_day: number;
  created_by: string;
  activated_by: string;
  last_evaluated_at: string | null;
  scopes: ScopeRule[];
  capabilities: CapabilityBinding[];
  proposal_counts: Record<string, number>;
}

interface ActionClassView {
  action_type: string;
  known_to_executor: boolean;
  risk: string | null;
  granted_at_level: number | null;
  never_budget_grantable?: boolean;
  tenant_disposition?: string;
  disposition: string;
  disposition_reason: string;
  blocking_conditions: { code: string; detail: string; scope: string }[];
  requires_approval: boolean;
  evidence: {
    executions: number;
    success_rate: number | null;
    sufficient: boolean;
  } | null;
  learning: { statement: string; confidence: number | null }[];
  advancement: { statement: string } | null;
}

interface AgentView {
  agent: AgentRow & { evaluating: boolean; species: string; created_at: string | null };
  scope: {
    rules: ScopeRule[];
    device_count: number;
    devices: {
      agent_id: string;
      agent_name: string;
      site_id: string;
      device_class: string;
      health: string;
      observation: string;
    }[];
    reads: string[];
    statement: string;
  };
  capabilities: {
    action_classes: ActionClassView[];
    skills: string[];
    autonomous_now: string[];
    needs_approval: string[];
    denied: string[];
  };
  activity: {
    by_status: Record<string, number>;
    awaiting_approval: number;
    blocked: number;
    executed: number;
    succeeded: number;
    success_rate: number | null;
  };
  posture: {
    tenant_level: number;
    stop_switch: { active: boolean };
    safety_reported: boolean;
    sites_not_reporting: string[];
  };
  proposals: AgentProposal[];
}

interface Catalogue {
  action_classes: {
    action_type: string;
    risk: string;
    proposable: boolean;
    observed_conditions: string[];
    note: string;
  }[];
  read_capabilities: { ref: string; description: string; required: boolean }[];
  scope_options: {
    sites: { id: string; name: string }[];
    device_classes: string[];
    device_count: number;
  };
}

/* ── Presentation maps ────────────────────────────── */

const STATUS_VARIANT: Record<string, "success" | "warning" | "critical" | "info" | "neutral"> = {
  active: "success",
  draft: "neutral",
  paused: "warning",
  retired: "neutral",
};

const DISPOSITION_LABEL: Record<string, string> = {
  autonomous: "runs unattended",
  requires_approval: "needs a human",
  denied: "denied",
  not_budget_mapped: "not mapped",
};

const DISPOSITION_VARIANT: Record<string, "success" | "warning" | "critical" | "info" | "neutral"> = {
  autonomous: "success",
  requires_approval: "warning",
  denied: "critical",
  not_budget_mapped: "neutral",
};

const PROPOSAL_VARIANT: Record<string, "success" | "warning" | "critical" | "info" | "neutral"> = {
  completed: "success",
  dispatched: "info",
  approved: "info",
  awaiting_approval: "warning",
  blocked: "neutral",
  denied: "critical",
  failed: "critical",
};

/* ── Styles ───────────────────────────────────────── */

const metricsGrid: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
  gap: "0.75rem",
  marginBottom: "1.25rem",
};

const sectionStyle: CSSProperties = {
  marginBottom: "1.5rem",
};

const sectionTitle: CSSProperties = {
  fontSize: "0.75rem",
  textTransform: "uppercase",
  letterSpacing: "0.05em",
  color: "var(--text-muted)",
  marginBottom: "0.5rem",
  fontWeight: 600,
};

const noteStyle: CSSProperties = {
  fontSize: "0.8125rem",
  color: "var(--text-secondary)",
  lineHeight: 1.5,
  marginBottom: "1rem",
};

const rowStyle: CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  gap: "1rem",
  padding: "0.5rem 0",
  borderBottom: "1px solid var(--border-color)",
  fontSize: "0.8125rem",
};

const blockStyle: CSSProperties = {
  padding: "0.625rem 0.75rem",
  borderRadius: "var(--radius-md, 6px)",
  background: "var(--bg-subtle, rgba(127,127,127,0.08))",
  borderLeft: "3px solid var(--accent)",
  fontSize: "0.8125rem",
  lineHeight: 1.5,
  marginBottom: "0.5rem",
};

const formStyle: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: "0.75rem",
  padding: "1rem 1.25rem",
  background: "var(--bg-card)",
  border: "1px solid var(--border-color)",
  borderRadius: "var(--radius-lg)",
  marginBottom: "1.25rem",
};

const labelStyle: CSSProperties = {
  fontSize: "0.75rem",
  fontWeight: 600,
  color: "var(--text-secondary)",
  display: "block",
  marginBottom: "0.25rem",
};

const checkRow: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: "0.375rem",
  fontSize: "0.8125rem",
};

/* ── Page ─────────────────────────────────────────── */

export default function OperationalAgents() {
  const { tenantId = "" } = useParams<{ tenantId: string }>();
  const { toasts, toast, dismiss } = useToast();
  const { user } = useAuth();

  const [agents, setAgents] = useState<AgentRow[]>([]);
  const [catalogue, setCatalogue] = useState<Catalogue | null>(null);
  const [loading, setLoading] = useState(true);
  const [view, setView] = useState<AgentView | null>(null);
  const [busy, setBusy] = useState(false);
  const [creating, setCreating] = useState(false);

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [siteRefs, setSiteRefs] = useState<string[]>([]);
  const [classRefs, setClassRefs] = useState<string[]>([]);
  const [alwaysApprove, setAlwaysApprove] = useState(true);
  const [ceiling, setCeiling] = useState(0);

  const canManage =
    user?.permissions?.includes("site.manage") || user?.permissions?.includes("*") || false;

  const fetchAll = useCallback(async () => {
    try {
      const [list, cat] = await Promise.all([
        getJson<{ agents: AgentRow[] }>(`/api/t/${tenantId}/operational-agents/`),
        getJson<Catalogue>(`/api/t/${tenantId}/operational-agents/catalogue`),
      ]);
      setAgents(list.agents ?? []);
      setCatalogue(cat);
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to load agents", "error");
    } finally {
      setLoading(false);
    }
  }, [tenantId, toast]);

  useEffect(() => {
    setLoading(true);
    void fetchAll();
  }, [fetchAll]);

  const openAgent = useCallback(
    async (row: AgentRow) => {
      try {
        setView(
          await getJson<AgentView>(`/api/t/${tenantId}/operational-agents/${row.id}`),
        );
      } catch (err) {
        toast(err instanceof Error ? err.message : "Failed to load agent", "error");
      }
    },
    [tenantId, toast],
  );

  const transition = async (agentId: string, action: string) => {
    setBusy(true);
    try {
      await postJson(`/api/t/${tenantId}/operational-agents/${agentId}/${action}`, {});
      toast(
        action === "activate"
          ? "Agent active. It evaluates its scope on the next pass and proposes; nothing runs without the governance it already faces."
          : `Agent ${action}d`,
        action === "activate" ? "success" : "info",
      );
      await fetchAll();
      if (view?.agent.id === agentId) {
        setView(
          await getJson<AgentView>(`/api/t/${tenantId}/operational-agents/${agentId}`),
        );
      }
    } catch (err) {
      toast(err instanceof Error ? err.message : `Could not ${action} the agent`, "error");
    } finally {
      setBusy(false);
    }
  };

  const create = async () => {
    setBusy(true);
    try {
      await postJson(`/api/t/${tenantId}/operational-agents/`, {
        name,
        description,
        autonomy_ceiling: ceiling,
        require_approval_always: alwaysApprove,
        scopes: siteRefs.map((ref) => ({ scope_type: "site", scope_ref: ref })),
        capabilities: classRefs.map((ref) => ({
          kind: "action_class",
          capability_ref: ref,
        })),
      });
      toast(`${name} created as a draft. Review it, then activate.`, "success");
      setCreating(false);
      setName("");
      setDescription("");
      setSiteRefs([]);
      setClassRefs([]);
      await fetchAll();
    } catch (err) {
      toast(err instanceof Error ? err.message : "Could not create the agent", "error");
    } finally {
      setBusy(false);
    }
  };

  const toggle = (list: string[], value: string, set: (v: string[]) => void) =>
    set(list.includes(value) ? list.filter((v) => v !== value) : [...list, value]);

  const columns: Column<AgentRow>[] = useMemo(
    () => [
      {
        key: "name",
        header: "Agent",
        render: (r) => (
          <span style={{ fontWeight: 600, fontSize: "0.8125rem" }}>
            {r.name}
            <div
              style={{
                fontSize: "0.72rem",
                color: "var(--text-muted)",
                fontWeight: 400,
                fontFamily: "var(--font-mono, monospace)",
              }}
            >
              {r.actor}
            </div>
          </span>
        ),
      },
      {
        key: "status",
        header: "Status",
        render: (r) => (
          <StatusBadge
            status={r.status}
            variant={STATUS_VARIANT[r.status] ?? "neutral"}
            size="sm"
          />
        ),
      },
      {
        key: "scopes",
        header: "Scope",
        render: (r) =>
          r.scopes.length === 0 ? (
            <span style={{ color: "var(--text-muted)" }}>nothing bound</span>
          ) : (
            <span>{r.scopes.map((s) => `${s.scope_type}:${s.scope_ref}`).join(", ")}</span>
          ),
      },
      {
        key: "capabilities",
        header: "Can propose",
        render: (r) => {
          const classes = r.capabilities.filter((c) => c.kind === "action_class");
          return classes.length === 0 ? (
            <span style={{ color: "var(--text-muted)" }}>nothing</span>
          ) : (
            <span>{classes.map((c) => c.capability_ref.replace(/_/g, " ")).join(", ")}</span>
          );
        },
      },
      {
        key: "autonomy",
        header: "Without a human",
        render: (r) =>
          r.require_approval_always ? (
            <span style={{ color: "var(--text-muted)" }}>never, by configuration</span>
          ) : (
            <span>up to level {r.autonomy_ceiling}</span>
          ),
      },
      {
        key: "proposal_counts",
        header: "Proposals",
        render: (r) => {
          const total = Object.values(r.proposal_counts ?? {}).reduce((a, b) => a + b, 0);
          const waiting = r.proposal_counts?.awaiting_approval ?? 0;
          return total === 0 ? (
            <span style={{ color: "var(--text-muted)" }}>none yet</span>
          ) : (
            <span>
              {total}
              {waiting > 0 ? (
                <span style={{ color: "var(--warning, #b45309)" }}> · {waiting} waiting</span>
              ) : null}
            </span>
          );
        },
      },
    ],
    [],
  );

  const totals = useMemo(() => {
    const active = agents.filter((a) => a.status === "active").length;
    const waiting = agents.reduce(
      (n, a) => n + (a.proposal_counts?.awaiting_approval ?? 0),
      0,
    );
    const autonomous = agents.filter(
      (a) => a.status === "active" && !a.require_approval_always && a.autonomy_ceiling > 0,
    ).length;
    return { active, waiting, autonomous };
  }, [agents]);

  return (
    <div>
      <Toast toasts={toasts} onDismiss={dismiss} />

      <PageHeader
        title="Operational Agents"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Governance" }, { label: "Agents" }]}
        actions={
          canManage
            ? [
                {
                  label: creating ? "Cancel" : "New agent",
                  onClick: () => setCreating((c) => !c),
                  variant: "primary" as const,
                },
              ]
            : undefined
        }
      />

      <p style={noteStyle}>
        An Operational Agent is a named bundle: a scope it may observe, capabilities it may
        propose, and a policy that can only ever be narrower than this tenant's own. It is
        another requester against the same governed system you use. Its proposals land in
        the same approval queue, under the same permission, and execute through the same
        gates on the device. It holds no credentials and reaches nothing its scope does not
        name.
      </p>

      <div style={metricsGrid}>
        <MetricCard title="Agents" value={agents.length} />
        <MetricCard title="Active" value={totals.active} />
        <MetricCard title="Waiting on a human" value={totals.waiting} />
        <MetricCard title="May act unattended" value={totals.autonomous} />
      </div>

      {creating && catalogue ? (
        <div style={formStyle}>
          <div>
            <label style={labelStyle} htmlFor="agent-name">
              Name
            </label>
            <input
              id="agent-name"
              className="input"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Night Shift"
              style={{ width: "100%" }}
            />
          </div>
          <div>
            <label style={labelStyle} htmlFor="agent-desc">
              What is it for
            </label>
            <input
              id="agent-desc"
              className="input"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="watches DC-1 overnight"
              style={{ width: "100%" }}
            />
          </div>
          <div>
            <span style={labelStyle}>What it may see (no site means it sees nothing)</span>
            {catalogue.scope_options.sites.map((s) => (
              <label key={s.id} style={checkRow}>
                <input
                  type="checkbox"
                  checked={siteRefs.includes(s.id)}
                  onChange={() => toggle(siteRefs, s.id, setSiteRefs)}
                />
                {s.name}
              </label>
            ))}
          </div>
          <div>
            <span style={labelStyle}>What it may propose</span>
            {catalogue.action_classes
              .filter((c) => c.proposable)
              .map((c) => (
                <label key={c.action_type} style={checkRow}>
                  <input
                    type="checkbox"
                    checked={classRefs.includes(c.action_type)}
                    onChange={() => toggle(classRefs, c.action_type, setClassRefs)}
                  />
                  {c.action_type.replace(/_/g, " ")}
                  <span style={{ color: "var(--text-muted)", fontSize: "0.72rem" }}>
                    risk {c.risk} · fires on {c.observed_conditions.join(", ")}
                  </span>
                </label>
              ))}
          </div>
          <label style={checkRow}>
            <input
              type="checkbox"
              checked={alwaysApprove}
              onChange={(e) => setAlwaysApprove(e.target.checked)}
            />
            Ask me before anything runs, even where this tenant grants autonomy
          </label>
          {!alwaysApprove ? (
            <div>
              <label style={labelStyle} htmlFor="agent-ceiling">
                Autonomy ceiling for this agent (never above the tenant's own level)
              </label>
              <select
                id="agent-ceiling"
                className="input"
                value={ceiling}
                onChange={(e) => setCeiling(Number(e.target.value))}
              >
                <option value={0}>0 — observe</option>
                <option value={1}>1 — suggest</option>
                <option value={2}>2 — batch (low-risk recovery)</option>
                <option value={3}>3 — autonomous (adds medium risk)</option>
              </select>
            </div>
          ) : null}
          <div>
            <button
              className="btn btn-primary"
              onClick={() => void create()}
              disabled={busy || !name || siteRefs.length === 0 || classRefs.length === 0}
            >
              Create as draft
            </button>
            <span style={{ marginLeft: "0.75rem", fontSize: "0.75rem", color: "var(--text-muted)" }}>
              Creating never activates. Review the bundle, then activate it deliberately.
            </span>
          </div>
        </div>
      ) : null}

      {!loading && agents.length === 0 ? (
        <EmptyState
          icon="&#9673;"
          title="No Operational Agents yet"
          description={
            canManage
              ? "Create one, give it a site and an action class, and it will start proposing work with its evidence attached."
              : "Nobody has created an Operational Agent for this tenant yet."
          }
        />
      ) : (
        <DataTable
          columns={columns}
          data={agents}
          loading={loading}
          onRowClick={(r) => void openAgent(r)}
          emptyMessage="No agents"
        />
      )}

      <DetailPanel
        open={view !== null}
        onClose={() => setView(null)}
        title={view?.agent.name ?? ""}
        subtitle={view?.agent.actor}
        width={720}
      >
        {view ? (
          <div style={{ padding: "1.25rem 1.5rem" }}>
            <div style={{ display: "flex", gap: "0.5rem", marginBottom: "1rem" }}>
              <StatusBadge
                status={view.agent.status}
                variant={STATUS_VARIANT[view.agent.status] ?? "neutral"}
                size="sm"
              />
              <StatusBadge status={`v${view.agent.version}`} variant="neutral" size="sm" />
              {view.posture.stop_switch.active ? (
                <StatusBadge status="stop switch active" variant="critical" size="sm" />
              ) : null}
            </div>

            {canManage ? (
              <div style={{ display: "flex", gap: "0.5rem", marginBottom: "1.25rem" }}>
                {view.agent.status !== "active" && view.agent.status !== "retired" ? (
                  <button
                    className="btn btn-primary"
                    disabled={busy}
                    onClick={() => void transition(view.agent.id, "activate")}
                  >
                    Activate
                  </button>
                ) : null}
                {view.agent.status === "active" ? (
                  <button
                    className="btn"
                    disabled={busy}
                    onClick={() => void transition(view.agent.id, "pause")}
                  >
                    Pause
                  </button>
                ) : null}
                {view.agent.status !== "retired" ? (
                  <button
                    className="btn btn-danger"
                    disabled={busy}
                    onClick={() => void transition(view.agent.id, "retire")}
                  >
                    Retire
                  </button>
                ) : null}
              </div>
            ) : null}

            {/* What can it see */}
            <div style={sectionStyle}>
              <div style={sectionTitle}>What it can see</div>
              <div style={blockStyle}>{view.scope.statement}</div>
              {view.scope.devices.slice(0, 12).map((d) => (
                <div key={d.agent_id} style={rowStyle}>
                  <span style={{ fontFamily: "var(--font-mono, monospace)" }}>
                    {d.agent_name || d.agent_id}
                  </span>
                  <span style={{ color: "var(--text-muted)" }}>
                    {d.device_class} · {d.health || "unknown"} · {d.observation || "unobserved"}
                  </span>
                </div>
              ))}
              <div style={{ ...rowStyle, borderBottom: "none" }}>
                <span>Reads</span>
                <span style={{ color: "var(--text-muted)" }}>
                  {view.scope.reads.join(", ") || "none"}
                </span>
              </div>
            </div>

            {/* What can it do, and what may it do without me */}
            <div style={sectionStyle}>
              <div style={sectionTitle}>What it can do, and what needs you</div>
              {view.capabilities.action_classes.map((c) => (
                <div key={c.action_type} style={{ marginBottom: "0.75rem" }}>
                  <div style={{ display: "flex", justifyContent: "space-between", gap: "0.5rem" }}>
                    <span style={{ fontWeight: 600, fontSize: "0.8125rem" }}>
                      {c.action_type.replace(/_/g, " ")}
                    </span>
                    <StatusBadge
                      status={DISPOSITION_LABEL[c.disposition] ?? c.disposition}
                      variant={DISPOSITION_VARIANT[c.disposition] ?? "neutral"}
                      size="sm"
                    />
                  </div>
                  <div style={{ fontSize: "0.78rem", color: "var(--text-secondary)" }}>
                    {c.disposition_reason || "no constraint recorded"}
                  </div>
                  {c.evidence ? (
                    <div style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>
                      {c.evidence.sufficient && c.evidence.success_rate !== null
                        ? `${Math.round(c.evidence.success_rate * 100)}% success over ${c.evidence.executions} executions in this tenant`
                        : `${c.evidence.executions} recorded execution(s): too few to judge`}
                    </div>
                  ) : null}
                  {c.advancement?.statement ? (
                    <div style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>
                      {c.advancement.statement}
                    </div>
                  ) : null}
                  {c.learning.map((l, i) => (
                    <div key={i} style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>
                      Learned: {l.statement}
                    </div>
                  ))}
                </div>
              ))}
            </div>

            {/* What did it do, what happened */}
            <div style={sectionStyle}>
              <div style={sectionTitle}>What it did, and what happened</div>
              <div style={rowStyle}>
                <span>Waiting on a human</span>
                <span>{view.activity.awaiting_approval}</span>
              </div>
              <div style={rowStyle}>
                <span>Blocked by governance</span>
                <span>{view.activity.blocked}</span>
              </div>
              <div style={rowStyle}>
                <span>Executed</span>
                <span>
                  {view.activity.executed}
                  {view.activity.success_rate !== null
                    ? ` · ${Math.round(view.activity.success_rate * 100)}% succeeded`
                    : ""}
                </span>
              </div>
            </div>

            <div style={sectionStyle}>
              <div style={sectionTitle}>Proposals</div>
              {view.proposals.length === 0 ? (
                <div style={{ fontSize: "0.8125rem", color: "var(--text-muted)" }}>
                  Nothing proposed yet.
                </div>
              ) : (
                view.proposals.slice(0, 20).map((p) => (
                  <div key={p.proposal_id} style={{ marginBottom: "0.75rem" }}>
                    <div style={{ display: "flex", justifyContent: "space-between", gap: "0.5rem" }}>
                      <span style={{ fontWeight: 600, fontSize: "0.8125rem" }}>
                        {p.action_type.replace(/_/g, " ")} on {p.device_agent_id}
                      </span>
                      <StatusBadge
                        status={p.status.replace(/_/g, " ")}
                        variant={PROPOSAL_VARIANT[p.status] ?? "neutral"}
                        size="sm"
                      />
                    </div>
                    <div style={{ fontSize: "0.78rem", color: "var(--text-secondary)" }}>
                      {p.rationale}
                    </div>
                    {p.decided_by ? (
                      <div style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>
                        Decided by {p.decided_by}
                        {p.outcome ? ` · outcome ${p.outcome}` : ""}
                      </div>
                    ) : null}
                    {p.status === "blocked" || p.status === "failed" ? (
                      <div style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>
                        {p.dispatch_reason || p.disposition_reason}
                      </div>
                    ) : null}
                  </div>
                ))
              )}
            </div>
          </div>
        ) : null}
      </DetailPanel>
    </div>
  );
}
