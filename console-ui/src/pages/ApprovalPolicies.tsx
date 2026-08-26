import { type CSSProperties, useCallback, useEffect, useMemo, useState } from "react";
import PageHeader from "../components/PageHeader";
import DataTable, { type Column } from "../components/DataTable";
import DetailPanel from "../components/DetailPanel";
import ConfirmDialog from "../components/ConfirmDialog";
import StatusBadge from "../components/StatusBadge";
import EmptyState from "../components/EmptyState";
import Toast from "../components/Toast";
import Spinner from "../components/Spinner";
import { useToast } from "../components/useToast";
import { getJson, postJson, patchJson, deleteJson } from "../api";

/* ── Types ────────────────────────────────────────── */

interface Policy {
  id: string;
  name: string;
  device_type: string;
  action_type: string;
  risk_level: string;
  time_window: string | null;
  approval_mode: string;
  required_approvers: number;
  approval_group_id: string | null;
  approval_group_name: string | null;
  status: "active" | "disabled";
  created_at: string;
  updated_at: string;
}

interface ApprovalGroup {
  id: string;
  name: string;
  members_count: number;
  required_approvals: number;
  slack_channel: string | null;
  github_team: string | null;
  created_at: string;
  members: GroupMember[];
}

interface GroupMember {
  id: string;
  email: string;
  role: string;
}

interface AutonomyBudget {
  id: string;
  device_type: string;
  level: number;
  budget_limit: number;
  period: string;
  actions_used: number;
  learning_ramp: boolean;
  ramp_start: string | null;
  ramp_increment: number | null;
  created_at: string;
}

interface PolicyFormData {
  name: string;
  device_type: string;
  action_type: string;
  risk_level: string;
  time_window: string;
  approval_mode: string;
  required_approvers: number;
  approval_group_id: string;
}

interface GroupFormData {
  name: string;
  required_approvals: number;
  slack_channel: string;
  github_team: string;
}

interface BudgetFormData {
  device_type: string;
  level: number;
  budget_limit: number;
  period: string;
  learning_ramp: boolean;
  ramp_increment: number;
}

/* ── Constants ────────────────────────────────────── */

const PAGE_SIZE = 20;

const RISK_VARIANT: Record<string, "success" | "warning" | "critical" | "info" | "neutral"> = {
  low: "info",
  medium: "warning",
  high: "critical",
  critical: "critical",
};

const LEVEL_CONFIG: Record<number, { label: string; variant: "neutral" | "info" | "warning" | "success" }> = {
  0: { label: "Observe", variant: "neutral" },
  1: { label: "Suggest", variant: "info" },
  2: { label: "Batch", variant: "warning" },
  3: { label: "Autonomous", variant: "success" },
};

const DEVICE_TYPE_OPTIONS = [
  { value: "*", label: "All Devices" },
  { value: "server", label: "Server" },
  { value: "switch", label: "Switch" },
  { value: "storage", label: "Storage" },
  { value: "pdu", label: "PDU" },
];

const ACTION_TYPE_OPTIONS = [
  { value: "*", label: "All Actions" },
  { value: "IDENTIFY_LED", label: "Identify LED" },
  { value: "COLLECT_DIAGNOSTICS", label: "Collect Diagnostics" },
  { value: "RESTART_BMC", label: "Restart BMC" },
  { value: "POWER_CYCLE", label: "Power Cycle" },
  { value: "FIRMWARE_UPDATE", label: "Firmware Update" },
  { value: "CLEAR_SEL", label: "Clear SEL" },
];

const RISK_LEVEL_OPTIONS = [
  { value: "low", label: "Low" },
  { value: "medium", label: "Medium" },
  { value: "high", label: "High" },
  { value: "critical", label: "Critical" },
];

const APPROVAL_MODE_OPTIONS = [
  { value: "auto_approve", label: "Auto Approve" },
  { value: "require_approval", label: "Require Approval" },
  { value: "escalate", label: "Escalate" },
];

const PERIOD_OPTIONS = [
  { value: "daily", label: "Daily" },
  { value: "weekly", label: "Weekly" },
  { value: "monthly", label: "Monthly" },
];

type PolicyTemplate = { label: string; fill: Partial<PolicyFormData> };

const POLICY_TEMPLATES: PolicyTemplate[] = [
  {
    label: "Conservative",
    fill: { approval_mode: "require_approval", required_approvers: 2 },
  },
  {
    label: "Balanced",
    fill: { approval_mode: "auto_approve", risk_level: "low", required_approvers: 1 },
  },
  {
    label: "Aggressive",
    fill: { approval_mode: "auto_approve", risk_level: "medium", required_approvers: 1 },
  },
];

/* ── Styles ───────────────────────────────────────── */

const tabBarStyle: CSSProperties = {
  display: "flex",
  gap: "0",
  borderBottom: "1px solid var(--border-color)",
  marginBottom: "1.25rem",
};

const tabStyle: CSSProperties = {
  padding: "0.625rem 1.25rem",
  fontSize: "0.875rem",
  fontWeight: 500,
  color: "var(--text-secondary)",
  background: "transparent",
  border: "none",
  borderBottom: "2px solid transparent",
  cursor: "pointer",
  fontFamily: "inherit",
  transition: "color var(--transition), border-color var(--transition)",
};

const tabActiveStyle: CSSProperties = {
  ...tabStyle,
  color: "var(--accent)",
  borderBottomColor: "var(--accent)",
  fontWeight: 600,
};

const modalOverlay: CSSProperties = {
  position: "fixed",
  inset: 0,
  background: "var(--bg-overlay)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  zIndex: 2000,
};

const modalCard: CSSProperties = {
  background: "var(--bg-card)",
  borderRadius: "var(--radius-lg)",
  boxShadow: "var(--shadow-lg)",
  padding: "1.5rem",
  maxWidth: 520,
  width: "90vw",
  maxHeight: "85vh",
  overflow: "auto",
};

const formGroup: CSSProperties = {
  marginBottom: "1rem",
};

const labelStyle: CSSProperties = {
  display: "block",
  fontSize: "0.8125rem",
  fontWeight: 600,
  color: "var(--text-secondary)",
  marginBottom: "0.375rem",
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

const templateBtnRow: CSSProperties = {
  display: "flex",
  gap: "0.5rem",
  marginBottom: "1rem",
};

/* ── Helpers ──────────────────────────────────────── */

const emptyPolicyForm: PolicyFormData = {
  name: "",
  device_type: "*",
  action_type: "*",
  risk_level: "low",
  time_window: "",
  approval_mode: "require_approval",
  required_approvers: 1,
  approval_group_id: "",
};

const emptyGroupForm: GroupFormData = {
  name: "",
  required_approvals: 1,
  slack_channel: "",
  github_team: "",
};

const emptyBudgetForm: BudgetFormData = {
  device_type: "*",
  level: 0,
  budget_limit: 10,
  period: "daily",
  learning_ramp: false,
  ramp_increment: 1,
};

/* ── Component ────────────────────────────────────── */

export default function ApprovalPolicies() {
  const { toasts, toast, dismiss } = useToast();

  /* ── Tab state ──────────────────────────────────── */
  const [activeTab, setActiveTab] = useState<"policies" | "groups" | "budgets">("policies");

  /* ── Policies state ────────────────────────────── */
  const [policies, setPolicies] = useState<Policy[]>([]);
  const [polTotal, setPolTotal] = useState(0);
  const [polPage, setPolPage] = useState(1);
  const [polLoading, setPolLoading] = useState(true);
  const [polModalOpen, setPolModalOpen] = useState(false);
  const [polForm, setPolForm] = useState<PolicyFormData>({ ...emptyPolicyForm });
  const [polEditing, setPolEditing] = useState<string | null>(null);
  const [polSaving, setPolSaving] = useState(false);
  const [polDeleteConfirm, setPolDeleteConfirm] = useState<Policy | null>(null);
  const [polDeleting, setPolDeleting] = useState(false);

  /* ── Groups state ──────────────────────────────── */
  const [groups, setGroups] = useState<ApprovalGroup[]>([]);
  const [grpTotal, setGrpTotal] = useState(0);
  const [grpPage, setGrpPage] = useState(1);
  const [grpLoading, setGrpLoading] = useState(true);
  const [grpModalOpen, setGrpModalOpen] = useState(false);
  const [grpForm, setGrpForm] = useState<GroupFormData>({ ...emptyGroupForm });
  const [grpEditing, setGrpEditing] = useState<string | null>(null);
  const [grpSaving, setGrpSaving] = useState(false);
  const [grpDeleteConfirm, setGrpDeleteConfirm] = useState<ApprovalGroup | null>(null);
  const [grpDeleting, setGrpDeleting] = useState(false);
  const [grpDetailOpen, setGrpDetailOpen] = useState(false);
  const [grpSelected, setGrpSelected] = useState<ApprovalGroup | null>(null);
  const [grpDetailLoading, setGrpDetailLoading] = useState(false);
  const [addMemberEmail, setAddMemberEmail] = useState("");
  const [addMemberRole, setAddMemberRole] = useState("approver");

  /* ── Budgets state ─────────────────────────────── */
  const [budgets, setBudgets] = useState<AutonomyBudget[]>([]);
  const [budTotal, setBudTotal] = useState(0);
  const [budPage, setBudPage] = useState(1);
  const [budLoading, setBudLoading] = useState(true);
  const [budModalOpen, setBudModalOpen] = useState(false);
  const [budForm, setBudForm] = useState<BudgetFormData>({ ...emptyBudgetForm });
  const [budEditing, setBudEditing] = useState<string | null>(null);
  const [budSaving, setBudSaving] = useState(false);
  const [budDeleteConfirm, setBudDeleteConfirm] = useState<AutonomyBudget | null>(null);
  const [budDeleting, setBudDeleting] = useState(false);

  /* ═══════════════════════════════════════════════════
     POLICIES TAB
     ═══════════════════════════════════════════════════ */

  const fetchPolicies = useCallback(async () => {
    try {
      const params = new URLSearchParams();
      params.set("page", String(polPage));
      params.set("page_size", String(PAGE_SIZE));
      // QA ISSUE-008: CC returns {policies,...}, not {items,...}
      const res = await getJson<{ policies: Policy[]; total: number }>(`/api/policies?${params.toString()}`);
      setPolicies(res.policies ?? []);
      setPolTotal(res.total ?? 0);
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to load policies", "error");
    } finally {
      setPolLoading(false);
    }
  }, [polPage, toast]);

  useEffect(() => {
    if (activeTab === "policies") {
      setPolLoading(true);
      void fetchPolicies();
    }
  }, [activeTab, fetchPolicies]);

  const handleSavePolicy = useCallback(async () => {
    if (!polForm.name.trim()) {
      toast("Policy name is required", "error");
      return;
    }
    setPolSaving(true);
    try {
      const payload = {
        ...polForm,
        time_window: polForm.time_window || null,
        approval_group_id: polForm.approval_group_id || null,
      };
      if (polEditing) {
        await patchJson(`/api/policies/${polEditing}`, payload);
        toast("Policy updated", "success");
      } else {
        await postJson("/api/policies", payload);
        toast("Policy created", "success");
      }
      setPolModalOpen(false);
      setPolForm({ ...emptyPolicyForm });
      setPolEditing(null);
      void fetchPolicies();
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to save policy", "error");
    } finally {
      setPolSaving(false);
    }
  }, [polForm, polEditing, toast, fetchPolicies]);

  const handleDeletePolicy = useCallback(async () => {
    if (!polDeleteConfirm) return;
    setPolDeleting(true);
    try {
      await deleteJson(`/api/policies/${polDeleteConfirm.id}`);
      toast("Policy deleted", "success");
      setPolDeleteConfirm(null);
      void fetchPolicies();
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to delete policy", "error");
    } finally {
      setPolDeleting(false);
    }
  }, [polDeleteConfirm, toast, fetchPolicies]);

  const openEditPolicy = useCallback((policy: Policy) => {
    setPolForm({
      name: policy.name,
      device_type: policy.device_type,
      action_type: policy.action_type,
      risk_level: policy.risk_level,
      time_window: policy.time_window ?? "",
      approval_mode: policy.approval_mode,
      required_approvers: policy.required_approvers,
      approval_group_id: policy.approval_group_id ?? "",
    });
    setPolEditing(policy.id);
    setPolModalOpen(true);
  }, []);

  const policyColumns = useMemo<Column<Policy>[]>(
    () => [
      { key: "name", header: "Name" },
      { key: "device_type", header: "Device Type" },
      { key: "action_type", header: "Action Type" },
      {
        key: "risk_level",
        header: "Risk Level",
        render: (r) => (
          <StatusBadge
            status={r.risk_level}
            variant={RISK_VARIANT[r.risk_level] ?? "neutral"}
            size="sm"
          />
        ),
      },
      {
        key: "approval_mode",
        header: "Approval Mode",
        render: (r) => r.approval_mode.replace(/_/g, " "),
      },
      {
        key: "status",
        header: "Status",
        render: (r) => (
          <StatusBadge
            status={r.status}
            variant={r.status === "active" ? "success" : "neutral"}
            size="sm"
          />
        ),
      },
    ],
    [],
  );

  /* ═══════════════════════════════════════════════════
     GROUPS TAB
     ═══════════════════════════════════════════════════ */

  const fetchGroups = useCallback(async () => {
    try {
      const params = new URLSearchParams();
      params.set("page", String(grpPage));
      params.set("page_size", String(PAGE_SIZE));
      const res = await getJson<{ groups: ApprovalGroup[]; total: number }>(`/api/policies/groups?${params.toString()}`);
      setGroups(res.groups ?? []);
      setGrpTotal(res.total ?? 0);
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to load groups", "error");
    } finally {
      setGrpLoading(false);
    }
  }, [grpPage, toast]);

  useEffect(() => {
    if (activeTab === "groups") {
      setGrpLoading(true);
      void fetchGroups();
    }
  }, [activeTab, fetchGroups]);

  const handleSaveGroup = useCallback(async () => {
    if (!grpForm.name.trim()) {
      toast("Group name is required", "error");
      return;
    }
    setGrpSaving(true);
    try {
      const payload = {
        ...grpForm,
        slack_channel: grpForm.slack_channel || null,
        github_team: grpForm.github_team || null,
      };
      if (grpEditing) {
        await patchJson(`/api/policies/groups/${grpEditing}`, payload);
        toast("Group updated", "success");
      } else {
        await postJson("/api/policies/groups", payload);
        toast("Group created", "success");
      }
      setGrpModalOpen(false);
      setGrpForm({ ...emptyGroupForm });
      setGrpEditing(null);
      void fetchGroups();
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to save group", "error");
    } finally {
      setGrpSaving(false);
    }
  }, [grpForm, grpEditing, toast, fetchGroups]);

  const handleDeleteGroup = useCallback(async () => {
    if (!grpDeleteConfirm) return;
    setGrpDeleting(true);
    try {
      await deleteJson(`/api/policies/groups/${grpDeleteConfirm.id}`);
      toast("Group deleted", "success");
      setGrpDeleteConfirm(null);
      void fetchGroups();
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to delete group", "error");
    } finally {
      setGrpDeleting(false);
    }
  }, [grpDeleteConfirm, toast, fetchGroups]);

  const openGroupDetail = useCallback(
    async (group: ApprovalGroup) => {
      setGrpDetailOpen(true);
      setGrpDetailLoading(true);
      try {
        const detail = await getJson<ApprovalGroup>(`/api/policies/groups/${group.id}`);
        setGrpSelected(detail);
      } catch (err) {
        toast(err instanceof Error ? err.message : "Failed to load group", "error");
        setGrpDetailOpen(false);
      } finally {
        setGrpDetailLoading(false);
      }
    },
    [toast],
  );

  const handleAddMember = useCallback(async () => {
    if (!grpSelected || !addMemberEmail.trim()) return;
    try {
      await postJson(`/api/policies/groups/${grpSelected.id}/members`, {
        email: addMemberEmail,
        role: addMemberRole,
      });
      toast("Member added", "success");
      setAddMemberEmail("");
      // Refresh detail
      const detail = await getJson<ApprovalGroup>(`/api/policies/groups/${grpSelected.id}`);
      setGrpSelected(detail);
      void fetchGroups();
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to add member", "error");
    }
  }, [grpSelected, addMemberEmail, addMemberRole, toast, fetchGroups]);

  const handleRemoveMember = useCallback(
    async (memberId: string) => {
      if (!grpSelected) return;
      try {
        await deleteJson(`/api/policies/groups/${grpSelected.id}/members/${memberId}`);
        toast("Member removed", "success");
        const detail = await getJson<ApprovalGroup>(`/api/policies/groups/${grpSelected.id}`);
        setGrpSelected(detail);
        void fetchGroups();
      } catch (err) {
        toast(err instanceof Error ? err.message : "Failed to remove member", "error");
      }
    },
    [grpSelected, toast, fetchGroups],
  );

  const groupColumns = useMemo<Column<ApprovalGroup>[]>(
    () => [
      { key: "name", header: "Name" },
      {
        key: "members_count",
        header: "Members",
        render: (r) => String(r.members_count),
      },
      {
        key: "required_approvals",
        header: "Required Approvals",
        render: (r) => `${r.required_approvals} of ${r.members_count}`,
      },
      {
        key: "slack_channel",
        header: "Slack Channel",
        render: (r) => r.slack_channel || "--",
      },
    ],
    [],
  );

  /* ═══════════════════════════════════════════════════
     BUDGETS TAB
     ═══════════════════════════════════════════════════ */

  const fetchBudgets = useCallback(async () => {
    try {
      const params = new URLSearchParams();
      params.set("page", String(budPage));
      params.set("page_size", String(PAGE_SIZE));
      const res = await getJson<{ budgets: AutonomyBudget[]; total: number }>(`/api/policies/autonomy?${params.toString()}`);
      setBudgets(res.budgets ?? []);
      setBudTotal(res.total ?? 0);
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to load budgets", "error");
    } finally {
      setBudLoading(false);
    }
  }, [budPage, toast]);

  useEffect(() => {
    if (activeTab === "budgets") {
      setBudLoading(true);
      void fetchBudgets();
    }
  }, [activeTab, fetchBudgets]);

  const handleSaveBudget = useCallback(async () => {
    setBudSaving(true);
    try {
      const payload = {
        ...budForm,
        ramp_increment: budForm.learning_ramp ? budForm.ramp_increment : null,
      };
      if (budEditing) {
        await patchJson(`/api/policies/autonomy/${budEditing}`, payload);
        toast("Budget updated", "success");
      } else {
        await postJson("/api/policies/autonomy", payload);
        toast("Budget created", "success");
      }
      setBudModalOpen(false);
      setBudForm({ ...emptyBudgetForm });
      setBudEditing(null);
      void fetchBudgets();
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to save budget", "error");
    } finally {
      setBudSaving(false);
    }
  }, [budForm, budEditing, toast, fetchBudgets]);

  const handleDeleteBudget = useCallback(async () => {
    if (!budDeleteConfirm) return;
    setBudDeleting(true);
    try {
      await deleteJson(`/api/policies/autonomy/${budDeleteConfirm.id}`);
      toast("Budget deleted", "success");
      setBudDeleteConfirm(null);
      void fetchBudgets();
    } catch (err) {
      toast(err instanceof Error ? err.message : "Failed to delete budget", "error");
    } finally {
      setBudDeleting(false);
    }
  }, [budDeleteConfirm, toast, fetchBudgets]);

  const budgetColumns = useMemo<Column<AutonomyBudget>[]>(
    () => [
      { key: "device_type", header: "Device Type" },
      {
        key: "level",
        header: "Level",
        render: (r) => {
          const cfg = LEVEL_CONFIG[r.level] ?? LEVEL_CONFIG[0];
          return <StatusBadge status={cfg.label} variant={cfg.variant} size="sm" />;
        },
      },
      { key: "budget_limit", header: "Budget Limit", render: (r) => String(r.budget_limit) },
      { key: "period", header: "Period", render: (r) => r.period },
      {
        key: "actions_used",
        header: "Actions Used",
        render: (r) => `${r.actions_used} / ${r.budget_limit}`,
      },
    ],
    [],
  );

  /* ── Render ─────────────────────────────────────── */
  return (
    <div>
      <Toast toasts={toasts} onDismiss={dismiss} />

      <PageHeader
        title="Approval Policies"
        breadcrumbs={[{ label: "HarkenIQ" }, { label: "Operations" }, { label: "Policies" }]}
      />

      {/* Tabs */}
      <div style={tabBarStyle}>
        <button
          style={activeTab === "policies" ? tabActiveStyle : tabStyle}
          onClick={() => setActiveTab("policies")}
        >
          Policies
        </button>
        <button
          style={activeTab === "groups" ? tabActiveStyle : tabStyle}
          onClick={() => setActiveTab("groups")}
        >
          Approval Groups
        </button>
        <button
          style={activeTab === "budgets" ? tabActiveStyle : tabStyle}
          onClick={() => setActiveTab("budgets")}
        >
          Autonomy Budgets
        </button>
      </div>

      {/* ═══ Policies Tab ═══════════════════════════ */}
      {activeTab === "policies" && (
        <>
          <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: "1rem" }}>
            <button
              className="btn btn-primary"
              onClick={() => {
                setPolForm({ ...emptyPolicyForm });
                setPolEditing(null);
                setPolModalOpen(true);
              }}
            >
              Create Policy
            </button>
          </div>

          {!polLoading && policies.length === 0 ? (
            <EmptyState
              title="No policies defined"
              description="Create your first approval policy to control action authorization."
              actionLabel="Create Policy"
              onAction={() => {
                setPolForm({ ...emptyPolicyForm });
                setPolEditing(null);
                setPolModalOpen(true);
              }}
              icon="&#x2699;"
            />
          ) : (
            <DataTable<Policy>
              columns={policyColumns}
              data={policies}
              loading={polLoading}
              emptyMessage="No policies"
              page={polPage}
              pageSize={PAGE_SIZE}
              total={polTotal}
              onPageChange={setPolPage}
              rowActions={[
                { label: "Edit", onClick: (r) => openEditPolicy(r) },
                { label: "Delete", variant: "danger", onClick: (r) => setPolDeleteConfirm(r) },
              ]}
              striped
            />
          )}

          {/* Policy modal */}
          {polModalOpen && (
            <div style={modalOverlay} onClick={() => { setPolModalOpen(false); setPolEditing(null); }} role="presentation">
              <div
                role="dialog"
                aria-modal="true"
                aria-labelledby="policy-modal-title"
                style={modalCard}
                onClick={(e) => e.stopPropagation()}
              >
                <h3 id="policy-modal-title" style={{ margin: "0 0 1rem 0", fontSize: "1.125rem", fontWeight: 600 }}>
                  {polEditing ? "Edit Policy" : "Create Policy"}
                </h3>

                {/* Templates */}
                {!polEditing && (
                  <div>
                    <label style={{ ...labelStyle, marginBottom: "0.5rem" }}>Quick Fill Template</label>
                    <div style={templateBtnRow}>
                      {POLICY_TEMPLATES.map((tpl) => (
                        <button
                          key={tpl.label}
                          className="btn"
                          style={{ fontSize: "0.75rem" }}
                          onClick={() => setPolForm((prev) => ({ ...prev, ...tpl.fill }))}
                        >
                          {tpl.label}
                        </button>
                      ))}
                    </div>
                  </div>
                )}

                <div style={formGroup}>
                  <label style={labelStyle}>Name *</label>
                  <input
                    className="input"
                    type="text"
                    value={polForm.name}
                    onChange={(e) => setPolForm((prev) => ({ ...prev, name: e.target.value }))}
                    placeholder="e.g. Low-risk auto-approve"
                    style={{ width: "100%" }}
                  />
                </div>

                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "1rem" }}>
                  <div style={formGroup}>
                    <label style={labelStyle}>Device Type</label>
                    <select
                      className="select"
                      value={polForm.device_type}
                      onChange={(e) => setPolForm((prev) => ({ ...prev, device_type: e.target.value }))}
                      style={{ width: "100%" }}
                    >
                      {DEVICE_TYPE_OPTIONS.map((o) => (
                        <option key={o.value} value={o.value}>{o.label}</option>
                      ))}
                    </select>
                  </div>
                  <div style={formGroup}>
                    <label style={labelStyle}>Action Type</label>
                    <select
                      className="select"
                      value={polForm.action_type}
                      onChange={(e) => setPolForm((prev) => ({ ...prev, action_type: e.target.value }))}
                      style={{ width: "100%" }}
                    >
                      {ACTION_TYPE_OPTIONS.map((o) => (
                        <option key={o.value} value={o.value}>{o.label}</option>
                      ))}
                    </select>
                  </div>
                </div>

                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "1rem" }}>
                  <div style={formGroup}>
                    <label style={labelStyle}>Risk Level</label>
                    <select
                      className="select"
                      value={polForm.risk_level}
                      onChange={(e) => setPolForm((prev) => ({ ...prev, risk_level: e.target.value }))}
                      style={{ width: "100%" }}
                    >
                      {RISK_LEVEL_OPTIONS.map((o) => (
                        <option key={o.value} value={o.value}>{o.label}</option>
                      ))}
                    </select>
                  </div>
                  <div style={formGroup}>
                    <label style={labelStyle}>Approval Mode</label>
                    <select
                      className="select"
                      value={polForm.approval_mode}
                      onChange={(e) => setPolForm((prev) => ({ ...prev, approval_mode: e.target.value }))}
                      style={{ width: "100%" }}
                    >
                      {APPROVAL_MODE_OPTIONS.map((o) => (
                        <option key={o.value} value={o.value}>{o.label}</option>
                      ))}
                    </select>
                  </div>
                </div>

                <div style={formGroup}>
                  <label style={labelStyle}>Time Window (optional, cron-like)</label>
                  <input
                    className="input"
                    type="text"
                    value={polForm.time_window}
                    onChange={(e) => setPolForm((prev) => ({ ...prev, time_window: e.target.value }))}
                    placeholder="e.g. 0 2-6 * * * (2am-6am)"
                    style={{ width: "100%" }}
                  />
                </div>

                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "1rem" }}>
                  <div style={formGroup}>
                    <label style={labelStyle}>Required Approvers</label>
                    <input
                      className="input"
                      type="number"
                      min={1}
                      value={polForm.required_approvers}
                      onChange={(e) => setPolForm((prev) => ({ ...prev, required_approvers: parseInt(e.target.value, 10) || 1 }))}
                      style={{ width: "100%" }}
                    />
                  </div>
                  <div style={formGroup}>
                    <label style={labelStyle}>Approval Group</label>
                    <select
                      className="select"
                      value={polForm.approval_group_id}
                      onChange={(e) => setPolForm((prev) => ({ ...prev, approval_group_id: e.target.value }))}
                      style={{ width: "100%" }}
                    >
                      <option value="">None</option>
                      {groups.map((g) => (
                        <option key={g.id} value={g.id}>{g.name}</option>
                      ))}
                    </select>
                  </div>
                </div>

                <div style={{ display: "flex", justifyContent: "flex-end", gap: "0.5rem", marginTop: "1.25rem" }}>
                  <button
                    className="btn"
                    onClick={() => { setPolModalOpen(false); setPolEditing(null); }}
                    disabled={polSaving}
                  >
                    Cancel
                  </button>
                  <button className="btn btn-primary" onClick={handleSavePolicy} disabled={polSaving}>
                    {polSaving && <Spinner size="sm" />}
                    {polEditing ? "Update" : "Create"}
                  </button>
                </div>
              </div>
            </div>
          )}

          <ConfirmDialog
            open={polDeleteConfirm !== null}
            title="Delete Policy"
            message={`Delete policy "${polDeleteConfirm?.name}"? This cannot be undone.`}
            confirmLabel="Delete"
            variant="danger"
            onConfirm={handleDeletePolicy}
            onCancel={() => setPolDeleteConfirm(null)}
            loading={polDeleting}
          />
        </>
      )}

      {/* ═══ Groups Tab ════════════════════════════ */}
      {activeTab === "groups" && (
        <>
          <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: "1rem" }}>
            <button
              className="btn btn-primary"
              onClick={() => {
                setGrpForm({ ...emptyGroupForm });
                setGrpEditing(null);
                setGrpModalOpen(true);
              }}
            >
              Create Group
            </button>
          </div>

          {!grpLoading && groups.length === 0 ? (
            <EmptyState
              title="No approval groups"
              description="Create groups to organize approvers and route approval requests."
              actionLabel="Create Group"
              onAction={() => {
                setGrpForm({ ...emptyGroupForm });
                setGrpEditing(null);
                setGrpModalOpen(true);
              }}
              icon="&#x263A;"
            />
          ) : (
            <DataTable<ApprovalGroup>
              columns={groupColumns}
              data={groups}
              loading={grpLoading}
              emptyMessage="No groups"
              page={grpPage}
              pageSize={PAGE_SIZE}
              total={grpTotal}
              onPageChange={setGrpPage}
              onRowClick={openGroupDetail}
              rowActions={[
                {
                  label: "Edit",
                  onClick: (r) => {
                    setGrpForm({
                      name: r.name,
                      required_approvals: r.required_approvals,
                      slack_channel: r.slack_channel ?? "",
                      github_team: r.github_team ?? "",
                    });
                    setGrpEditing(r.id);
                    setGrpModalOpen(true);
                  },
                },
                { label: "Delete", variant: "danger", onClick: (r) => setGrpDeleteConfirm(r) },
              ]}
              striped
            />
          )}

          {/* Group modal */}
          {grpModalOpen && (
            <div style={modalOverlay} onClick={() => { setGrpModalOpen(false); setGrpEditing(null); }} role="presentation">
              <div
                role="dialog"
                aria-modal="true"
                aria-labelledby="group-modal-title"
                style={modalCard}
                onClick={(e) => e.stopPropagation()}
              >
                <h3 id="group-modal-title" style={{ margin: "0 0 1rem 0", fontSize: "1.125rem", fontWeight: 600 }}>
                  {grpEditing ? "Edit Group" : "Create Group"}
                </h3>

                <div style={formGroup}>
                  <label style={labelStyle}>Name *</label>
                  <input
                    className="input"
                    type="text"
                    value={grpForm.name}
                    onChange={(e) => setGrpForm((prev) => ({ ...prev, name: e.target.value }))}
                    placeholder="e.g. SRE Team"
                    style={{ width: "100%" }}
                  />
                </div>
                <div style={formGroup}>
                  <label style={labelStyle}>Required Approvals</label>
                  <input
                    className="input"
                    type="number"
                    min={1}
                    value={grpForm.required_approvals}
                    onChange={(e) => setGrpForm((prev) => ({ ...prev, required_approvals: parseInt(e.target.value, 10) || 1 }))}
                    style={{ width: "100%" }}
                  />
                </div>
                <div style={formGroup}>
                  <label style={labelStyle}>Slack Channel (optional)</label>
                  <input
                    className="input"
                    type="text"
                    value={grpForm.slack_channel}
                    onChange={(e) => setGrpForm((prev) => ({ ...prev, slack_channel: e.target.value }))}
                    placeholder="#approvals"
                    style={{ width: "100%" }}
                  />
                </div>
                <div style={formGroup}>
                  <label style={labelStyle}>GitHub Team (optional)</label>
                  <input
                    className="input"
                    type="text"
                    value={grpForm.github_team}
                    onChange={(e) => setGrpForm((prev) => ({ ...prev, github_team: e.target.value }))}
                    placeholder="org/team-name"
                    style={{ width: "100%" }}
                  />
                </div>

                <div style={{ display: "flex", justifyContent: "flex-end", gap: "0.5rem", marginTop: "1.25rem" }}>
                  <button
                    className="btn"
                    onClick={() => { setGrpModalOpen(false); setGrpEditing(null); }}
                    disabled={grpSaving}
                  >
                    Cancel
                  </button>
                  <button className="btn btn-primary" onClick={handleSaveGroup} disabled={grpSaving}>
                    {grpSaving && <Spinner size="sm" />}
                    {grpEditing ? "Update" : "Create"}
                  </button>
                </div>
              </div>
            </div>
          )}

          {/* Group detail panel */}
          <DetailPanel
            open={grpDetailOpen}
            onClose={() => {
              setGrpDetailOpen(false);
              setGrpSelected(null);
              setAddMemberEmail("");
            }}
            title={grpSelected?.name ?? "Group Details"}
            subtitle={grpSelected ? `${grpSelected.required_approvals} of ${grpSelected.members_count} required` : undefined}
            width={480}
          >
            {grpDetailLoading ? (
              <div style={{ display: "flex", justifyContent: "center", padding: "2rem" }}>
                <Spinner size="md" />
              </div>
            ) : grpSelected ? (
              <>
                <div style={sectionTitle}>Group Info</div>
                <div style={detailRow}>
                  <span style={detailLabel}>Name</span>
                  <span style={detailValue}>{grpSelected.name}</span>
                </div>
                <div style={detailRow}>
                  <span style={detailLabel}>Required Approvals</span>
                  <span style={detailValue}>{grpSelected.required_approvals}</span>
                </div>
                <div style={detailRow}>
                  <span style={detailLabel}>Slack Channel</span>
                  <span style={detailValue}>{grpSelected.slack_channel || "--"}</span>
                </div>
                <div style={detailRow}>
                  <span style={detailLabel}>GitHub Team</span>
                  <span style={detailValue}>{grpSelected.github_team || "--"}</span>
                </div>

                <div style={sectionTitle}>Members ({grpSelected.members?.length ?? 0})</div>
                {grpSelected.members && grpSelected.members.length > 0 ? (
                  grpSelected.members.map((m) => (
                    <div
                      key={m.id}
                      style={{
                        display: "flex",
                        justifyContent: "space-between",
                        alignItems: "center",
                        padding: "0.375rem 0.5rem",
                        fontSize: "0.8125rem",
                        background: "var(--bg-primary)",
                        borderRadius: "var(--radius-md)",
                        marginBottom: "0.25rem",
                      }}
                    >
                      <div>
                        <span style={{ fontWeight: 500 }}>{m.email}</span>
                        <span style={{ marginLeft: "0.5rem", color: "var(--text-muted)", fontSize: "0.75rem" }}>
                          {m.role}
                        </span>
                      </div>
                      <button
                        className="btn btn-danger"
                        style={{ padding: "0.125rem 0.5rem", fontSize: "0.6875rem" }}
                        onClick={() => handleRemoveMember(m.id)}
                      >
                        Remove
                      </button>
                    </div>
                  ))
                ) : (
                  <div style={{ fontSize: "0.8125rem", color: "var(--text-muted)" }}>No members yet</div>
                )}

                <div style={{ ...sectionTitle, marginTop: "1.5rem" }}>Add Member</div>
                <div style={{ display: "flex", gap: "0.5rem", alignItems: "flex-end" }}>
                  <div style={{ flex: 1 }}>
                    <label style={labelStyle}>Email</label>
                    <input
                      className="input"
                      type="email"
                      value={addMemberEmail}
                      onChange={(e) => setAddMemberEmail(e.target.value)}
                      placeholder="user@company.com"
                      style={{ width: "100%" }}
                    />
                  </div>
                  <div>
                    <label style={labelStyle}>Role</label>
                    <select
                      className="select"
                      value={addMemberRole}
                      onChange={(e) => setAddMemberRole(e.target.value)}
                    >
                      <option value="approver">Approver</option>
                      <option value="admin">Admin</option>
                    </select>
                  </div>
                  <button
                    className="btn btn-primary"
                    style={{ fontSize: "0.8125rem" }}
                    onClick={handleAddMember}
                    disabled={!addMemberEmail.trim()}
                  >
                    Add
                  </button>
                </div>
              </>
            ) : null}
          </DetailPanel>

          <ConfirmDialog
            open={grpDeleteConfirm !== null}
            title="Delete Group"
            message={`Delete group "${grpDeleteConfirm?.name}"? All members will be removed.`}
            confirmLabel="Delete"
            variant="danger"
            onConfirm={handleDeleteGroup}
            onCancel={() => setGrpDeleteConfirm(null)}
            loading={grpDeleting}
          />
        </>
      )}

      {/* ═══ Budgets Tab ═══════════════════════════ */}
      {activeTab === "budgets" && (
        <>
          <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: "1rem" }}>
            <button
              className="btn btn-primary"
              onClick={() => {
                setBudForm({ ...emptyBudgetForm });
                setBudEditing(null);
                setBudModalOpen(true);
              }}
            >
              Create Budget
            </button>
          </div>

          {!budLoading && budgets.length === 0 ? (
            <EmptyState
              title="No autonomy budgets"
              description="Define budgets to control how many autonomous actions devices can execute."
              actionLabel="Create Budget"
              onAction={() => {
                setBudForm({ ...emptyBudgetForm });
                setBudEditing(null);
                setBudModalOpen(true);
              }}
              icon="&#x2B22;"
            />
          ) : (
            <DataTable<AutonomyBudget>
              columns={budgetColumns}
              data={budgets}
              loading={budLoading}
              emptyMessage="No budgets"
              page={budPage}
              pageSize={PAGE_SIZE}
              total={budTotal}
              onPageChange={setBudPage}
              rowActions={[
                {
                  label: "Edit",
                  onClick: (r) => {
                    setBudForm({
                      device_type: r.device_type,
                      level: r.level,
                      budget_limit: r.budget_limit,
                      period: r.period,
                      learning_ramp: r.learning_ramp,
                      ramp_increment: r.ramp_increment ?? 1,
                    });
                    setBudEditing(r.id);
                    setBudModalOpen(true);
                  },
                },
                { label: "Delete", variant: "danger", onClick: (r) => setBudDeleteConfirm(r) },
              ]}
              striped
            />
          )}

          {/* Budget modal */}
          {budModalOpen && (
            <div style={modalOverlay} onClick={() => { setBudModalOpen(false); setBudEditing(null); }} role="presentation">
              <div
                role="dialog"
                aria-modal="true"
                aria-labelledby="budget-modal-title"
                style={modalCard}
                onClick={(e) => e.stopPropagation()}
              >
                <h3 id="budget-modal-title" style={{ margin: "0 0 1rem 0", fontSize: "1.125rem", fontWeight: 600 }}>
                  {budEditing ? "Edit Budget" : "Create Budget"}
                </h3>

                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "1rem" }}>
                  <div style={formGroup}>
                    <label style={labelStyle}>Device Type</label>
                    <select
                      className="select"
                      value={budForm.device_type}
                      onChange={(e) => setBudForm((prev) => ({ ...prev, device_type: e.target.value }))}
                      style={{ width: "100%" }}
                    >
                      {DEVICE_TYPE_OPTIONS.map((o) => (
                        <option key={o.value} value={o.value}>{o.label}</option>
                      ))}
                    </select>
                  </div>
                  <div style={formGroup}>
                    <label style={labelStyle}>Level</label>
                    <select
                      className="select"
                      value={budForm.level}
                      onChange={(e) => setBudForm((prev) => ({ ...prev, level: parseInt(e.target.value, 10) }))}
                      style={{ width: "100%" }}
                    >
                      <option value={0}>0 - Observe</option>
                      <option value={1}>1 - Suggest</option>
                      <option value={2}>2 - Batch</option>
                      <option value={3}>3 - Autonomous</option>
                    </select>
                  </div>
                </div>

                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "1rem" }}>
                  <div style={formGroup}>
                    <label style={labelStyle}>Budget Limit</label>
                    <input
                      className="input"
                      type="number"
                      min={1}
                      value={budForm.budget_limit}
                      onChange={(e) => setBudForm((prev) => ({ ...prev, budget_limit: parseInt(e.target.value, 10) || 1 }))}
                      style={{ width: "100%" }}
                    />
                  </div>
                  <div style={formGroup}>
                    <label style={labelStyle}>Period</label>
                    <select
                      className="select"
                      value={budForm.period}
                      onChange={(e) => setBudForm((prev) => ({ ...prev, period: e.target.value }))}
                      style={{ width: "100%" }}
                    >
                      {PERIOD_OPTIONS.map((o) => (
                        <option key={o.value} value={o.value}>{o.label}</option>
                      ))}
                    </select>
                  </div>
                </div>

                {/* Learning ramp */}
                <div style={formGroup}>
                  <label style={{ ...labelStyle, display: "flex", alignItems: "center", gap: "0.5rem", cursor: "pointer" }}>
                    <input
                      type="checkbox"
                      checked={budForm.learning_ramp}
                      onChange={(e) => setBudForm((prev) => ({ ...prev, learning_ramp: e.target.checked }))}
                      style={{ accentColor: "var(--accent)" }}
                    />
                    Enable Learning Ramp
                  </label>
                  {budForm.learning_ramp && (
                    <div style={{ marginTop: "0.5rem" }}>
                      <label style={labelStyle}>Ramp Increment (actions per period)</label>
                      <input
                        className="input"
                        type="number"
                        min={1}
                        value={budForm.ramp_increment}
                        onChange={(e) => setBudForm((prev) => ({ ...prev, ramp_increment: parseInt(e.target.value, 10) || 1 }))}
                        style={{ width: "100%" }}
                      />
                      <div style={{ fontSize: "0.75rem", color: "var(--text-muted)", marginTop: "0.25rem" }}>
                        Budget increases by this amount each period if no failures occur
                      </div>
                    </div>
                  )}
                </div>

                <div style={{ display: "flex", justifyContent: "flex-end", gap: "0.5rem", marginTop: "1.25rem" }}>
                  <button
                    className="btn"
                    onClick={() => { setBudModalOpen(false); setBudEditing(null); }}
                    disabled={budSaving}
                  >
                    Cancel
                  </button>
                  <button className="btn btn-primary" onClick={handleSaveBudget} disabled={budSaving}>
                    {budSaving && <Spinner size="sm" />}
                    {budEditing ? "Update" : "Create"}
                  </button>
                </div>
              </div>
            </div>
          )}

          <ConfirmDialog
            open={budDeleteConfirm !== null}
            title="Delete Budget"
            message={`Delete this autonomy budget for "${budDeleteConfirm?.device_type}" devices? This cannot be undone.`}
            confirmLabel="Delete"
            variant="danger"
            onConfirm={handleDeleteBudget}
            onCancel={() => setBudDeleteConfirm(null)}
            loading={budDeleting}
          />
        </>
      )}
    </div>
  );
}
