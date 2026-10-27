/** A34.6: the operational context, as a human reads it.
 *
 *  A28.11 requires the shell to state five things at all times: tenant,
 *  effective scope, enforcement posture, safety state and freshness. Every
 *  one already has a server contract. This module turns those contracts
 *  into words, and the risk is entirely in the words:
 *
 *  - SAFETY is never "safe" or "healthy". The strongest clear statement
 *    the contract supports is that no stop is in effect, and a site that
 *    has not reported is unknown (S5: unreported is UNKNOWN, never safe).
 *  - SCOPE describes the caller's grants. It does not predict what any
 *    page will show: the site set `/api/scope-grants/me` publishes is
 *    permission-neutral (S2's recorded `/me` follow-up), and each page asks
 *    Central Command for itself. Nothing here filters anything.
 *  - FRESHNESS states when this Console last heard, never "connected" or
 *    "online" (A28.16).
 *  - A platform identity is not a tenant identity: Central Command refuses
 *    it (E1.4), so its scope, posture and safety are not applicable rather
 *    than failing or unknown.
 *
 *  Pure: what the server sent in, words out. It answers no authorization,
 *  scope, approval, autonomy or capability question.
 */

import { closedVocabulary } from "./vocabulary";
import { statusOf, type Snapshot } from "./dataState";

/** The fields of `GET /api/scope-grants/me` this module reads. */
export interface MyScopeContract {
  enforcement?: string;
  tenant_wide?: boolean;
  site_ids?: string[];
  org_unit_paths?: string[];
  device_ids?: string[];
  device_classes?: string[];
  inert_grants?: unknown[];
  synthesis?: string;
}

/** The fields of `GET /api/autonomy/` this module reads. The contract is
 *  already composed over the caller's own `fleet.view` reach (A30.26). */
export interface AutonomySafetyContract {
  generated_at?: string;
  posture?: {
    stop_switch?: { active?: boolean; changed_at?: string | null };
  };
  safety_state?: {
    reported?: boolean;
    every_site_reported?: boolean;
    sites_not_reporting?: string[];
    site_stop_switches?: { site_id?: string; active?: boolean }[];
    suppressions?: unknown[];
  };
  scope?: { sites?: { id?: string; name?: string }[] };
}

export const ENFORCEMENT = closedVocabulary(["strict", "legacy_open"] as const);
export const SYNTHESIS = closedVocabulary([
  "granted",
  "agent",
  "strict",
  "previously_granted",
  "never_granted",
] as const);

/** States every fact can be in besides its own. */
export type Availability = "loading" | "denied" | "unavailable" | "not_applicable" | "unknown";

export interface Fact<S extends string> {
  state: S | Availability;
  /** Short text for the context bar. Never empty. */
  text: string;
  /** One or two sentences for a tooltip and for assistive technology. */
  detail: string;
}

export type TenantFact = Fact<"named" | "unnamed">;
export type ScopeFact = Fact<"tenant_wide" | "narrowed" | "no_reach">;
export type PostureFact = Fact<"strict" | "legacy_open">;
export type SafetyFact = Fact<
  "tenant_stop" | "site_halt" | "no_sites" | "not_reported" | "partially_reported" | "no_stop"
>;
export type FreshnessFact = Fact<"current" | "stale" | "never">;

export interface OperationalContextView {
  tenant: TenantFact;
  scope: ScopeFact;
  posture: PostureFact;
  safety: SafetyFact;
  freshness: FreshnessFact;
}

export interface ContextInputs {
  isPlatformUser: boolean;
  tenantId: string;
  tenantName: string | null;
  scope: Snapshot<MyScopeContract>;
  safety: Snapshot<AutonomySafetyContract>;
  now: number;
  /** A successful read older than this is stale (the poll stopped). */
  staleAfterMs: number;
  /** Renders an epoch-ms time. Injected so tests are not time-zone bound. */
  formatTime?: (ms: number) => string;
}

const PLATFORM_DETAIL =
  "Central Command scope, posture and safety apply to tenant identities. " +
  "A platform identity is not one, so Central Command is not asked.";

function plural(n: number, one: string, many = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

function defaultTime(ms: number): string {
  return new Date(ms).toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

/** The fact a read is in when it has no data to describe. `null` when it
 *  has data -- stale data is still described; freshness reports the age. */
function availability(
  snapshot: Snapshot<unknown>,
  now: number,
  subject: string,
): Fact<never> | null {
  const status = statusOf(snapshot, { now });
  switch (status) {
    case "ready":
    case "stale":
    case "partial":
      return null;
    case "loading":
      return { state: "loading", text: "Loading", detail: `Asking Central Command for ${subject}.` };
    case "denied":
      return {
        state: "denied",
        text: "Not available to you",
        detail:
          `Central Command refused to show ${subject} to this identity` +
          (snapshot.failure?.message ? `: ${snapshot.failure.message}` : "."),
      };
    default:
      return {
        state: "unavailable",
        text: "Unavailable",
        detail:
          `${subject[0].toUpperCase()}${subject.slice(1)} could not be loaded` +
          (snapshot.failure?.message ? `: ${snapshot.failure.message}` : "."),
      };
  }
}

export function tenantFact(inputs: Pick<ContextInputs, "isPlatformUser" | "tenantId" | "tenantName">): TenantFact {
  if (inputs.tenantName) {
    return { state: "named", text: inputs.tenantName, detail: `Tenant: ${inputs.tenantName}.` };
  }
  // Never a raw 32-hex id dressed as a name for a tenant user; platform
  // staff work with ids and are shown the id they navigated to.
  return inputs.isPlatformUser
    ? { state: "unnamed", text: inputs.tenantId, detail: `Tenant id ${inputs.tenantId}.` }
    : { state: "unnamed", text: "Your tenant", detail: "The tenant's name could not be read." };
}

export function scopeFact(inputs: ContextInputs): ScopeFact {
  if (inputs.isPlatformUser) {
    return { state: "not_applicable", text: "Not applicable", detail: PLATFORM_DETAIL };
  }
  const missing = availability(inputs.scope, inputs.now, "your effective scope");
  if (missing) return missing;
  const me = inputs.scope.data ?? {};
  const inert = (me.inert_grants ?? []).length;
  const inertNote = inert
    ? ` ${plural(inert, "grant")} ${inert === 1 ? "names" : "name"} a target that no longer exists and ${inert === 1 ? "reaches" : "reach"} nothing.`
    : "";
  if (me.tenant_wide === true) {
    const synthesized = SYNTHESIS.read(me.synthesis) === "never_granted";
    return {
      state: "tenant_wide",
      text: synthesized ? "Tenant-wide (legacy posture)" : "Tenant-wide",
      detail:
        (synthesized
          ? "You hold no grant. The tenant's legacy posture gives a principal that was never granted any scope tenant-wide reach (A23.10)."
          : "Your grants reach the whole tenant.") + inertNote,
    };
  }
  const parts = [
    [(me.site_ids ?? []).length, "site"],
    [(me.org_unit_paths ?? []).length, "org unit"],
    [(me.device_ids ?? []).length, "device"],
    [(me.device_classes ?? []).length, "device class", "device classes"],
  ]
    .filter(([n]) => (n as number) > 0)
    .map(([n, one, many]) => plural(n as number, one as string, many as string | undefined));
  if (parts.length === 0) {
    return {
      state: "no_reach",
      text: "No reach",
      detail:
        "Your grants reach nothing in this tenant. Lists here are empty because of your scope, not because nothing is there." +
        inertNote,
    };
  }
  return {
    state: "narrowed",
    text: parts.join(" · "),
    detail:
      "What your grants name. Each page asks Central Command what you may see there, so this describes your grants and does not predict any page." +
      inertNote,
  };
}

export function postureFact(inputs: ContextInputs): PostureFact {
  if (inputs.isPlatformUser) {
    return { state: "not_applicable", text: "Not applicable", detail: PLATFORM_DETAIL };
  }
  const missing = availability(inputs.scope, inputs.now, "the enforcement posture");
  if (missing) return missing;
  const raw = inputs.scope.data?.enforcement;
  switch (ENFORCEMENT.read(raw)) {
    case "strict":
      return { state: "strict", text: "Strict", detail: "Reach comes only from explicit grants." };
    case "legacy_open":
      return {
        state: "legacy_open",
        text: "Legacy open",
        detail: "A person who was never granted any scope is given tenant-wide reach (A23.10).",
      };
    default:
      return {
        state: "unknown",
        text: "Unknown",
        detail: `Central Command reported a posture this Console does not recognise${raw ? ` ("${String(raw)}")` : ""}.`,
      };
  }
}

export function safetyFact(inputs: ContextInputs): SafetyFact {
  if (inputs.isPlatformUser) {
    return { state: "not_applicable", text: "Not applicable", detail: PLATFORM_DETAIL };
  }
  const missing = availability(inputs.safety, inputs.now, "the safety state");
  if (missing) return missing;
  const contract = inputs.safety.data ?? {};
  const state = contract.safety_state ?? {};
  const sites = contract.scope?.sites ?? [];
  const names = new Map(sites.map((s) => [s.id ?? "", s.name || s.id || ""]));
  const named = (ids: readonly string[]) =>
    ids.map((id) => names.get(id) || id).join(", ");
  const suppressions = (state.suppressions ?? []).length;
  const suppressionNote = suppressions
    ? ` ${plural(suppressions, "fault-domain suppression")} active.`
    : "";

  // A stop outranks everything, and a stale stop still stops (S3-E1 D8):
  // this is checked before anything about reporting.
  if (contract.posture?.stop_switch?.active === true) {
    return {
      state: "tenant_stop",
      text: "Stop switch ON",
      detail: "The tenant stop switch is active: every action class is denied until it is cleared.",
    };
  }
  const halted = (state.site_stop_switches ?? [])
    .filter((s) => s.active === true && s.site_id)
    .map((s) => s.site_id as string);
  if (halted.length > 0) {
    return {
      state: "site_halt",
      text: `${plural(halted.length, "site")} halted`,
      detail: `A Site Manager reports a halt at: ${named(halted)}.${suppressionNote}`,
    };
  }
  if (sites.length === 0) {
    return {
      state: "no_sites",
      text: "No sites in scope",
      detail: "No site in your reach reports safety state to you.",
    };
  }
  if (state.reported !== true) {
    return {
      state: "not_reported",
      text: "Not reported",
      detail:
        "No Site Manager in your reach has reported its safety state in the last 15 minutes. Unknown is not safe.",
    };
  }
  const silent = state.sites_not_reporting ?? [];
  if (state.every_site_reported !== true || silent.length > 0) {
    return {
      state: "partially_reported",
      text: silent.length ? `${plural(silent.length, "site")} not reporting` : "Sites not reporting",
      detail: `Safety state is unknown at: ${named(silent) || "one or more sites"}. No reporting site reports a halt.${suppressionNote}`,
    };
  }
  return {
    state: "no_stop",
    text: "No stop in effect",
    detail:
      "Every site in your reach reported within 15 minutes, none reports a halt, and the tenant stop switch is clear." +
      suppressionNote,
  };
}

export function freshnessFact(inputs: ContextInputs): FreshnessFact {
  if (inputs.isPlatformUser) {
    return {
      state: "not_applicable",
      text: "Not applicable",
      detail: "Nothing in this bar is refreshed from Central Command for a platform identity.",
    };
  }
  const fmt = inputs.formatTime ?? defaultTime;
  const reads = [inputs.scope, inputs.safety];
  // Still on its first attempt: nothing to date yet.
  if (reads.some((r) => r.lastSuccessAt === null && r.failure === null)) {
    return { state: "loading", text: "Loading", detail: "Asking Central Command for the first time." };
  }
  const reasons = reads
    .map((r) => r.failure)
    .filter((f) => f !== null)
    .map((f) => f!.message || `HTTP ${f!.status}`);
  const successes = reads
    .map((r) => r.lastSuccessAt)
    .filter((t): t is number => t !== null);
  if (successes.length === 0) {
    return {
      state: "never",
      text: "Not updated",
      detail: `No context read has succeeded yet: ${reasons.join("; ")}.`,
    };
  }
  // The oldest success is the honest age of what the bar shows, and a read
  // that has never succeeded makes the bar incomplete, however new the rest.
  const shownAt = Math.min(...successes);
  const incomplete = successes.length < reads.length;
  const tooOld = inputs.now - shownAt > inputs.staleAfterMs;
  if (reasons.length > 0 || incomplete || tooOld) {
    const why =
      reasons.length > 0
        ? `The latest refresh failed: ${reasons.join("; ")}.`
        : "No refresh has succeeded since then.";
    return {
      state: "stale",
      text: `Last updated ${fmt(shownAt)}`,
      detail: `This Console last heard from Central Command at ${fmt(shownAt)}. ${why}`,
    };
  }
  return {
    state: "current",
    text: `Updated ${fmt(shownAt)}`,
    detail: `This Console last heard from Central Command at ${fmt(shownAt)}.`,
  };
}

export function operationalContextView(inputs: ContextInputs): OperationalContextView {
  return {
    tenant: tenantFact(inputs),
    scope: scopeFact(inputs),
    posture: postureFact(inputs),
    safety: safetyFact(inputs),
    freshness: freshnessFact(inputs),
  };
}
