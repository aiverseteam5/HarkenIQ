/** A34.8: the data-state contract.
 *
 *  The VIEW-MODEL test pattern (A34.3): a pure function, asserted over the
 *  closed vocabulary it reads. No DOM, no React, no network.
 */

import { describe, expect, it } from "vitest";
import { ApiError, AuthError } from "../apiErrors";
import {
  DATA_STATUSES,
  classifyFailure,
  combineStatuses,
  dataStatus,
  failed,
  initialSnapshot,
  statusOf,
  succeeded,
} from "./dataState";
import { UNKNOWN, closedVocabulary } from "./vocabulary";

describe("a closed vocabulary reads the server's spelling and nothing else", () => {
  const v = closedVocabulary(["strict", "legacy_open"] as const);

  it("accepts each known value as itself", () => {
    for (const value of v.values) expect(v.read(value)).toBe(value);
  });

  it.each([undefined, null, "", "STRICT", " strict", "open", 1, true, {}, ["strict"]])(
    "turns %j into unknown",
    (raw) => {
      expect(v.read(raw)).toBe(UNKNOWN);
      expect(v.has(raw)).toBe(false);
    },
  );
});

describe("a failure says what it means for the data", () => {
  it.each([
    [403, "denied"],
    [404, "not_found"],
    [400, "refused"],
    [409, "refused"],
    [422, "refused"],
    [500, "unavailable"],
    [502, "unavailable"],
    [503, "unavailable"],
    [0, "unavailable"],
  ])("HTTP %i is %s", (status, kind) => {
    const f = classifyFailure(new ApiError("the server's reason", status));
    expect(f).toEqual({ kind, status, message: "the server's reason" });
  });

  it("a 401 is the session's to handle, not a data state", () => {
    expect(classifyFailure(new AuthError())).toBeNull();
  });

  it("anything that is not an ApiError reads as unavailable, never as empty", () => {
    expect(classifyFailure(new TypeError("boom"))?.kind).toBe("unavailable");
    expect(classifyFailure("a string")?.kind).toBe("unavailable");
  });
});

describe("a snapshot's status over time", () => {
  const now = 1_000_000;
  const denied = classifyFailure(new ApiError("no", 403));
  const down = classifyFailure(new ApiError("gateway", 503));

  it("loads, then is ready", () => {
    const s0 = initialSnapshot<number>();
    expect(statusOf(s0, { now })).toBe("loading");
    expect(statusOf(succeeded(s0, 7, now), { now })).toBe("ready");
  });

  it("a first failure is the failure's kind", () => {
    expect(statusOf(failed(initialSnapshot(), denied, now), { now })).toBe("denied");
    expect(statusOf(failed(initialSnapshot(), down, now), { now })).toBe("unavailable");
  });

  it("a failure after a success keeps the data and turns stale -- never blank", () => {
    const s = failed(succeeded(initialSnapshot<number>(), 7, now), down, now + 1);
    expect(s.data).toBe(7);
    expect(statusOf(s, { now })).toBe("stale");
  });

  it("the next success clears the failure", () => {
    const s = succeeded(failed(succeeded(initialSnapshot<number>(), 7, now), down, now), 8, now + 2);
    expect(s.failure).toBeNull();
    expect(statusOf(s, { now })).toBe("ready");
  });

  it("data that stopped refreshing reads stale even though nothing failed", () => {
    const s = succeeded(initialSnapshot<number>(), 7, now);
    expect(statusOf(s, { now: now + 59_000, staleAfterMs: 60_000 })).toBe("ready");
    expect(statusOf(s, { now: now + 61_000, staleAfterMs: 60_000 })).toBe("stale");
  });

  it("a 401 leaves the snapshot exactly as it was", () => {
    const s = succeeded(initialSnapshot<number>(), 7, now);
    expect(failed(s, classifyFailure(new AuthError()), now + 5)).toBe(s);
  });
});

describe("a composite view", () => {
  it("is ready only when every part is", () => {
    expect(combineStatuses(["ready", "ready"])).toBe("ready");
    expect(combineStatuses(["ready", "stale"])).toBe("stale");
    expect(combineStatuses(["ready", "loading"])).toBe("loading");
  });

  it("is partial when something shown sits beside something that failed", () => {
    expect(combineStatuses(["ready", "denied"])).toBe("partial");
    expect(combineStatuses(["stale", "unavailable"])).toBe("partial");
  });

  it("reports the failure that most needs saying when every part failed", () => {
    expect(combineStatuses(["not_found", "denied"])).toBe("denied");
    expect(combineStatuses(["denied", "unavailable"])).toBe("unavailable");
    expect(combineStatuses(["refused", "not_found"])).toBe("refused");
  });

  it("never invents a status outside the vocabulary", () => {
    for (const s of DATA_STATUSES) expect(dataStatus.has(combineStatuses([s, s]))).toBe(true);
  });
});
