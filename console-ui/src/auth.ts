/**
 * OIDC PKCE flow helpers for Keycloak integration.
 */

/** Generate a cryptographically random code verifier (43-128 chars). */
export function generateCodeVerifier(): string {
  const array = new Uint8Array(32);
  crypto.getRandomValues(array);
  return base64UrlEncode(array);
}

/** Derive the S256 code challenge from a verifier. */
export async function generateCodeChallenge(verifier: string): Promise<string> {
  const encoder = new TextEncoder();
  const data = encoder.encode(verifier);
  const digest = await crypto.subtle.digest("SHA-256", data);
  return base64UrlEncode(new Uint8Array(digest));
}

/** Build the Keycloak authorization URL for PKCE login. */
export function buildAuthUrl(
  keycloakUrl: string,
  realm: string,
  clientId: string,
  redirectUri: string,
  codeChallenge: string,
): string {
  const params = new URLSearchParams({
    response_type: "code",
    client_id: clientId,
    redirect_uri: redirectUri,
    code_challenge: codeChallenge,
    code_challenge_method: "S256",
    scope: "openid profile email",
  });
  return `${keycloakUrl}/realms/${realm}/protocol/openid-connect/auth?${params}`;
}

interface TokenResponse {
  access_token: string;
  refresh_token: string;
  expires_in: number;
}

/** Exchange an authorization code for tokens. */
export async function exchangeCode(
  keycloakUrl: string,
  realm: string,
  clientId: string,
  redirectUri: string,
  code: string,
  codeVerifier: string,
): Promise<TokenResponse> {
  const body = new URLSearchParams({
    grant_type: "authorization_code",
    client_id: clientId,
    redirect_uri: redirectUri,
    code,
    code_verifier: codeVerifier,
  });
  const resp = await fetch(
    `${keycloakUrl}/realms/${realm}/protocol/openid-connect/token`,
    {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: body.toString(),
    },
  );
  if (!resp.ok) {
    throw new Error(`Token exchange failed: ${resp.status} ${resp.statusText}`);
  }
  return resp.json();
}

/** Refresh an access token using a refresh token. */
export async function refreshAccessToken(
  keycloakUrl: string,
  realm: string,
  clientId: string,
  refreshToken: string,
): Promise<TokenResponse> {
  const body = new URLSearchParams({
    grant_type: "refresh_token",
    client_id: clientId,
    refresh_token: refreshToken,
  });
  const resp = await fetch(
    `${keycloakUrl}/realms/${realm}/protocol/openid-connect/token`,
    {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: body.toString(),
    },
  );
  if (!resp.ok) {
    throw new Error(`Token refresh failed: ${resp.status} ${resp.statusText}`);
  }
  return resp.json();
}

/**
 * Decode a JWT payload without verification.
 * For UI display only -- never trust this for authorization.
 */
export function parseJwt(token: string): Record<string, unknown> {
  const parts = token.split(".");
  if (parts.length !== 3) throw new Error("Invalid JWT");
  const payload = parts[1];
  const json = atob(payload.replace(/-/g, "+").replace(/_/g, "/"));
  return JSON.parse(json);
}

/** Build the Keycloak logout URL. */
export function buildLogoutUrl(
  keycloakUrl: string,
  realm: string,
  redirectUri: string,
): string {
  const params = new URLSearchParams({
    post_logout_redirect_uri: redirectUri,
    client_id: "harkeniq-console",
  });
  return `${keycloakUrl}/realms/${realm}/protocol/openid-connect/logout?${params}`;
}

/* ── helpers ──────────────────────────────────────── */

function base64UrlEncode(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}
