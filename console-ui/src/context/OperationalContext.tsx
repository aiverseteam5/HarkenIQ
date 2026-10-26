import { createContext, type ReactNode, useCallback, useContext, useEffect, useState } from "react";
import { getJson } from "../api";
import { useAuth } from "../useAuth";
import { useDataState } from "../useDataState";
import {
  operationalContextView,
  type AutonomySafetyContract,
  type MyScopeContract,
  type OperationalContextView,
} from "../viewModel/operationalContext";

/** A34.6: the operational context every tenant page lives inside.
 *
 *  One provider at the tenant layout, so the shell and any page read the
 *  SAME answer and nothing fetches it twice. It reads existing contracts
 *  only -- the tenant name, `/scope-grants/me` and the autonomy contract --
 *  and turns them into words through `viewModel/operationalContext`, which
 *  is where the wording rules live.
 *
 *  For a platform identity the Central Command reads are never made: Central
 *  Command refuses a platform-realm token with 401 (E1.4), and the client
 *  treats a 401 as the end of the session, so asking would sign platform
 *  staff out of every tenant page, Console-native ones included.
 *
 *  The autonomy contract tallies outcome history on every request (A30.41),
 *  so it is read once a minute and not at all while the tab is hidden.
 */

const CADENCE_MS = 60_000;
/** Two missed refreshes and a little slack: older than this is stale. */
const STALE_AFTER_MS = 2.5 * CADENCE_MS;

export interface OperationalContextValue {
  tenantId: string;
  view: OperationalContextView;
  /** Re-read now -- e.g. after this page changed the stop switch. */
  refresh: () => void;
}

const Ctx = createContext<OperationalContextValue | null>(null);

interface TenantOption {
  id: string;
  name: string;
}

/** The tenant's display name, by the read each plane is allowed: a tenant
 *  user reads their own tenant through `/api/me/tenants` (every role can),
 *  platform staff read the registry. */
function useTenantName(tenantId: string, isPlatformUser: boolean | null): string | null {
  const [name, setName] = useState<string | null>(null);
  useEffect(() => {
    if (isPlatformUser === null) return;
    let cancelled = false;
    setName(null);
    void (async () => {
      try {
        if (isPlatformUser) {
          const res = await getJson<TenantOption>(`/api/admin/tenants/${tenantId}`);
          if (!cancelled) setName(res.name ?? null);
        } else {
          const res = await getJson<{ tenants: TenantOption[] }>("/api/me/tenants");
          const own = res.tenants?.find((t) => t.id === tenantId) ?? null;
          if (!cancelled) setName(own?.name ?? null);
        }
      } catch {
        if (!cancelled) setName(null);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [tenantId, isPlatformUser]);
  return name;
}

export function OperationalContextProvider({
  tenantId,
  children,
}: {
  tenantId: string;
  children: ReactNode;
}) {
  const { user } = useAuth();
  const isPlatformUser = user ? user.is_platform_user : null;
  const asksCentralCommand = isPlatformUser === false;
  const tenantName = useTenantName(tenantId, isPlatformUser);

  const scope = useDataState(
    () => getJson<MyScopeContract>(`/api/t/${tenantId}/scope-grants/me`),
    [tenantId],
    { pollMs: CADENCE_MS, pauseWhenHidden: true, enabled: asksCentralCommand },
  );
  const safety = useDataState(
    () => getJson<AutonomySafetyContract>(`/api/t/${tenantId}/autonomy/`),
    [tenantId],
    { pollMs: CADENCE_MS, pauseWhenHidden: true, enabled: asksCentralCommand },
  );

  const refreshScope = scope.refresh;
  const refreshSafety = safety.refresh;
  const refresh = useCallback(() => {
    void refreshScope();
    void refreshSafety();
  }, [refreshScope, refreshSafety]);

  const view = operationalContextView({
    isPlatformUser: isPlatformUser ?? false,
    tenantId,
    tenantName,
    scope: scope.snapshot,
    safety: safety.snapshot,
    now: Date.now(),
    staleAfterMs: STALE_AFTER_MS,
  });

  return <Ctx.Provider value={{ tenantId, view, refresh }}>{children}</Ctx.Provider>;
}

/** The context of the tenant this page is in, or null on the platform plane. */
export function useOperationalContext(): OperationalContextValue | null {
  return useContext(Ctx);
}
