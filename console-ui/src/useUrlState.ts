import { useCallback, useMemo } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";

/** A34.4: page state that belongs in the URL.
 *
 *  Two hooks, one for each kind of state a page has:
 *
 *  - `useQueryState`: collection state -- filters, tab, page number. It
 *    lives in the query string and is REPLACED as it changes, so the
 *    history holds places, not keystrokes. A value equal to its default
 *    is left out, so an unfiltered list has a clean URL.
 *  - `useObjectRoute`: the object a page has open. Opening one PUSHES its
 *    URL, so Back closes it; closing returns to the list with the list's
 *    own query string intact.
 *
 *  Neither decides anything. A URL names what to ask for; Central Command
 *  decides what the answer is.
 */

export function useQueryState<K extends string>(
  defaults: Readonly<Record<K, string>>,
): readonly [Readonly<Record<K, string>>, (patch: Partial<Record<K, string>>) => void, () => void] {
  const [params, setParams] = useSearchParams();

  const values = useMemo(() => {
    const out = {} as Record<K, string>;
    for (const key of Object.keys(defaults) as K[]) {
      out[key] = params.get(key) ?? defaults[key];
    }
    return out;
  }, [params, defaults]);

  const set = useCallback(
    (patch: Partial<Record<K, string>>) => {
      setParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          for (const [key, value] of Object.entries(patch) as [K, string | undefined][]) {
            if (value === undefined || value === "" || value === defaults[key]) next.delete(key);
            else next.set(key, value);
          }
          return next;
        },
        { replace: true },
      );
    },
    [setParams, defaults],
  );

  const reset = useCallback(() => {
    setParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        for (const key of Object.keys(defaults)) next.delete(key);
        return next;
      },
      { replace: true },
    );
  }, [setParams, defaults]);

  return [values, set, reset] as const;
}

/** `path` with `search` merged in; the path's own query wins. */
export function withSearch(path: string, search: string): string {
  const [pathname, own = ""] = path.split("?", 2);
  const merged = new URLSearchParams(search);
  for (const [key, value] of new URLSearchParams(own)) merged.set(key, value);
  const s = merged.toString();
  return s ? `${pathname}?${s}` : pathname;
}

interface OpenedFromList {
  /** Set when this page pushed the object's URL itself, so closing can
   *  step back to the exact list entry instead of adding another. */
  openedFromList?: boolean;
}

export function useObjectRoute(listPath: string) {
  const navigate = useNavigate();
  const location = useLocation();

  const open = useCallback(
    (objectPath: string) => {
      navigate(withSearch(objectPath, location.search), {
        state: { openedFromList: true } satisfies OpenedFromList,
      });
    },
    [navigate, location.search],
  );

  const close = useCallback(() => {
    if ((location.state as OpenedFromList | null)?.openedFromList) {
      navigate(-1);
    } else {
      // Arrived by link, bookmark or reload: there is no list entry behind
      // this one to step back to, so the list replaces the object.
      navigate(withSearch(listPath, location.search), { replace: true });
    }
  }, [navigate, location.state, location.search, listPath]);

  return { open, close } as const;
}
