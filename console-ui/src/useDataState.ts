import { useCallback, useEffect, useRef, useState } from "react";
import {
  classifyFailure,
  failed,
  initialSnapshot,
  statusOf,
  succeeded,
  type DataStatus,
  type Snapshot,
} from "./viewModel/dataState";

/** A34.8: one read, its state over time, and how often to repeat it.
 *
 *  The hook over `viewModel/dataState`. It keeps the last good data when a
 *  refresh fails (stale, never blank), drops an answer that arrives for a
 *  request a newer one has superseded, and -- when asked -- does not poll
 *  a tab nobody is looking at.
 */

export interface DataStateOptions {
  /** Repeat the read this often. Omit for a one-off read. */
  pollMs?: number;
  /** Data older than this reads as stale even if nothing failed. */
  staleAfterMs?: number;
  /** Skip polls while the document is hidden; refresh on return. */
  pauseWhenHidden?: boolean;
  /** False: make no request at all, and stay in the initial state. */
  enabled?: boolean;
}

export interface DataStateHandle<T> {
  snapshot: Snapshot<T>;
  status: DataStatus;
  refresh: () => Promise<void>;
}

function hidden(): boolean {
  return typeof document !== "undefined" && document.visibilityState === "hidden";
}

export function useDataState<T>(
  fetcher: () => Promise<T>,
  deps: readonly unknown[],
  options: DataStateOptions = {},
): DataStateHandle<T> {
  const { pollMs, staleAfterMs, pauseWhenHidden = false, enabled = true } = options;
  const [snapshot, setSnapshot] = useState<Snapshot<T>>(initialSnapshot);
  // Every request takes a ticket; only the newest ticket may write. A slow
  // answer for the previous tenant must never land on the current one.
  const ticket = useRef(0);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const refresh = useCallback(async () => {
    if (!enabled) return;
    const mine = ++ticket.current;
    try {
      const data = await fetcherRef.current();
      if (mine === ticket.current) setSnapshot((prev) => succeeded(prev, data, Date.now()));
    } catch (err) {
      if (mine === ticket.current) {
        setSnapshot((prev) => failed(prev, classifyFailure(err), Date.now()));
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, ...deps]);

  // A new subject starts from nothing: the old subject's data is not this
  // subject's stale data.
  useEffect(() => {
    ticket.current += 1;
    setSnapshot(initialSnapshot());
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!enabled || pollMs === undefined) return;
    const timer = setInterval(() => {
      if (pauseWhenHidden && hidden()) return;
      void refresh();
    }, pollMs);
    const onVisible = () => {
      if (pauseWhenHidden && !hidden()) void refresh();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [enabled, pollMs, pauseWhenHidden, refresh]);

  return {
    snapshot,
    status: statusOf(snapshot, { now: Date.now(), staleAfterMs }),
    refresh,
  };
}
