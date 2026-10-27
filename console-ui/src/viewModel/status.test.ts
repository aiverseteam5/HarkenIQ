/** A35.5: the semantic status module's rules, pinned.
 *
 *  These are presentation rules, and they matter because colour is read as
 *  meaning: green says "fine, proceed". So the tests below hold the module
 *  to never endorse what governance decides, to keep approved apart from
 *  completed (A18), and to say "unknown" when it does not know (P3).
 */

import { describe, expect, it } from "vitest";
import statusSource from "./status.ts?raw";
import {
  CATEGORY_DOMAINS,
  SEVERITY_TONES,
  STATE_TONES,
  STATUS_DOMAINS,
  TONES,
  statusTone,
  toneFamily,
  type EnumeratedDomain,
} from "./status";

describe("the twelve tones", () => {
  it("are exactly the twelve the brief names, in two families plus unknown", () => {
    expect([...TONES].sort()).toEqual(
      [
        "approved", "blocked", "critical", "denied", "healthy", "high",
        "informational", "neutral", "pending", "stale", "unknown", "warning",
      ],
    );
    expect(SEVERITY_TONES.map(toneFamily)).toEqual(SEVERITY_TONES.map(() => "severity"));
    expect(STATE_TONES.map(toneFamily)).toEqual(STATE_TONES.map(() => "state"));
    expect(toneFamily("unknown")).toBe("unknown");
  });
});

describe("unknown is first-class (P3)", () => {
  it.each([null, undefined, "", "a-value-the-server-adds-tomorrow"])("%j reads unknown", (value) => {
    expect(statusTone("proposal.status", value)).toBe("unknown");
  });

  it("a category reads neutral for any value, and unknown for none", () => {
    for (const domain of CATEGORY_DOMAINS) {
      expect(statusTone(domain, "anything")).toBe("neutral");
      expect(statusTone(domain, "")).toBe("unknown");
    }
  });

  it("is never borrowed from a neighbouring value: no prototype keys", () => {
    expect(statusTone("proposal.status", "toString")).toBe("unknown");
    expect(statusTone("proposal.status", "constructor")).toBe("unknown");
  });
});

describe("rule 1: green never endorses governance", () => {
  const GOVERNANCE: EnumeratedDomain[] = [
    "approval.decision",
    "approval.queue",
    "autonomy.disposition",
    "autonomy.level",
    "autonomy.safety",
    "safety.stop_switch",
    "action.risk",
    "action.reversibility",
    "policy.status",
    "operational_agent.status",
    "proposal.attendance",
    "attention.summary",
    "attention.band",
    "risk.band",
  ];

  it.each(GOVERNANCE)("%s never reads healthy", (domain) => {
    expect(Object.values(STATUS_DOMAINS[domain])).not.toContain("healthy");
  });

  it("names the checkpoint's cases one by one", () => {
    expect(statusTone("autonomy.disposition", "autonomous")).toBe("informational");
    expect(statusTone("proposal.attendance", "unattended")).toBe("informational");
    expect(statusTone("safety.stop_switch", "inactive")).toBe("neutral");
    expect(statusTone("action.risk", "low")).toBe("informational");
    expect(statusTone("autonomy.safety", "clear")).toBe("neutral");
    expect(statusTone("attention.summary", "clear")).toBe("neutral");
    expect(statusTone("autonomy.level", 3)).toBe("informational");
  });
});

describe("rule 2: approved is not completed (A18)", () => {
  it("on every domain that has both", () => {
    expect(statusTone("campaign.wave", "approved")).toBe("approved");
    expect(statusTone("campaign.wave", "completed")).toBe("healthy");
    expect(statusTone("proposal.status", "approved")).toBe("approved");
    expect(statusTone("proposal.status", "completed")).toBe("healthy");
    expect(statusTone("approval.decision", "approved")).toBe("approved");
  });
});

describe("one word, one tone, across governance domains", () => {
  const each = (word: string) =>
    (Object.keys(STATUS_DOMAINS) as EnumeratedDomain[])
      .filter((d) => word in STATUS_DOMAINS[d])
      .map((d) => [d, STATUS_DOMAINS[d][word]]);

  it.each([
    ["approved", "approved"],
    ["denied", "denied"],
    ["blocked", "blocked"],
    ["awaiting_approval", "pending"],
    ["pending_approval", "pending"],
    ["requires_approval", "pending"],
  ])("%s always reads %s", (word, tone) => {
    const found = each(word);
    expect(found.length).toBeGreaterThan(0);
    expect(found.filter(([, t]) => t !== tone)).toEqual([]);
  });
});

describe("rule 4: one ladder for risk-shaped vocabularies", () => {
  it.each(["action.risk", "attention.band", "risk.band", "cve.severity"] as EnumeratedDomain[])(
    "%s ranks critical > high > medium > low",
    (domain) => {
      const v = STATUS_DOMAINS[domain];
      if ("critical" in v) expect(v.critical).toBe("critical");
      expect(v.high).toBe("high");
      expect(v.medium).toBe("warning");
      expect(v.low).toBe("informational");
    },
  );

  it("support severities follow the same ladder", () => {
    expect(["S1", "S2", "S3", "S4"].map((s) => statusTone("support.severity", s))).toEqual([
      "critical", "high", "warning", "informational",
    ]);
  });
});

describe("rule 5: critical only for an active fault", () => {
  const FAULTS = new Set([
    "critical", "faulted", "unreachable", "failed", "failure", "halted", "S1",
    "cross_site_batch", "batch_failure", "unhealthy", "current_failure", "invalid", "active",
  ]);

  it("every value that reads critical names a fault", () => {
    const critical = (Object.keys(STATUS_DOMAINS) as EnumeratedDomain[]).flatMap((d) =>
      Object.entries(STATUS_DOMAINS[d])
        .filter(([, t]) => t === "critical")
        .map(([v]) => `${d}:${v}`),
    );
    expect(critical.length).toBeGreaterThan(8);
    // "active" is allowed only for the stop switch: an active halt is the
    // one governance state that stops everything.
    expect(critical.filter((k) => !FAULTS.has(k.split(":")[1]))).toEqual([]);
    expect(critical.filter((k) => k.endsWith(":active"))).toEqual(["safety.stop_switch:active"]);
  });
});

describe("domains mirror the server's closed vocabularies", () => {
  // Central Command: campaigns.py STATUS_* and WAVE_*, operational_agent.py
  // PROPOSAL_*, agent_activation.py verdicts, capabilities.py reach states.
  it.each([
    ["campaign.status", ["draft", "preflighted", "acknowledged", "awaiting_approval", "running", "completed", "halted", "cancelled"]],
    ["campaign.wave", ["autonomous", "pending_approval", "approved", "denied", "voided", "dispatched", "completed", "failed"]],
    ["proposal.status", ["proposed", "awaiting_approval", "approved", "blocked", "denied", "dispatched", "completed", "failed"]],
    ["preflight.verdict", ["ready", "warn", "blocked", "unknown"]],
    ["capability.reach", ["available", "unimplemented", "no_effective_reach", "not_permitted_on_any_node", "no_devices_in_scope", "unknown"]],
    ["autonomy.disposition", ["autonomous", "requires_approval", "denied", "not_budget_mapped"]],
    ["attention.driver", ["current_failure", "awaiting_approval", "degraded_now", "predicted_risk", "insufficient_evidence"]],
    ["device.warranty", ["active", "expiring", "expired", "unknown"]],
  ] as [EnumeratedDomain, string[]][])("%s", (domain, vocabulary) => {
    expect(Object.keys(STATUS_DOMAINS[domain]).sort()).toEqual([...vocabulary].sort());
  });
});

describe("purity (A34.5, A35.5)", () => {
  it("imports nothing, so the Site Manager can take it unchanged in PX7", () => {
    expect(statusSource).not.toMatch(/^\s*import\s/m);
  });
});
