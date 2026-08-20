import { useCallback, useEffect, useState } from "react";
import { AuthError } from "./api";

const POLL_MS = 5000;

/**
 * Fetch immediately, then every 5s. Reports auth failures separately.
 * Calls onAuthError when a 401 is received.
 */
export function usePoll<T>(
  fetcher: () => Promise<T>,
  deps: unknown[] = [],
  onAuthError?: () => void,
) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [authFailed, setAuthFailed] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setData(await fetcher());
      setError(null);
      setAuthFailed(false);
    } catch (e) {
      if (e instanceof AuthError) {
        setAuthFailed(true);
        if (onAuthError) onAuthError();
      } else {
        setError(e instanceof Error ? e.message : String(e));
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), POLL_MS);
    return () => clearInterval(timer);
  }, [refresh]);

  return { data, error, authFailed, refresh };
}
