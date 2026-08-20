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
    (accessToken: string, refreshToken: string, expiresIn: number) => {
      setAccessToken(accessToken);
      sessionStorage.setItem("hiq_refresh_token", refreshToken);

      // Parse user info from JWT
      try {
        const claims = parseJwt(accessToken);
        setUser({
          email: (claims.email as string) ?? "",
          name: (claims.name as string) ?? (claims.preferred_username as string) ?? "",
          role: extractRole(claims),
          tenant_id: (claims.tenant_id as string) ?? "",
          permissions: (claims.permissions as string[]) ?? [],
          is_platform_user: (claims.is_platform_user as boolean) ?? false,
        });
      } catch {
        setUser(null);
      }

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
      setTokensFromResponse(tokens.access_token, tokens.refresh_token, tokens.expires_in);
    } catch {
      clearSession();
    }
  }, [clearSession, setTokensFromResponse]);

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
  };

  return createElement(AuthContext.Provider, { value }, children);
}

export function useAuth(): AuthState {
  return useContext(AuthContext);
}

/* ── helpers ──────────────────────────────────────── */

function extractRole(claims: Record<string, unknown>): string {
  // Try realm_access.roles, then resource_access
  const realmAccess = claims.realm_access as { roles?: string[] } | undefined;
  if (realmAccess?.roles) {
    const hiqRoles = realmAccess.roles.filter((r) => r.startsWith("hiq_"));
    if (hiqRoles.length > 0) return hiqRoles[0];
  }
  return (claims.role as string) ?? "viewer";
}
