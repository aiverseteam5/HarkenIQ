import {
  createContext,
  createElement,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { setAccessToken, clearAccessToken, setOnAuthError } from "./api";
import {
  generateCodeVerifier,
  generateCodeChallenge,
  buildAuthUrl,
  refreshAccessToken,
  parseJwt,
  buildLogoutUrl,
} from "./auth";

/* ── Configuration ────────────────────────────────── */

const KEYCLOAK_URL = import.meta.env.VITE_KEYCLOAK_URL ?? "";
const KEYCLOAK_REALM = import.meta.env.VITE_KEYCLOAK_REALM ?? "harkeniq";
const CLIENT_ID = import.meta.env.VITE_OIDC_CLIENT_ID ?? "harkeniq-console";
const DEV_MODE = import.meta.env.VITE_AUTH_DEV_MODE === "true";

/* ── Types ────────────────────────────────────────── */

export interface AuthUser {
  email: string;
  name: string;
  role: string;
  tenant_id: string;
  permissions: string[];
  is_platform_user: boolean;
}

interface AuthState {
  user: AuthUser | null;
  isAuthenticated: boolean;
  login: () => void;
  logout: () => void;
  loading: boolean;
  /** Complete an OIDC code exchange: store tokens AND resolve the user
   *  before returning, so callers can navigate without racing RequireAuth
   *  (D1 fix, P0 2026-08-29). */
  completeLogin: (tokens: {
    access_token: string;
    refresh_token: string;
    expires_in: number;
  }) => Promise<void>;
}

/* ── Dev mock ─────────────────────────────────────── */

const MOCK_USER: AuthUser = {
  email: "admin@harkeniq.local",
  name: "Dev Admin",
  role: "platform_admin",
  tenant_id: "dev-tenant",
  permissions: ["*"],
  is_platform_user: true,
};

/* ── Context ──────────────────────────────────────── */

const AuthContext = createContext<AuthState>({
  user: null,
  isAuthenticated: false,
  login: () => {},
  logout: () => {},
  loading: true,
  completeLogin: async () => {},
});

/* ── Provider ─────────────────────────────────────── */

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(DEV_MODE ? MOCK_USER : null);
  const [loading, setLoading] = useState(!DEV_MODE);
  const refreshTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const clearSession = useCallback(() => {
    clearAccessToken();
    sessionStorage.removeItem("hiq_refresh_token");
    sessionStorage.removeItem("hiq_code_verifier");
    setUser(null);
  }, []);

  // Wire up global auth error handler
  useEffect(() => {
    if (!DEV_MODE) {
      setOnAuthError(() => clearSession());
    }
  }, [clearSession]);

  const setTokensFromResponse = useCallback(
    async (accessToken: string, refreshToken: string, expiresIn: number) => {
      setAccessToken(accessToken);
      sessionStorage.setItem("hiq_refresh_token", refreshToken);

      // Identity comes from the server, not from decoding the token.
      // Spec S4: enforcement is server-side and the UI only reflects it,
      // so the UI asks what it may do rather than guessing. The token
      // carries no permissions / tenant_id / is_platform_user claim, and
      // its role claim is realm_roles (not realm_access.roles), which is
      // why every user used to render as "viewer".
      //
      // D1 fix (P0 2026-08-29): AWAITED. The un-awaited version let
      // `loading` flip false while `user` was still null, so RequireAuth
      // bounced every valid session to /login on first paint.
      setUser(await fetchMe(accessToken));

      // Schedule token refresh at 80% of expiry
      if (refreshTimerRef.current) clearTimeout(refreshTimerRef.current);
      const refreshMs = Math.max((expiresIn * 0.8 - 5) * 1000, 10000);
      refreshTimerRef.current = setTimeout(() => void doRefresh(), refreshMs);
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  const doRefresh = useCallback(async () => {
    const rt = sessionStorage.getItem("hiq_refresh_token");
    if (!rt || !KEYCLOAK_URL) {
      clearSession();
      return;
    }
    try {
      const tokens = await refreshAccessToken(KEYCLOAK_URL, KEYCLOAK_REALM, CLIENT_ID, rt);
      await setTokensFromResponse(tokens.access_token, tokens.refresh_token, tokens.expires_in);
    } catch {
      clearSession();
    }
  }, [clearSession, setTokensFromResponse]);

  const completeLogin = useCallback(
    async (tokens: {
      access_token: string;
      refresh_token: string;
      expires_in: number;
    }) => {
      await setTokensFromResponse(
        tokens.access_token, tokens.refresh_token, tokens.expires_in,
      );
    },
    [setTokensFromResponse],
  );

  // On mount, try silent refresh if we have a refresh token
  useEffect(() => {
    if (DEV_MODE) {
      setLoading(false);
      return;
    }
    const rt = sessionStorage.getItem("hiq_refresh_token");
    if (rt && KEYCLOAK_URL) {
      doRefresh().finally(() => setLoading(false));
    } else {
      setLoading(false);
    }
    return () => {
      if (refreshTimerRef.current) clearTimeout(refreshTimerRef.current);
    };
  }, [doRefresh]);

  const login = useCallback(async () => {
    if (DEV_MODE) {
      setUser(MOCK_USER);
      return;
    }
    const verifier = generateCodeVerifier();
    sessionStorage.setItem("hiq_code_verifier", verifier);
    const challenge = await generateCodeChallenge(verifier);
    const redirectUri = `${window.location.origin}/callback`;
    const url = buildAuthUrl(KEYCLOAK_URL, KEYCLOAK_REALM, CLIENT_ID, redirectUri, challenge);
    window.location.href = url;
  }, []);

  const logout = useCallback(() => {
    clearSession();
    if (!DEV_MODE && KEYCLOAK_URL) {
      const redirectUri = window.location.origin;
      window.location.href = buildLogoutUrl(KEYCLOAK_URL, KEYCLOAK_REALM, redirectUri);
    }
  }, [clearSession]);

  const value: AuthState = {
    user,
    isAuthenticated: user !== null,
    login,
    logout,
    loading,
    completeLogin,
  };

  return createElement(AuthContext.Provider, { value }, children);
}

export function useAuth(): AuthState {
  return useContext(AuthContext);
}

/* ── helpers ──────────────────────────────────────── */

/** GET /api/me — the server's own view of who this is and what they may do.
 *
 *  Replaces the previous token-decoding path, which was wrong three ways:
 *  it read realm_access.roles (Keycloak mints realm_roles), it filtered
 *  for an "hiq_" prefix the roles do not carry, and permissions /
 *  tenant_id / is_platform_user are not minted as claims at all. Every
 *  user therefore fell through to the "viewer" default, super admins
 *  included.
 */
async function fetchMe(accessToken: string): Promise<AuthUser | null> {
  try {
    const resp = await fetch("/api/me", {
      headers: { authorization: `Bearer ${accessToken}` },
    });
    if (!resp.ok) return null;
    const me = (await resp.json()) as {
      email?: string;
      role?: string;
      tenant_id?: string | null;
      is_platform_user?: boolean;
      permissions?: string[];
    };
    return {
      email: me.email ?? "",
      // /api/me has no display name; the token is the right source for it.
      name: displayName(accessToken, me.email ?? ""),
      role: me.role ?? "viewer",
      tenant_id: me.tenant_id ?? "",
      permissions: me.permissions ?? [],
      is_platform_user: me.is_platform_user ?? false,
    };
  } catch {
    return null;
  }
}

function displayName(accessToken: string, fallback: string): string {
  try {
    const claims = parseJwt(accessToken);
    return (
      (claims.name as string) ??
      (claims.preferred_username as string) ??
      fallback
    );
  } catch {
    return fallback;
  }
}
