/**
 * API client for HarkenIQ Console.
 * Token stored in module scope (not sessionStorage) for security.
 */

export class AuthError extends Error {
  constructor(message = "unauthorized") {
    super(message);
    this.name = "AuthError";
  }
}

let _accessToken = "";
let _onAuthError: (() => void) | null = null;
/** Platform admins act on one tenant at a time; the "current" alias needs to
 *  be told which. Selection only — the server still authorizes every call,
 *  so naming a tenant you are not entitled to yields 403, not access. */
let _activeTenant = "";

export function setAccessToken(token: string): void {
  _accessToken = token;
}

export function getAccessToken(): string {
  return _accessToken;
}

export function clearAccessToken(): void {
  _accessToken = "";
}

export function setActiveTenant(tenantId: string): void {
  _activeTenant = tenantId;
  try {
    if (tenantId) localStorage.setItem("hiq_active_tenant", tenantId);
    else localStorage.removeItem("hiq_active_tenant");
  } catch {
    /* storage unavailable (private window, blocked cookies) — in-memory is fine */
  }
}

export function getActiveTenant(): string {
  if (_activeTenant) return _activeTenant;
  try {
    return localStorage.getItem("hiq_active_tenant") ?? "";
  } catch {
    return "";
  }
}

export function setOnAuthError(callback: () => void): void {
  _onAuthError = callback;
}

async function request(path: string, init?: RequestInit): Promise<Response> {
  const headers = new Headers(init?.headers);
  if (_accessToken) {
    headers.set("authorization", `Bearer ${_accessToken}`);
  }
  const tenant = getActiveTenant();
  if (tenant) {
    headers.set("x-harken-tenant", tenant);
  }
  const resp = await fetch(path, { ...init, headers });
  if (resp.status === 401) {
    if (_onAuthError) _onAuthError();
    throw new AuthError();
  }
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      detail = (await resp.json()).detail ?? detail;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return resp;
}

export async function getJson<T>(path: string): Promise<T> {
  return (await request(path)).json();
}

export async function postJson<T>(path: string, body: unknown): Promise<T> {
  const resp = await request(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  return resp.json();
}

export async function patchJson<T>(path: string, body: unknown): Promise<T> {
  const resp = await request(path, {
    method: "PATCH",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  return resp.json();
}

export async function deleteJson(path: string): Promise<void> {
  await request(path, { method: "DELETE" });
}
