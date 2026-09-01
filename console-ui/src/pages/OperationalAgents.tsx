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
import { getJson, patchJson, postJson } from "../api";
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
  /** A19.9: the configuration actually switched on. 0 with an active
   *  agent means it was activated before the platform recorded this,
   *  which is UNKNOWN — never read as drift. */
  activated_version: number;
  configuration_drifted: boolean;
  execution_budget: number;
  budget_period: string;
  paused_reason: string | null;
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

/* ── A2: readiness, activation, runtime ───────────── */

/** One dimension of the activation readiness contract.
 *
 * Composed at Central Command and stored. This page RENDERS it and
 * never recomputes it: if the browser could reach its own verdict, an
 * operator could approve something different from what the activation
 * gate enforces, and the divergence would be invisible until it
 * mattered. */
interface PreflightDimension {
  dimension: string;
  verdict: "ready" | "blocked" | "warn" | "unknown";
  detail: string;
  [extra: string]: unknown;
}

interface ActivationApproval {
  subject_ref: string;
  state: string;
  required: number;
  received: number;
  remaining?: number;
  approvers: { approver: string; decision: string }[];
  policy_name?: string;
  group_name?: string;
  note: string;
}

interface Preflight {
  agent_id: string;
  exists: boolean;
  current?: boolean;
  produced_by?: string;
  produced_at?: string | null;
  detail?: string;
  configuration_version: number;
  overall?: "ready" | "blocked" | "warn" | "unknown";
  can_activate?: boolean;
  requires_acknowledgement?: boolean;
  requires_activation_approval?: boolean;
  unattended_classes?: string[];
  blocked_dimensions?: string[];
  warn_dimensions?: string[];
  unknown_dimensions?: string[];
  dimensions?: PreflightDimension[];
  skills?: {
    skill_id: string;
    usable: boolean | null;
    recommended: string[];
    unsupported: string[];
    reason: string;
    name?: string;
    version?: string;
  }[];
  acknowledged_by?: string | null;
  acknowledgement_current?: boolean;
  activation_approval?: ActivationApproval | null;
  contract?: { authority: string; unknown: string; versioning: string };
}

/** What the runtime can HONESTLY say. Only signals the platform
 *  actually produces; a dimension it cannot observe reads unknown
 *  rather than being filled with a plausible value. */
interface RuntimeState {
  agent_id: string;
  actor: string;
  activation_state: string;
  configuration_version: number;
  activated_version: number;
  activation_provenance: "recorded" | "unknown" | "inactive";
  configuration_drifted: boolean;
  last_evaluated_at: string | null;
  evaluation: string;
  devices: {
    in_scope: number;
    seen_recently: number;
    stale: number;
    /** Never counted as healthy OR unhealthy. */
    never_reported: number;
  };
  budget: {
    period: string;
    limit: number;
    executions_used: number;
    remaining: number | null;
    exhausted: boolean;
  };
  proposals_in_window: number;
  skills: Record<string, number>;
  skills_by_id: {
    skill_id: string;
    skill_version: string;
    counts: Record<string, number>;
    devices: {
      device_agent_id: string;
      site_id: string;
      status: string;
      detail: string;
      installed_at: string | null;
    }[];
  }[];
  paused_reason: string | null;
  preflight: {
    exists: boolean;
    configuration_version: number | null;
    overall: string;
    current: boolean;
  };
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

/* Four verdicts, and UNKNOWN is one of them. A fleet mid-upgrade is
 * unknown, not incapable, and colouring the two alike would make an
 * agent look broken for the duration of an upgrade. */
const VERDICT_VARIANT: Record<string, "success" | "warning" | "critical" | "info" | "neutral"> = {
  ready: "success",
  warn: "warning",
  blocked: "critical",
  unknown: "neutral",
};

const VERDICT_LABEL: Record<string, string> = {
  ready: "ready",
  warn: "needs a decision",
  blocked: "blocked",
  unknown: "unknown",
};

/** The twelve dimensions, in the order an operator reads them. Labels
 *  are the question each one answers, not the field name. */
const DIMENSION_LABEL: Record<string, string> = {
  identity: "Who is it",
  tenant: "Whose is it",
  scope: "Where can it operate",
  capabilities: "What can it do",
  skills: "What skills are bound",
  autonomy_ceiling: "How autonomous is it",
  approval_policy: "What approval is required",
  budget: "What budget applies",
  safety: "What safety constraints apply",
  executor_reach: "Can the devices actually run it",
  configuration_version: "Which configuration is this",
  activation_state: "Where is it now",
};

const PROVENANCE_NOTE: Record<string, string> = {
  recorded: "the configuration that is running is recorded",
  unknown:
    "this agent was activated before the platform recorded activation versions, so what is running cannot be named — re-run preflight and activate to record it",
  inactive: "not running, so there is nothing to drift",
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

/** A5 (A22.7): what this agent WOULD propose against current state.
 *  Composed entirely by Central Command through the same
 *  `govern_proposal` the runtime uses. The page renders it and derives
 *  nothing — a preview the browser reasoned about could show an operator
 *  something the runtime would never do. */
interface DryRun {
  agent_id: string;
  agent_version: number;
  status: string;
  evaluated_at: string;
  devices_in_scope: number;
  would_propose: Array<{
    device_agent_id: string;
    site_id: string;
    action_type: string;
    params: Record<string, string>;
    disposition: string;
    disposition_reason: string;
    authorization_basis: string;
    requires_human: boolean;
    rationale: string;
  }>;
  withheld: Array<{
    device_agent_id: string;
    action_type: string;
    subsystem: string;
    condition: string;
    code: string;
    reason: string;
  }>;
  contract: { governs: string; wrote_nothing: string; same_reasoning: string };
}

/* ── Page ─────────────────────────────────────────── */

export default function OperationalAgents() {
  const { tenantId = "" } = useParams<{ tenantId: string }>();
  const { toasts, toast, dismiss } = useToast();
  const { user } = useAuth();

  const [agents, setAgents] = useState<AgentRow[]>([]);
  const [catalogue, setCatalogue] = useState<Catalogue | null>(null);
  const [loading, setLoading] = useState(true);
  const [view, setView] = useState<AgentView | null>(null);
  const [preflight, setPreflight] = useState<Preflight | null>(null);
  const [runtime, setRuntime] = useState<RuntimeState | null>(null);
  const [busy, setBusy] = useState(false);
  const [creating, setCreating] = useState(false);
  const [budgetDraft, setBudgetDraft] = useState<{ limit: string; period: string }>({
    limit: "0",
    period: "daily",
  });
  const [pauseDraft, setPauseDraft] = useState("");
  const [dryRun, setDryRun] = useState<DryRun | null>(null);
  const [dryRunning, setDryRunning] = useState(false);

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

  /** Everything about one agent, from the contracts Central Command
   *  already composes. Three reads, no derivation in the browser. */
  const loadAgent = useCallback(
    async (agentId: string) => {
      const base = `/api/t/${tenantId}/operational-agents/${agentId}`;
      const [detail, pre, run] = await Promise.all([
        getJson<AgentView>(base),
        getJson<Preflight>(`${base}/preflight`),
        getJson<RuntimeState>(`${base}/runtime`),
      ]);
      setView(detail);
      setPreflight(pre);
      setRuntime(run);
      setBudgetDraft({
        limit: String(run.budget.limit ?? 0),
        period: run.budget.period || "daily",
      });
      setPauseDraft(run.paused_reason ?? "");
    },
    [tenantId],
  );

  const openAgent = useCallback(
    async (row: AgentRow) => {
      try {
        await loadAgent(row.id);
      } catch (err) {
        toast(err instanceof Error ? err.message : "Failed to load agent", "error");
      }
    },
    [loadAgent, toast],
  );

  const refreshOpen = useCallback(
    async (agentId: string) => {
      await fetchAll();
      if (view?.agent.id === agentId || preflight?.agent_id === agentId) {
        await loadAgent(agentId);
      }
    },
    [fetchAll, loadAgent, view, preflight],
  );

  /* A2: activation is a governed transition, not a status write. The
   * page walks the same sequence the server enforces — it never decides
   * anything, and a refusal is shown as the server's own reason. */

  const runPreflight = async (agentId: string) => {
    setBusy(true);
    try {
      const result = await postJson<Preflight>(
        `/api/t/${tenantId}/operational-agents/${agentId}/preflight`,
        {},
      );
      toast(
        result.overall === "blocked"
          ? `Preflight blocked: ${(result.blocked_dimensions ?? []).join(", ")}`
          : `Preflight ${result.overall} for version ${result.configuration_version}`,
        result.overall === "blocked" ? "error" : "success",
      );
      await refreshOpen(agentId);
    } catch (err) {
      toast(err instanceof Error ? err.message : "Preflight failed", "error");
    } finally {
      setBusy(false);
    }
  };

  const acknowledge = async (agentId: string) => {
    setBusy(true);
    try {
      await postJson(`/api/t/${tenantId}/operational-agents/${agentId}/acknowledge`, {});
      toast(
        "Warnings accepted against this configuration version. Editing the agent invalidates this.",
        "success",
      );
      await refreshOpen(agentId);
    } catch (err) {
      toast(err instanceof Error ? err.message : "Could not acknowledge", "error");
    } finally {
      setBusy(false);
    }
  };

  const patchAgent = async (agentId: string, body: Record<string, unknown>, note: string) => {
    setBusy(true);
    try {
      await patchJson(`/api/t/${tenantId}/operational-agents/${agentId}`, body);
      toast(note, "success");
      await refreshOpen(agentId);
    } catch (err) {
      toast(err instanceof Error ? err.message : "Could not save", "error");
    } finally {
      setBusy(false);
    }
  };

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
      await refreshOpen(agentId);
    } catch (err) {
      // The server is the authority on activation. When it refuses, its
      // reason is the message — this page does not invent one.
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
        key: "activated_version",
        header: "Running",
        render: (r) =>
          r.status !== "active" ? (
            <span style={{ color: "var(--text-muted)" }}>—</span>
          ) : r.configuration_drifted ? (
            <span style={{ color: "var(--status-warning, #b45309)" }}>
              v{r.activated_version} · edited to v{r.version}
            </span>
          ) : r.activated_version > 0 ? (
            <span>v{r.activated_version}</span>
          ) : (
            <span style={{ color: "var(--text-muted)" }}>unknown</span>
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

  /* What the page shows about activation comes from the STORED contract,
   * not from logic here. Central Command enforces all of this again on
   * the transition — this only spares an operator a refusal they can
   * already see, and quotes the server's own words for why. */
  /** Ask the agent what it would do. Writes nothing, decides nothing.
   *  Available whatever the agent's status — the point of a preview is
   *  to see the answer BEFORE switching anything on. */
  const runDryRun = useCallback(async () => {
    if (!view) return;
    setDryRunning(true);
    try {
      setDryRun(
        await getJson<DryRun>(
          `/api/t/${tenantId}/operational-agents/${view.agent.id}/dry-run`,
        ),
      );
    } catch (err) {
      toast(err instanceof Error ? err.message : "Dry run failed", "error");
    } finally {
      setDryRunning(false);
    }
  }, [tenantId, view, toast]);

  const preflightCurrent = Boolean(
    preflight?.exists && preflight.current && view &&
      preflight.configuration_version === view.agent.version,
  );

  const activationBlocker: string | null = useMemo(() => {
    if (!view || view.agent.status === "active") return null;
    if (!preflight?.exists) {
      return "Run preflight first — an agent cannot be activated without a reviewable readiness result.";
    }
    if (!preflightCurrent) {
      return `The stored preflight is for version ${preflight.configuration_version}; this agent is now v${view.agent.version}. Re-run it.`;
    }
    if (preflight.overall === "blocked") {
      return `Blocked by ${(preflight.blocked_dimensions ?? [])
        .map((d) => DIMENSION_LABEL[d] ?? d)
        .join(", ")}.`;
    }
    if (preflight.requires_acknowledgement && !preflight.acknowledgement_current) {
      return "Someone must accept the warnings and unknowns above first.";
    }
    if (
      preflight.requires_activation_approval &&
      preflight.activation_approval?.state !== "approved"
    ) {
      const a = preflight.activation_approval;
      return a
        ? `Waiting on the approvals queue — ${a.received} of ${a.required} recorded${
            a.state === "denied" ? ", and it was denied" : ""
          }.`
        : "Waiting on the approvals queue.";
    }
    return null;
  }, [view, preflight, preflightCurrent]);

  const activationReady = activationBlocker === null;

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
        onClose={() => {
          setView(null);
          setPreflight(null);
          setRuntime(null);
          setDryRun(null);
        }}
        title={view?.agent.name ?? ""}
        subtitle={view?.agent.actor}
        width={780}
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
              {/* A19.9: the version being EDITED and the version that is
                  RUNNING are the same number until somebody edits an
                  active agent — and the moment they differ is exactly
                  when an operator needs to know. */}
              {runtime?.configuration_drifted ? (
                <StatusBadge
                  status={`running v${runtime.activated_version}`}
                  variant="warning"
                  size="sm"
                />
              ) : null}
              {runtime?.activation_provenance === "unknown" ? (
                <StatusBadge status="running version unknown" variant="neutral" size="sm" />
              ) : null}
              {runtime?.paused_reason ? (
                <StatusBadge status="held" variant="warning" size="sm" />
              ) : null}
              {runtime?.budget.exhausted ? (
                <StatusBadge status="budget spent" variant="warning" size="sm" />
              ) : null}
              {view.posture.stop_switch.active ? (
                <StatusBadge status="stop switch active" variant="critical" size="sm" />
              ) : null}
            </div>

            {/* ── Activation: CREATE → CONFIGURE → PREFLIGHT →
                   ACKNOWLEDGE → APPROVAL (where required) → ACTIVATE.
                   Each step is enabled by the server's own contract; the
                   page reflects it and never decides it. ── */}
            {canManage && view.agent.status !== "retired" ? (
              <div style={{ ...formStyle, marginBottom: "1.25rem" }}>
                <div style={sectionTitle}>Activation</div>

                {/* 1. PREFLIGHT — mandatory, and bound to this version. */}
                <div style={{ display: "flex", gap: "0.5rem", alignItems: "center", flexWrap: "wrap" }}>
                  <button
                    className="btn"
                    disabled={busy}
                    onClick={() => void runPreflight(view.agent.id)}
                  >
                    {preflightCurrent ? "Re-run preflight" : "Run preflight"}
                  </button>
                  <span style={{ fontSize: "0.78rem", color: "var(--text-secondary)" }}>
                    {!preflight?.exists
                      ? "No readiness result yet. An agent cannot be activated without one."
                      : preflightCurrent
                        ? `Checked by ${preflight.produced_by} for version ${preflight.configuration_version}.`
                        : `The stored result is for version ${preflight.configuration_version}; this agent is now v${view.agent.version}. Re-run it.`}
                  </span>
                </div>

                {/* 2. ACKNOWLEDGE — a warning is not a veto, but a named
                       person must accept it, and an edit invalidates that. */}
                {preflightCurrent && preflight?.requires_acknowledgement ? (
                  <div
                    style={{
                      ...blockStyle,
                      borderLeftColor: "var(--status-warning, #b45309)",
                      marginBottom: 0,
                    }}
                  >
                    <strong>
                      {(preflight.warn_dimensions ?? []).length +
                        (preflight.unknown_dimensions ?? []).length}{" "}
                      dimension(s) need a decision.
                    </strong>{" "}
                    {[...(preflight.warn_dimensions ?? []), ...(preflight.unknown_dimensions ?? [])]
                      .map((d) => DIMENSION_LABEL[d] ?? d)
                      .join(", ")}
                    .
                    <div style={{ marginTop: "0.5rem" }}>
                      {preflight.acknowledgement_current ? (
                        <span style={{ fontSize: "0.78rem" }}>
                          Accepted by {preflight.acknowledged_by} for this version.
                        </span>
                      ) : (
                        <button
                          className="btn"
                          disabled={busy}
                          onClick={() => void acknowledge(view.agent.id)}
                        >
                          Accept these and continue
                        </button>
                      )}
                      {preflight.acknowledged_by && !preflight.acknowledgement_current ? (
                        <span
                          style={{ marginLeft: "0.5rem", fontSize: "0.75rem", color: "var(--text-muted)" }}
                        >
                          A previous acceptance by {preflight.acknowledged_by} no longer
                          applies — the configuration changed.
                        </span>
                      ) : null}
                    </div>
                  </div>
                ) : null}

                {/* 3. APPROVAL — raised ONLY where activation would confer
                       real unattended execution (D1). Decided on the one
                       approvals queue, never here. */}
                {preflightCurrent && preflight?.requires_activation_approval ? (
                  <div
                    style={{
                      ...blockStyle,
                      borderLeftColor: "var(--status-info, #2563eb)",
                      marginBottom: 0,
                    }}
                  >
                    <strong>Activating this would let it run without a human:</strong>{" "}
                    {(preflight.unattended_classes ?? [])
                      .map((c) => c.replace(/_/g, " "))
                      .join(", ")}
                    .
                    <div style={{ marginTop: "0.375rem", fontSize: "0.78rem" }}>
                      {preflight.activation_approval
                        ? `${preflight.activation_approval.received} of ${preflight.activation_approval.required} approval(s) recorded${
                            preflight.activation_approval.policy_name
                              ? ` · ${preflight.activation_approval.policy_name}`
                              : ""
                          }${
                            preflight.activation_approval.approvers.length > 0
                              ? ` · ${preflight.activation_approval.approvers
                                  .map((a) => `${a.approver} ${a.decision}`)
                                  .join(", ")}`
                              : ""
                          }`
                        : "Waiting for a decision."}
                    </div>
                    <div style={{ fontSize: "0.75rem", color: "var(--text-muted)", marginTop: "0.25rem" }}>
                      It is waiting on the approvals queue, under the same permission and
                      the same ledger a node action uses. Approving authorizes activation;
                      you still activate it here.
                    </div>
                  </div>
                ) : null}

                {/* 4. ACTIVATE — the server is the authority. The button
                       reflects the stored contract and the server refuses
                       independently with its own reason. */}
                <div style={{ display: "flex", gap: "0.5rem", alignItems: "center", flexWrap: "wrap" }}>
                  {view.agent.status !== "active" ? (
                    <button
                      className="btn btn-primary"
                      disabled={busy || !activationReady}
                      title={activationBlocker ?? undefined}
                      onClick={() => void transition(view.agent.id, "activate")}
                    >
                      Activate
                    </button>
                  ) : (
                    <button
                      className="btn"
                      disabled={busy}
                      onClick={() => void transition(view.agent.id, "pause")}
                    >
                      Pause
                    </button>
                  )}
                  <button
                    className="btn btn-danger"
                    disabled={busy}
                    onClick={() => void transition(view.agent.id, "retire")}
                  >
                    Retire
                  </button>
                  {activationBlocker ? (
                    <span style={{ fontSize: "0.78rem", color: "var(--text-secondary)" }}>
                      {activationBlocker}
                    </span>
                  ) : null}
                </div>

                {preflight?.contract?.authority ? (
                  <div style={{ fontSize: "0.72rem", color: "var(--text-muted)" }}>
                    {preflight.contract.authority}
                  </div>
                ) : null}
              </div>
            ) : null}

            {/* ── Readiness: the twelve dimensions, as the server
                   composed them. Rendered, never recomputed. ── */}
            {preflight?.exists && preflight.dimensions ? (
              <div style={sectionStyle}>
                <div style={sectionTitle}>
                  Is it ready
                  {preflight.overall ? (
                    <StatusBadge
                      status={VERDICT_LABEL[preflight.overall] ?? preflight.overall}
                      variant={VERDICT_VARIANT[preflight.overall] ?? "neutral"}
                      size="sm"
                    />
                  ) : null}
                </div>
                {!preflightCurrent ? (
                  <div style={{ ...blockStyle, borderLeftColor: "var(--status-warning, #b45309)" }}>
                    This result describes version {preflight.configuration_version}, not the
                    current v{view.agent.version}. It is shown for reference and cannot
                    activate anything.
                  </div>
                ) : null}
                {preflight.dimensions.map((d) => (
                  <div key={d.dimension} style={{ marginBottom: "0.625rem" }}>
                    <div style={{ display: "flex", justifyContent: "space-between", gap: "0.5rem" }}>
                      <span style={{ fontWeight: 600, fontSize: "0.8125rem" }}>
                        {DIMENSION_LABEL[d.dimension] ?? d.dimension.replace(/_/g, " ")}
                      </span>
                      <StatusBadge
                        status={VERDICT_LABEL[d.verdict] ?? d.verdict}
                        variant={VERDICT_VARIANT[d.verdict] ?? "neutral"}
                        size="sm"
                      />
                    </div>
                    <div style={{ fontSize: "0.78rem", color: "var(--text-secondary)" }}>
                      {d.detail}
                    </div>
                  </div>
                ))}
                {preflight.contract?.unknown ? (
                  <div style={{ fontSize: "0.72rem", color: "var(--text-muted)", marginTop: "0.5rem" }}>
                    {preflight.contract.unknown}
                  </div>
                ) : null}
              </div>
            ) : null}

            {/* ── Runtime: only what the platform actually observes ── */}
            {runtime ? (
              <div style={sectionStyle}>
                <div style={sectionTitle}>What it is doing now</div>
                <div style={rowStyle}>
                  <span>Running configuration</span>
                  <span>
                    {runtime.activation_provenance === "recorded"
                      ? `v${runtime.activated_version}`
                      : runtime.activation_provenance === "inactive"
                        ? "not running"
                        : "unknown"}
                    {runtime.configuration_drifted ? (
                      <span style={{ color: "var(--status-warning, #b45309)" }}>
                        {" "}
                        · edited since (now v{runtime.configuration_version})
                      </span>
                    ) : null}
                  </span>
                </div>
                <div style={{ fontSize: "0.75rem", color: "var(--text-muted)", padding: "0 0 0.5rem" }}>
                  {PROVENANCE_NOTE[runtime.activation_provenance]}
                </div>
                <div style={rowStyle}>
                  <span>Last evaluated</span>
                  <span>
                    {runtime.last_evaluated_at
                      ? new Date(runtime.last_evaluated_at).toLocaleString()
                      : "not yet — unknown, not idle"}
                  </span>
                </div>
                <div style={rowStyle}>
                  <span>Devices in scope</span>
                  <span>
                    {runtime.devices.in_scope} · {runtime.devices.seen_recently} seen
                    recently · {runtime.devices.stale} stale
                    {runtime.devices.never_reported > 0 ? (
                      <span style={{ color: "var(--text-muted)" }}>
                        {" "}
                        · {runtime.devices.never_reported} never reported
                      </span>
                    ) : null}
                  </span>
                </div>
                {runtime.devices.never_reported > 0 ? (
                  <div style={{ fontSize: "0.75rem", color: "var(--text-muted)", padding: "0 0 0.5rem" }}>
                    A device the site has never reported is counted as neither healthy nor
                    unhealthy.
                  </div>
                ) : null}
                <div style={rowStyle}>
                  <span>Proposals this window</span>
                  <span>{runtime.proposals_in_window}</span>
                </div>
                <div style={{ ...rowStyle, borderBottom: "none" }}>
                  <span>Preflight</span>
                  <span>
                    {runtime.preflight.exists
                      ? runtime.preflight.current
                        ? `${runtime.preflight.overall}, current`
                        : `${runtime.preflight.overall}, stale`
                      : "none"}
                  </span>
                </div>
              </div>
            ) : null}

            {/* ── Budget and safety: configuration, not a status read ── */}
            {runtime ? (
              <div style={sectionStyle}>
                <div style={sectionTitle}>Budget and safety</div>
                <div style={rowStyle}>
                  <span>Unattended executions used</span>
                  <span>
                    {runtime.budget.limit > 0
                      ? `${runtime.budget.executions_used} of ${runtime.budget.limit} this ${runtime.budget.period}`
                      : `${runtime.budget.executions_used} · no per-agent limit set`}
                    {runtime.budget.exhausted ? (
                      <span style={{ color: "var(--status-warning, #b45309)" }}> · spent</span>
                    ) : null}
                  </span>
                </div>
                {runtime.budget.exhausted ? (
                  <div style={{ ...blockStyle, borderLeftColor: "var(--status-warning, #b45309)" }}>
                    The budget is spent, so nothing runs unattended until it resets. This
                    agent keeps observing, keeps proposing, and still executes whatever a
                    person approves.
                  </div>
                ) : null}
                {canManage ? (
                  <div style={{ display: "flex", gap: "0.5rem", alignItems: "flex-end", flexWrap: "wrap", marginBottom: "0.75rem" }}>
                    <div>
                      <label style={labelStyle} htmlFor="budget-limit">
                        Executions allowed unattended (0 = no per-agent limit)
                      </label>
                      <input
                        id="budget-limit"
                        className="input"
                        type="number"
                        min={0}
                        max={10000}
                        value={budgetDraft.limit}
                        onChange={(e) =>
                          setBudgetDraft((b) => ({ ...b, limit: e.target.value }))
                        }
                        style={{ width: "10rem" }}
                      />
                    </div>
                    <div>
                      <label style={labelStyle} htmlFor="budget-period">
                        Per
                      </label>
                      <select
                        id="budget-period"
                        className="input"
                        value={budgetDraft.period}
                        onChange={(e) =>
                          setBudgetDraft((b) => ({ ...b, period: e.target.value }))
                        }
                      >
                        <option value="daily">day</option>
                        <option value="weekly">week</option>
                        <option value="monthly">month</option>
                      </select>
                    </div>
                    <button
                      className="btn"
                      disabled={busy}
                      onClick={() =>
                        void patchAgent(
                          view.agent.id,
                          {
                            execution_budget: Number(budgetDraft.limit) || 0,
                            budget_period: budgetDraft.period,
                          },
                          "Budget saved. This is configuration, so it bumps the version and the preflight must be re-run.",
                        )
                      }
                    >
                      Save budget
                    </button>
                  </div>
                ) : null}
                <div style={rowStyle}>
                  <span>Paused</span>
                  <span>{runtime.paused_reason ?? "no"}</span>
                </div>
                {canManage ? (
                  <div style={{ display: "flex", gap: "0.5rem", alignItems: "flex-end", flexWrap: "wrap" }}>
                    <div style={{ flex: "1 1 16rem" }}>
                      <label style={labelStyle} htmlFor="pause-reason">
                        Hold this agent (a reason pauses it; clearing it resumes)
                      </label>
                      <input
                        id="pause-reason"
                        className="input"
                        value={pauseDraft}
                        onChange={(e) => setPauseDraft(e.target.value)}
                        placeholder="held by ops during the DC move"
                        style={{ width: "100%" }}
                      />
                    </div>
                    <button
                      className="btn"
                      disabled={busy}
                      onClick={() =>
                        void patchAgent(
                          view.agent.id,
                          { paused_reason: pauseDraft },
                          pauseDraft
                            ? "Held. Nothing runs unattended; it still observes and proposes."
                            : "Resumed.",
                        )
                      }
                    >
                      {pauseDraft ? "Hold" : "Resume"}
                    </button>
                  </div>
                ) : null}
                <div style={{ fontSize: "0.72rem", color: "var(--text-muted)", marginTop: "0.5rem" }}>
                  A hold is a runtime control, so it does not change the configuration
                  version or invalidate an approval. It can only tighten — it cannot
                  resume an agent this tenant or site has stopped.
                </div>
              </div>
            ) : null}

            {/* ── A5: what would you do? (A22.7) ── */}
            <div style={sectionStyle}>
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "space-between",
                  gap: "0.5rem",
                }}
              >
                <div style={sectionTitle}>What would this agent do?</div>
                <button className="btn" disabled={dryRunning} onClick={() => void runDryRun()}>
                  {dryRunning ? "Reasoning…" : "Dry run"}
                </button>
              </div>
              <div style={{ fontSize: "0.78rem", color: "var(--text-secondary)" }}>
                Ask the agent what it would propose against the estate as it stands right
                now. Nothing is created, nothing is dispatched, no budget is spent and no
                decision is recorded.
              </div>

              {dryRun ? (
                <div style={{ marginTop: "0.75rem" }}>
                  <div style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>
                    v{dryRun.agent_version} · {dryRun.devices_in_scope} device
                    {dryRun.devices_in_scope === 1 ? "" : "s"} in scope ·{" "}
                    {new Date(dryRun.evaluated_at).toLocaleString()}
                  </div>

                  {dryRun.would_propose.length === 0 ? (
                    <div
                      style={{
                        fontSize: "0.8125rem",
                        marginTop: "0.5rem",
                        color: "var(--text-secondary)",
                      }}
                    >
                      Nothing to propose. That is a real answer, not a failure — the estate
                      shows nothing this agent is bound to act on.
                    </div>
                  ) : null}

                  {dryRun.would_propose.map((p, i) => (
                    <div key={`${p.device_agent_id}-${p.action_type}-${i}`} style={{ marginTop: "0.75rem" }}>
                      <div
                        style={{ display: "flex", justifyContent: "space-between", gap: "0.5rem" }}
                      >
                        <span style={{ fontWeight: 600, fontSize: "0.8125rem" }}>
                          {p.action_type.replace(/_/g, " ")}{" "}
                          <span style={{ fontFamily: "var(--font-mono, monospace)", fontWeight: 400 }}>
                            {p.device_agent_id}
                          </span>
                        </span>
                        <StatusBadge
                          status={p.requires_human ? "needs a human" : "unattended"}
                          variant={p.requires_human ? "warning" : "success"}
                          size="sm"
                        />
                      </div>
                      {/* The parameters the node would actually receive. Before A5
                          every proposal carried {"reason": …} whatever the class,
                          so four classes could never have executed. */}
                      <div style={{ fontSize: "0.75rem", fontFamily: "var(--font-mono, monospace)", color: "var(--text-muted)" }}>
                        {Object.entries(p.params)
                          .filter(([k]) => k !== "reason")
                          .map(([k, v]) => `${k}=${v}`)
                          .join("  ") || "no parameters"}
                      </div>
                      <div style={{ fontSize: "0.78rem", color: "var(--text-secondary)" }}>
                        {p.rationale}
                      </div>
                      {p.disposition_reason ? (
                        <div style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>
                          {p.disposition_reason}
                        </div>
                      ) : null}
                    </div>
                  ))}

                  {dryRun.withheld.length > 0 ? (
                    <div style={{ marginTop: "1rem" }}>
                      <div style={{ fontWeight: 600, fontSize: "0.8125rem" }}>
                        Considered and withheld
                      </div>
                      {dryRun.withheld.map((w, i) => (
                        <div key={`${w.device_agent_id}-${w.action_type}-${i}`} style={rowStyle}>
                          <span style={{ fontFamily: "var(--font-mono, monospace)" }}>
                            {w.action_type.replace(/_/g, " ")} · {w.device_agent_id}
                          </span>
                          <span style={{ color: "var(--text-muted)" }}>{w.reason}</span>
                        </div>
                      ))}
                    </div>
                  ) : null}

                  <div style={{ fontSize: "0.72rem", color: "var(--text-muted)", marginTop: "0.75rem" }}>
                    {dryRun.contract.same_reasoning} {dryRun.contract.wrote_nothing}
                  </div>
                </div>
              ) : null}
            </div>

            {/* ── Skills: bindings, and where they actually landed ── */}
            {(preflight?.skills?.length ?? 0) > 0 || (runtime?.skills_by_id?.length ?? 0) > 0 ? (
              <div style={sectionStyle}>
                <div style={sectionTitle}>Skills</div>
                {(preflight?.skills ?? []).map((s) => (
                  <div key={s.skill_id} style={{ marginBottom: "0.75rem" }}>
                    <div style={{ display: "flex", justifyContent: "space-between", gap: "0.5rem" }}>
                      <span style={{ fontWeight: 600, fontSize: "0.8125rem" }}>
                        {s.name || s.skill_id}
                      </span>
                      <StatusBadge
                        status={
                          s.usable === true ? "usable" : s.usable === false ? "unusable" : "unknown"
                        }
                        variant={
                          s.usable === true ? "success" : s.usable === false ? "critical" : "neutral"
                        }
                        size="sm"
                      />
                    </div>
                    <div style={{ fontSize: "0.78rem", color: "var(--text-secondary)" }}>
                      {s.reason}
                    </div>
                    {s.recommended.length > 0 ? (
                      <div style={{ fontSize: "0.75rem", color: "var(--text-muted)" }}>
                        Recommends {s.recommended.map((a) => a.replace(/_/g, " ")).join(", ")}
                      </div>
                    ) : null}
                  </div>
                ))}
                {(runtime?.skills_by_id ?? []).map((s) => (
                  <div key={s.skill_id} style={{ marginBottom: "0.75rem" }}>
                    <div style={{ fontWeight: 600, fontSize: "0.8125rem" }}>
                      {s.skill_id} — where it landed
                    </div>
                    {s.devices.map((d) => (
                      <div key={d.device_agent_id} style={rowStyle}>
                        <span style={{ fontFamily: "var(--font-mono, monospace)" }}>
                          {d.device_agent_id}
                        </span>
                        <span style={{ color: "var(--text-muted)" }}>
                          {d.status}
                          {d.detail ? ` · ${d.detail}` : ""}
                        </span>
                      </div>
                    ))}
                  </div>
                ))}
                <div style={{ fontSize: "0.72rem", color: "var(--text-muted)" }}>
                  A skill composes capabilities this agent already holds. It never widens
                  permission, scope, capability, autonomy or approval authority, and it
                  installs only onto devices in this agent's own scope that can run what it
                  recommends.
                </div>
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
