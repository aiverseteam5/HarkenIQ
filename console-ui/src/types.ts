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
  status: "active" | "past_due" | "cancelled" | "trialing";
  billing_cycle: "monthly" | "annual";
  current_period_start: string;
  current_period_end: string;
  node_commit: number;
  node_high_water: number;
  amount_cents: number;
  currency: string;
}

/** Invoice. */
export interface Invoice {
  id: string;
  tenant_id: string;
  number: string;
  status: "draft" | "open" | "paid" | "void" | "uncollectible";
  issued_at: string;
  due_at: string;
  paid_at: string | null;
  total_cents: number;
  currency: string;
  lines: InvoiceLine[];
  pdf_url: string | null;
}

/** Invoice line item. */
export interface InvoiceLine {
  description: string;
  quantity: number;
  unit_price_cents: number;
  amount_cents: number;
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
  service_tag: string;
  health: "ok" | "warning" | "critical" | "unknown";
  tier: "observe" | "approve" | "autonomy";
  last_seen_at: string;
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
export interface ApprovalAction {
  id: string;
  tenant_id: string;
  site_id: string;
  site_name: string;
  agent_id: string;
  action_type: string;
  skill_name: string;
  severity: string;
  params: Record<string, unknown> | null;
  status: "pending" | "approved" | "denied" | "expired";
  proposed_at: string;
  decided_by: string | null;
  decided_at: string | null;
}

/** Usage/billing summary for a tenant. */
export interface UsageSummary {
  tenant_id: string;
  period_start: string;
  period_end: string;
  node_commit: number;
  node_high_water: number;
  active_nodes: number;
  total_events: number;
  total_actions: number;
  total_approvals: number;
  estimated_bill_cents: number;
  currency: string;
}

/** Time-series metric data point. */
export interface MetricData {
  timestamp: string;
  value: number;
  label?: string;
}
