/** A35.5: the one semantic status module (A28.12).
 *
 *  Every StatusBadge in the Console gets its tone here, from a (domain,
 *  value) pair, and nowhere else. A domain mirrors one closed vocabulary the
 *  server sends; a value the module does not know reads `unknown` (P3), so
 *  a new server value is shown honestly instead of guessed.
 *
 *  Presentation only. A tone colours words the server already said; it
 *  answers no authorization, scope, approval, autonomy or capability
 *  question (A28.23), and the view-model purity test holds this module to
 *  that. The rules it encodes:
 *
 *   1. `healthy` only for an observed good condition or a success the
 *      server reported: never an approval, a disposition, a risk class,
 *      unattended operation, or the absence of a stop. Green is read as
 *      "fine, proceed", and the screen must not endorse what governance
 *      decides. An account or entitlement in good standing is a good
 *      condition; an active governance or automation object is
 *      `informational`.
 *   2. Approved is not completed (A18).
 *   3. requires_approval → pending; autonomous → informational; denied →
 *      denied, which renders subdued: D16 makes a denial final, and a
 *      decision is not a fault.
 *   4. Ladders: critical → critical, high → high, medium → warning, low →
 *      informational, none → neutral, insufficient data → unknown.
 *   5. `critical` only for an active fault: failed, faulted, halted, S1.
 *   6. Categories (roles, plans, tiers, scopes, lanes, action types, line
 *      types) are not statuses, so any value renders neutral.
 *
 *  It imports nothing, so the Site Manager can take it unchanged (PX7).
 */

export const SEVERITY_TONES = [
  "critical",
  "high",
  "warning",
  "informational",
  "healthy",
  "neutral",
] as const;

export const STATE_TONES = ["pending", "approved", "denied", "blocked", "stale"] as const;

export const TONES = [...SEVERITY_TONES, ...STATE_TONES, "unknown"] as const;

export type Tone = (typeof TONES)[number];
export type ToneFamily = "severity" | "state" | "unknown";

export function toneFamily(tone: Tone): ToneFamily {
  if ((SEVERITY_TONES as readonly string[]).includes(tone)) return "severity";
  if ((STATE_TONES as readonly string[]).includes(tone)) return "state";
  return "unknown";
}

/** Rule 4, shared by every risk-shaped vocabulary. */
const LADDER = {
  critical: "critical",
  high: "high",
  medium: "warning",
  low: "informational",
  none: "neutral",
  insufficient_data: "unknown",
} as const satisfies Record<string, Tone>;

/** Each domain is one closed vocabulary from the server (or, for a few
 *  console-side facts, the page's own named states). */
const DOMAINS = {
  // ── Fleet and Harken Nodes ─────────────────────────────────────────────
  "device.health": { ok: "healthy", warning: "warning", critical: "critical", unknown: "unknown" },
  "device.observation": {
    normal: "healthy",
    degraded: "warning",
    degraded_link: "warning",
    faulted: "critical",
    unreachable: "critical",
    unreported: "unknown",
    unknown: "unknown",
  },
  "node.observation": { observed: "healthy", unobserved: "unknown" },
  "device.warranty": { active: "healthy", expiring: "warning", expired: "warning", unknown: "unknown" },
  "cve.severity": { critical: "critical", high: "high", medium: "warning", low: "informational" },
  "risk.band": { ...LADDER, unavailable: "unknown" },
  "fleet.pattern": {
    cross_site_batch: "critical",
    batch_failure: "critical",
    anomaly: "warning",
    reliability: "warning",
  },
  "platform.health": { healthy: "healthy", degraded: "warning", unhealthy: "critical", unknown: "unknown" },

  // ── Attention and incidents ────────────────────────────────────────────
  "attention.band": LADDER,
  "attention.driver": {
    current_failure: "critical",
    awaiting_approval: "pending",
    degraded_now: "warning",
    predicted_risk: "informational",
    insufficient_evidence: "unknown",
  },
  /** Silence is never health (P7): nothing to attend to is neutral. */
  "attention.summary": { needs_attention: "warning", high_risk: "high", clear: "neutral" },
  "incident.status": { open: "warning", resolved: "healthy" },
  "incident.diagnosis": { explained: "informational" },

  // ── Governance ─────────────────────────────────────────────────────────
  "approval.decision": { pending: "pending", approved: "approved", denied: "denied", expired: "neutral" },
  "approval.queue": { waiting: "pending", clear: "neutral" },
  "autonomy.disposition": {
    autonomous: "informational",
    requires_approval: "pending",
    denied: "denied",
    not_budget_mapped: "neutral",
  },
  /** Observe and suggest keep a human in every loop; batch and autonomous
   *  can act without one. Neither is good or bad, so neither is green. */
  "autonomy.level": { "0": "neutral", "1": "neutral", "2": "informational", "3": "informational" },
  /** Rule 1: no drop-back is not "safe", it is the absence of a finding. */
  "autonomy.safety": { not_reported: "unknown", dropped_back: "warning", clear: "neutral" },
  "safety.stop_switch": { active: "critical", inactive: "neutral" },
  "action.risk": LADDER,
  "action.reversibility": {
    none: "neutral",
    self_reverting: "neutral",
    reversible: "neutral",
    irreversible: "warning",
  },
  "capability.reach": {
    available: "healthy",
    unimplemented: "blocked",
    no_effective_reach: "warning",
    not_permitted_on_any_node: "warning",
    no_devices_in_scope: "neutral",
    unknown: "unknown",
  },
  "policy.status": { active: "informational", inactive: "neutral" },

  // ── Operational Agents and proposals ──────────────────────────────────
  "operational_agent.status": { draft: "neutral", active: "informational", paused: "warning", retired: "neutral" },
  "operational_agent.runtime": {
    drifted: "warning",
    held: "warning",
    budget_spent: "warning",
    version_unknown: "unknown",
  },
  "preflight.verdict": { ready: "healthy", warn: "warning", blocked: "blocked", unknown: "unknown" },
  "proposal.status": {
    proposed: "pending",
    awaiting_approval: "pending",
    approved: "approved",
    blocked: "blocked",
    denied: "denied",
    dispatched: "informational",
    completed: "healthy",
    failed: "critical",
  },
  "proposal.attendance": { requires_human: "pending", unattended: "informational" },
  "skill.usability": { usable: "healthy", unusable: "blocked", unknown: "unknown" },

  // ── Campaigns ──────────────────────────────────────────────────────────
  "campaign.status": {
    draft: "neutral",
    preflighted: "informational",
    acknowledged: "informational",
    awaiting_approval: "pending",
    running: "informational",
    completed: "healthy",
    halted: "critical",
    cancelled: "neutral",
  },
  "campaign.outcome": { partial_success: "warning" },
  "campaign.applicability": {
    eligible: "healthy",
    warn_not_permitted: "warning",
    unknown: "unknown",
    excluded_unimplemented: "blocked",
    excluded_by_operator: "neutral",
  },
  "campaign.wave": {
    autonomous: "informational",
    pending_approval: "pending",
    approved: "approved",
    denied: "denied",
    voided: "neutral",
    dispatched: "informational",
    completed: "healthy",
    failed: "critical",
  },

  // ── Learning and outcomes ──────────────────────────────────────────────
  "learning.cycle": {
    open: "neutral",
    measuring: "informational",
    promotion_recommended: "pending",
    closed: "neutral",
  },
  /** A promoted candidate waits on marketplace review by a person. */
  "learning.candidate": { received: "neutral", promoted: "pending" },
  "learning.validation": { community: "neutral", tested: "informational", verified: "informational" },
  "outcome.result": {
    success: "healthy",
    failed: "critical",
    failure: "critical",
    partial: "warning",
    rollback: "warning",
  },
  "trend.direction": { improving: "healthy", stable: "neutral", degrading: "warning" },

  // ── Tenancy, people and billing ────────────────────────────────────────
  "tenant.status": { active: "healthy", pending: "pending", delinquent: "warning", suspended: "blocked" },
  "user.status": { active: "healthy", invited: "pending", disabled: "neutral" },
  "license.status": { active: "healthy", expired: "warning", revoked: "denied" },
  "license.validation": { valid: "healthy", invalid: "critical" },
  "api_key.status": { active: "healthy", revoked: "neutral" },
  "billing.delinquency": { current: "healthy", overdue: "warning", restricted: "blocked", suspended: "blocked" },
  "invoice.status": { draft: "neutral", issued: "pending", paid: "healthy", void: "neutral" },
  "payment.status": { pending: "pending", completed: "healthy", failed: "critical" },
  "subscription.status": {
    active: "healthy",
    past_due: "warning",
    expired: "warning",
    cancelled: "neutral",
    canceled: "neutral",
  },
  "support.severity": { S1: "critical", S2: "high", S3: "warning", S4: "informational" },
  "support.status": {
    open: "pending",
    acknowledged: "informational",
    in_progress: "informational",
    waiting_on_tenant: "pending",
    closed: "neutral",
  },
  "release.currency": { current: "healthy", update_available: "warning" },
  "integration.state": { not_configured: "neutral" },
} as const satisfies Record<string, Record<string, Tone>>;

/** Rule 6: a category names a kind of thing, not its condition. */
export const CATEGORY_DOMAINS = [
  "action.type",
  "approval.lane",
  "user.role",
  "license.plan",
  "api_key.scope",
  "invoice.line_type",
  "skill.tier",
  "feature.category",
  "support.component",
  "version.label",
] as const;

export type EnumeratedDomain = keyof typeof DOMAINS;
export type CategoryDomain = (typeof CATEGORY_DOMAINS)[number];
export type StatusDomain = EnumeratedDomain | CategoryDomain;

/** The vocabulary each enumerated domain recognises, for tests and tools. */
export const STATUS_DOMAINS: Readonly<Record<EnumeratedDomain, Readonly<Record<string, Tone>>>> = DOMAINS;

function isCategory(domain: StatusDomain): domain is CategoryDomain {
  return (CATEGORY_DOMAINS as readonly string[]).includes(domain);
}

/** The tone for one value the server sent. Unrecognised → `unknown`. */
export function statusTone(domain: StatusDomain, value: string | number | null | undefined): Tone {
  if (value === null || value === undefined) return "unknown";
  const key = String(value);
  if (key === "") return "unknown";
  if (isCategory(domain)) return "neutral";
  const vocabulary: Readonly<Record<string, Tone>> = DOMAINS[domain];
  return Object.prototype.hasOwnProperty.call(vocabulary, key) ? vocabulary[key] : "unknown";
}
