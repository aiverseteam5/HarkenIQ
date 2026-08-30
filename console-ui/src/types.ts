/** Paginated API response wrapper. */
export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}

/** Tenant (L4 entity). */
export interface Tenant {
  id: string;
  name: string;
  slug: string;
  status: "active" | "suspended" | "pending" | "delinquent";
  plan: string;
  region: string;
  contact_email: string;
  created_at: string;
  updated_at: string;
  node_count: number;
  site_count: number;
}

/** Console/platform user. */
export interface User {
  id: string;
  email: string;
  name: string;
  role: string;
  tenant_id: string;
  status: "active" | "disabled" | "invited";
  last_login_at: string | null;
  created_at: string;
  mfa_enabled: boolean;
}

/** Node license. */
export interface License {
  id: string;
  tenant_id: string;
  sku: string;
  tier: "observe" | "approve" | "autonomy";
  quantity: number;
  used: number;
  starts_at: string;
  expires_at: string;
  status: "active" | "expired" | "cancelled";
}

/** Billing subscription. */
export interface Subscription {
  id: string;
  tenant_id: string;
  plan: string;
  status: "active" | "past_due" | "cancelled";
  billing_frequency: "annual" | "quarterly";
  billing_cycle_start: string;
  node_commit: number;
  price_book_version: number;
  currency: string;
}

/** Invoice. */
export interface Invoice {
  id: string;
  tenant_id: string;
  invoice_number: string;
  type: "commit" | "overage";
  status: "draft" | "issued" | "paid" | "void";
  currency: string;
  subtotal_cents: number;
  tax_cents: number;
  total_cents: number;
  issued_at: string | null;
  due_at: string | null;
  paid_at: string | null;
  period_start: string;
  period_end: string;
  payment_provider: string | null;
  provider_payment_id: string | null;
  created_at: string;
}

/** Invoice line item. */
export interface InvoiceLine {
  id: string;
  invoice_id: string;
  description: string;
  quantity: number;
  unit_price_cents: number;
  amount_cents: number;
  line_type: "commit" | "overage" | "credit" | "site_fee";
}

/** Credit note. */
export interface CreditNote {
  id: string;
  invoice_id: string;
  tenant_id: string;
  amount_cents: number;
  currency: string;
  reason: string;
  issued_by: string | null;
  issued_at: string;
}

/** Payment record. */
export interface PaymentRecord {
  id: string;
  tenant_id: string;
  invoice_id: string | null;
  provider: "stripe" | "razorpay" | "manual";
  amount_cents: number;
  currency: string;
  status: "pending" | "completed" | "failed" | "refunded";
  created_at: string;
  completed_at: string | null;
}

/** Usage summary for a billing period. */
export interface UsagePeriodSummary {
  high_water: number;
  daily_counts: { date: string; node_count: number }[];
  per_site: { site_name: string; avg_nodes: number; peak_nodes: number; days: number }[];
}

/** True-up estimate. */
export interface TrueUpEstimate {
  high_water_so_far: number;
  committed: number;
  estimated_overage: number;
  estimated_amount_cents: number;
  currency: string;
}

/** Delinquency state. */
export interface DelinquencyState {
  status: "current" | "overdue" | "restricted" | "suspended";
  days_overdue: number;
  overdue_amount_cents: number;
}

/** Admin billing stats. */
export interface AdminBillingOverview {
  mrr_cents: number;
  arr_cents: number;
  active_tenants: number;
  total_nodes: number;
  currency: string;
  revenue_by_plan: { type: string; currency: string; total_cents: number; count: number }[];
  delinquent_tenants: {
    tenant_id: string;
    tenant_name: string;
    status: string;
    amount_owed_cents: number;
    days_overdue: number;
  }[];
}

/** Support ticket. */
export interface SupportTicket {
  id: string;
  tenant_id: string;
  subject: string;
  status: "open" | "in_progress" | "waiting" | "resolved" | "closed";
  priority: "low" | "medium" | "high" | "critical";
  created_by: string;
  assigned_to: string | null;
  created_at: string;
  updated_at: string;
  last_reply_at: string | null;
}

/** Audit log entry. */
export interface AuditEntry {
  id: string;
  timestamp: string;
  actor_id: string;
  actor_email: string;
  action: string;
  resource_type: string;
  resource_id: string;
  tenant_id: string | null;
  detail: Record<string, unknown>;
  ip_address: string;
}

/** Fleet device (aggregated view from Central Command). */
export interface FleetDevice {
  id: string;
  tenant_id: string;
  site_id: string;
  site_name: string;
  agent_id: string;
  vendor: string;
  model: string;
  /** R6: "server" | "switch"; absent from pre-R6 backends. */
  device_class?: string;
  service_tag: string;
  health: "ok" | "warning" | "critical" | "unknown";
  tier: "observe" | "approve" | "autonomy";
  last_seen_at: string;
  /** Fleet-cache refresh time — what /api/fleet actually sends (QA ISSUE-004). */
  snapshot_at?: string;
}

/** Fleet-level incident. */
export interface FleetIncident {
  id: string;
  tenant_id: string;
  site_id: string;
  site_name: string;
  kind: string;
  status: "open" | "acknowledged" | "resolved";
  severity: "info" | "warning" | "critical";
  title: string;
  device_count: number;
  opened_at: string;
  resolved_at: string | null;
}

/** Approval action surfaced to console. */
/** A labelled proposal from an Operational Agent (A1). */
export interface AgentProposal {
  proposal_id: string;
  agent_id: string;
  /** Attribution key: op-agent:<id>@v<n>. */
  actor: string;
  agent_version: number;
  site_id: string;
  device_agent_id: string;
  action_type: string;
  params: Record<string, unknown>;
  rationale: string;
  evidence: {
    observed?: string;
    condition_kind?: string;
    subsystem?: string;
    incident_ids?: string[];
    has_diagnosis?: boolean;
    remediation_provenance?: string;
    attention?: { rank?: number; band?: string; driver?: string; risk_score?: number } | null;
    outcome_evidence?: {
      executions: number;
      success: number;
      failure: number;
      success_rate: number | null;
      sufficient: boolean;
    } | null;
    learned_signals?: { statement: string; confidence: number | null }[];
    device?: { vendor?: string; model?: string; health?: string; observation?: string };
  };
  disposition: string;
  disposition_reason: string;
  blocking_conditions: { code: string; detail: string; scope: string }[];
  authorization_basis: string;
  status: string;
  decided_by: string;
  decided_at: string | null;
  directive_id: string;
  dispatch_reason: string;
  dispatched_at: string | null;
  outcome: string;
  outcome_at: string | null;
  created_at: string | null;
}

/** How many humans this subject needs, and how many it has (E0.1).
 *
 * A second approver has no way to know they are needed unless the queue
 * says so, which is why this rides every item rather than living behind
 * a detail view. */
export interface ApprovalProgress {
  state: "pending" | "approved" | "denied";
  required: number;
  received: number;
  remaining: number;
  approvers: { approver: string; decided_at: string | null }[];
  denied_by: string | null;
  denied_reason: string;
  policy_id: string | null;
  policy_name: string;
  mode: string;
  group_id: string | null;
  group_name: string;
}

/** One item in the single approval queue.
 *
 * A1: `origin` says who asked. The permission, the decision endpoint and
 * the downstream execution funnel are identical for both. Field names
 * mirror Central Command's payload exactly -- the previous shape here
 * described fields CC has never sent, so every card rendered blank. */
export interface ApprovalAction {
  origin: "node" | "agent";
  id: string;
  site_id: string;
  /** The id the decision endpoint takes. NOT `id` for node items. */
  action_id: string;
  action_type: string;
  device_agent_id: string;
  decision: string | null;
  decided_by: string | null;
  decided_at: string | null;
  routed_at: string | null;
  delivered_at: string | null;
  approval?: ApprovalProgress;
  proposal?: AgentProposal;
}

/** Chargeback usage summary for a tenant. */
export interface ChargebackSummary {
  tenant_id: string;
  period_start: string;
  period_end: string;
  node_commit: number;
  node_high_water: number;
  per_site: { site_name: string; avg_nodes: number; peak_nodes: number; days: number }[];
  base_amount_cents: number;
  overage_amount_cents: number;
  currency: string;
}

/** Time-series metric data point. */
export interface MetricData {
  timestamp: string;
  value: number;
  label?: string;
}
