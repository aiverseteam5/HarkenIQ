/** A34.8: the one contract for "what state is this data in?".
 *
 *  Every surface asks the same question of every read, and before this
 *  module each page answered it alone -- usually as "data, or a toast".
 *  That collapses five different situations into one, and two of them are
 *  dangerous to collapse:
 *
 *  - an empty or missing answer that is really a REFUSAL reads as "there is
 *    nothing", which on an incident list means "nothing is wrong";
 *  - data that stopped refreshing keeps rendering as though it were
 *    current (A28.6 P7: silence never renders as health).
 *
 *  So the states are named, and each one is distinguishable on screen:
 *
 *    loading      nothing yet, a request is in flight
 *    ready        the latest request succeeded
 *    stale        data is shown, but the latest refresh failed, or the
 *                 last success is older than the caller tolerates
 *    partial      some parts of a composite view loaded and some did not
 *    denied       403 -- the server refused this identity
 *    not_found    404 -- absent, or not visible in the caller's scope;
 *                 Central Command answers both with one 404 on purpose, so
 *                 the screen never claims which
 *    refused      another 4xx -- the server refused the request and said
 *                 why (P4: its own words are shown)
 *    unavailable  no response, or 5xx
 *
 *  A 401 is not a data state: it ends the session and `useAuth` handles it
 *  once, for everyone.
 *
 *  Pure: no React, no fetching. `useDataState` is the hook over it.
 */

import { ApiError, AuthError } from "../apiErrors";
import { closedVocabulary } from "./vocabulary";

export const FAILURE_KINDS = ["denied", "not_found", "refused", "unavailable"] as const;
export type FailureKind = (typeof FAILURE_KINDS)[number];

export const DATA_STATUSES = [
  "loading",
  "ready",
  "stale",
  "partial",
  ...FAILURE_KINDS,
] as const;
export type DataStatus = (typeof DATA_STATUSES)[number];

export const dataStatus = closedVocabulary(DATA_STATUSES);

export interface Failure {
  kind: FailureKind;
  /** The HTTP status, or 0 when nothing answered. */
  status: number;
  /** The server's own reason when it gave one (P4). */
  message: string;
}

/** What a failed request means for the data it was fetching, or null for
 *  a 401, which ends the session instead. Anything that is not an
 *  `ApiError` -- a bug, a malformed body -- reads as unavailable, never as
 *  empty. */
export function classifyFailure(err: unknown): Failure | null {
  if (err instanceof AuthError) return null;
  if (err instanceof ApiError) {
    const { status, message } = err;
    if (status === 403) return { kind: "denied", status, message };
    if (status === 404) return { kind: "not_found", status, message };
    if (status >= 400 && status < 500) return { kind: "refused", status, message };
    return { kind: "unavailable", status, message };
  }
  const message = err instanceof Error ? err.message : String(err);
  return { kind: "unavailable", status: 0, message };
}

/** Everything known about one read, over time. */
export interface Snapshot<T> {
  data: T | null;
  /** The latest attempt's failure, cleared by the next success. */
  failure: Failure | null;
  /** Epoch ms of the last success, or null if there has been none. */
  lastSuccessAt: number | null;
  /** Epoch ms of the latest completed attempt, success or failure. */
  lastAttemptAt: number | null;
}

export function initialSnapshot<T>(): Snapshot<T> {
  return { data: null, failure: null, lastSuccessAt: null, lastAttemptAt: null };
}

export function succeeded<T>(prev: Snapshot<T>, data: T, now: number): Snapshot<T> {
  return { ...prev, data, failure: null, lastSuccessAt: now, lastAttemptAt: now };
}

/** A failure keeps the last good data: it becomes stale, never blank.
 *  `null` (a 401) leaves the snapshot alone; the session handles it. */
export function failed<T>(prev: Snapshot<T>, failure: Failure | null, now: number): Snapshot<T> {
  if (failure === null) return prev;
  return { ...prev, failure, lastAttemptAt: now };
}

export interface StatusOptions {
  now: number;
  /** Data older than this reads as stale even if nothing failed -- a poll
   *  that silently stopped must not keep looking current. */
  staleAfterMs?: number;
}

export function statusOf(snapshot: Snapshot<unknown>, opts: StatusOptions): DataStatus {
  if (snapshot.data !== null) {
    if (snapshot.failure) return "stale";
    if (
      opts.staleAfterMs !== undefined &&
      snapshot.lastSuccessAt !== null &&
      opts.now - snapshot.lastSuccessAt > opts.staleAfterMs
    ) {
      return "stale";
    }
    return "ready";
  }
  if (snapshot.failure) return snapshot.failure.kind;
  return "loading";
}

/** Which failure a composite view reports when every part failed: the one
 *  that most needs saying. Nothing answered outranks a refusal, which
 *  outranks a narrower refusal. */
const SEVERITY: Record<FailureKind, number> = {
  unavailable: 4,
  denied: 3,
  refused: 2,
  not_found: 1,
};

/** The state of a view composed from several reads. */
export function combineStatuses(statuses: readonly DataStatus[]): DataStatus {
  if (statuses.length === 0) return "loading";
  const failures = statuses.filter(
    (s): s is FailureKind => (FAILURE_KINDS as readonly string[]).includes(s),
  );
  const shown = statuses.filter((s) => s === "ready" || s === "stale" || s === "partial");
  if (failures.length === statuses.length) {
    return failures.reduce((a, b) => (SEVERITY[b] > SEVERITY[a] ? b : a));
  }
  if (failures.length > 0 || statuses.includes("partial")) {
    return shown.length > 0 ? "partial" : "loading";
  }
  if (statuses.includes("loading")) return "loading";
  if (statuses.includes("stale")) return "stale";
  return "ready";
}
