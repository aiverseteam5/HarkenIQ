/** A34.6: the operational context, word by word.
 *
 *  The rules these tests hold are the ones a context bar is most tempted to
 *  break: never call anything safe, never let silence read as current,
 *  never present a description of grants as a prediction of reads, and
 *  never ask Central Command about a platform identity.
 */

import { describe, expect, it } from "vitest";
import { ApiError } from "../apiErrors";
import {
  classifyFailure,
  failed,
  initialSnapshot,
  succeeded,
  type Snapshot,
} from "./dataState";
import {
  operationalContextView,
  type AutonomySafetyContract,
  type ContextInputs,
  type MyScopeContract,
} from "./operationalContext";

const NOW = 10_000_000;
const fmt = (ms: number) => `t${ms}`;

function ready<T>(data: T, at = NOW): Snapshot<T> {
  return succeeded(initialSnapshot<T>(), data, at);
}

const SITE_A = { id: "site-a", name: "Pune DC1" };
const SITE_B = { id: "site-b", name: "Austin" };

function safety(over: Partial<AutonomySafetyContract["safety_state"]> = {}, stop = false): AutonomySafetyContract {
  return {
    generated_at: "2026-10-26T12:00:00Z",
    posture: { stop_switch: { active: stop, changed_at: null } },
    safety_state: {
      reported: true,
      every_site_reported: true,
      sites_not_reporting: [],
      site_stop_switches: [],
      suppressions: [],
      ...over,
    },
    scope: { sites: [SITE_A, SITE_B] },
  };
}

function inputs(over: Partial<ContextInputs> = {}): ContextInputs {
  return {
    isPlatformUser: false,
    tenantId: "0f3c9a",
    tenantName: "Acme",
    scope: ready<MyScopeContract>({ enforcement: "strict", tenant_wide: true, synthesis: "granted" }),
    safety: ready(safety()),
    now: NOW,
    staleAfterMs: 150_000,
    formatTime: fmt,
    ...over,
  };
}

const view = (over: Partial<ContextInputs> = {}) => operationalContextView(inputs(over));
const SAFE_WORDS = /\b(safe|healthy|connected|online|all clear)\b/i;

describe("safety is never called safe", () => {
  it("the strongest clear statement is that no stop is in effect", () => {
    const s = view().safety;
    expect(s.state).toBe("no_stop");
    expect(s.text).toBe("No stop in effect");
    expect(`${s.text} ${s.detail}`).not.toMatch(SAFE_WORDS);
  });

  it("the tenant stop switch outranks everything, including not reporting", () => {
    const s = view({ safety: ready(safety({ reported: false }, true)) }).safety;
    expect(s.state).toBe("tenant_stop");
    expect(s.text).toBe("Stop switch ON");
  });

  it("a site halt names the site", () => {
    const s = view({
      safety: ready(safety({ site_stop_switches: [{ site_id: "site-b", active: true }] })),
    }).safety;
    expect(s.state).toBe("site_halt");
    expect(s.text).toBe("1 site halted");
    expect(s.detail).toContain("Austin");
  });

  it("a site that has not reported is unknown, and says so", () => {
    const s = view({
      safety: ready(safety({ every_site_reported: false, sites_not_reporting: ["site-a"] })),
    }).safety;
    expect(s.state).toBe("partially_reported");
    expect(s.text).toBe("1 site not reporting");
    expect(s.detail).toContain("Pune DC1");
  });

  it("nothing reported at all is not reported -- unknown is not safe", () => {
    const s = view({ safety: ready(safety({ reported: false, every_site_reported: false })) }).safety;
    expect(s.state).toBe("not_reported");
    expect(s.detail).toContain("Unknown is not safe");
  });

  it("no site in reach is its own state, not a clean bill", () => {
    const s = view({ safety: ready({ ...safety(), scope: { sites: [] } }) }).safety;
    expect(s.state).toBe("no_sites");
  });

  it("suppressions are reported beside the state, never as a stop", () => {
    const s = view({ safety: ready(safety({ suppressions: [{}, {}] })) }).safety;
    expect(s.state).toBe("no_stop");
    expect(s.detail).toContain("2 fault-domain suppressions active");
  });

  it.each([
    ["loading", initialSnapshot<AutonomySafetyContract>(), "loading"],
    ["denied", failed(initialSnapshot<AutonomySafetyContract>(), classifyFailure(new ApiError("needs fleet.view", 403)), NOW), "denied"],
    ["down", failed(initialSnapshot<AutonomySafetyContract>(), classifyFailure(new ApiError("bad gateway", 502)), NOW), "unavailable"],
  ])("a %s read is its own state, never a guess", (_name, snap, state) => {
    const s = view({ safety: snap }).safety;
    expect(s.state).toBe(state);
    expect(s.text).not.toMatch(SAFE_WORDS);
  });
});

describe("scope describes grants and predicts nothing", () => {
  it("tenant-wide", () => {
    expect(view().scope).toMatchObject({ state: "tenant_wide", text: "Tenant-wide" });
  });

  it("tenant-wide by legacy synthesis says where it came from", () => {
    const s = view({
      scope: ready<MyScopeContract>({ enforcement: "legacy_open", tenant_wide: true, synthesis: "never_granted" }),
    }).scope;
    expect(s.text).toBe("Tenant-wide (legacy posture)");
    expect(s.detail).toContain("never granted");
  });

  it("a narrowed scope counts what the grants name, in every native type", () => {
    const s = view({
      scope: ready<MyScopeContract>({
        enforcement: "strict",
        tenant_wide: false,
        site_ids: ["a", "b"],
        org_unit_paths: ["/u/"],
        device_ids: ["d1"],
        device_classes: ["switch", "server"],
      }),
    }).scope;
    expect(s.state).toBe("narrowed");
    expect(s.text).toBe("2 sites · 1 org unit · 1 device · 2 device classes");
    expect(s.detail).toContain("does not predict");
  });

  it("no reach says the lists are empty because of scope", () => {
    const s = view({ scope: ready<MyScopeContract>({ enforcement: "strict", tenant_wide: false }) }).scope;
    expect(s.state).toBe("no_reach");
    expect(s.detail).toContain("not because nothing is there");
  });

  it("inert grants are reported, not hidden", () => {
    const s = view({
      scope: ready<MyScopeContract>({ enforcement: "strict", tenant_wide: false, site_ids: ["a"], inert_grants: [{}] }),
    }).scope;
    expect(s.detail).toContain("1 grant names a target that no longer exists");
  });
});

describe("posture reads the server's closed vocabulary", () => {
  it.each([
    ["strict", "strict", "Strict"],
    ["legacy_open", "legacy_open", "Legacy open"],
    ["permissive", "unknown", "Unknown"],
    [undefined, "unknown", "Unknown"],
  ])("%s reads as %s", (raw, state, text) => {
    const p = view({ scope: ready<MyScopeContract>({ enforcement: raw as string | undefined }) }).posture;
    expect(p).toMatchObject({ state, text });
  });
});

describe("freshness states when the Console last heard, never that it is connected", () => {
  it("current", () => {
    const f = view().freshness;
    expect(f).toMatchObject({ state: "current", text: `Updated t${NOW}` });
    expect(`${f.text} ${f.detail}`).not.toMatch(SAFE_WORDS);
  });

  it("a failed refresh keeps the last time and says it failed", () => {
    const s = failed(ready(safety(), NOW - 60_000), classifyFailure(new ApiError("bad gateway", 502)), NOW);
    const f = view({ safety: s }).freshness;
    expect(f.state).toBe("stale");
    expect(f.text).toBe(`Last updated t${NOW - 60_000}`);
    expect(f.detail).toContain("bad gateway");
  });

  it("a poll that silently stopped turns stale by age", () => {
    const old = NOW - 200_000;
    const f = view({ scope: ready<MyScopeContract>({ enforcement: "strict", tenant_wide: true }, old), safety: ready(safety(), old) }).freshness;
    expect(f.state).toBe("stale");
  });

  it("the oldest part sets the age", () => {
    const f = view({ safety: ready(safety(), NOW - 30_000) }).freshness;
    expect(f.text).toBe(`Updated t${NOW - 30_000}`);
  });

  it("never updated is not loading", () => {
    const down = failed(initialSnapshot<MyScopeContract>(), classifyFailure(new ApiError("down", 503)), NOW);
    const f = view({ scope: down, safety: failed(initialSnapshot<AutonomySafetyContract>(), classifyFailure(new ApiError("down", 503)), NOW) }).freshness;
    expect(f.state).toBe("never");
  });
});

describe("a platform identity is not asked about", () => {
  const p = view({ isPlatformUser: true, tenantName: null, scope: initialSnapshot(), safety: initialSnapshot() });

  it("scope, posture, safety and freshness are not applicable -- not loading, not unknown", () => {
    for (const fact of [p.scope, p.posture, p.safety, p.freshness]) {
      expect(fact.state).toBe("not_applicable");
    }
  });

  it("platform staff see the tenant id they navigated to", () => {
    expect(p.tenant.text).toBe("0f3c9a");
  });

  it("a tenant user is never shown a raw id as a name", () => {
    expect(view({ tenantName: null }).tenant.text).toBe("Your tenant");
  });
});

describe("every fact always has words", () => {
  it("no text and no detail is ever empty, in any state", () => {
    const variants: Partial<ContextInputs>[] = [
      {},
      { isPlatformUser: true },
      { scope: initialSnapshot(), safety: initialSnapshot() },
      { tenantName: null },
      { safety: ready(safety({ reported: false })) },
    ];
    for (const v of variants) {
      for (const fact of Object.values(view(v))) {
        expect(fact.text.trim()).not.toBe("");
        expect(fact.detail.trim()).not.toBe("");
      }
    }
  });
});
