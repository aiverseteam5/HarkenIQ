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

/* Tenant context deliberately does NOT live here any more. It was a
   module-scope variable mirrored into localStorage and sent as
   X-Harken-Tenant on every request — invisible, sticky across sessions,
   and one-tenant-per-browser. It is now a path segment, so the URL you are
   looking at is the tenant you are in, and each page reads it from its own
   route params. */

export function setAccessToken(token: string): void {
  _accessToken = token;
}

export function getAccessToken(): string {
  return _accessToken;
}

export function clearAccessToken(): void {
  _accessToken = "";
}

export function setOnAuthError(callback: () => void): void {
  _onAuthError = callback;
}

async function request(path: string, init?: RequestInit): Promise<Response> {
  const headers = new Headers(init?.headers);
  if (_accessToken) {
    headers.set("authorization", `Bearer ${_accessToken}`);
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
