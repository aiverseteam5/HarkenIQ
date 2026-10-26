/**
 * API client for HarkenIQ Console.
 * Token stored in module scope (not sessionStorage) for security.
 */

/* A34.8: the error classes live in `apiErrors` so a view-model can read a
   failure's status without importing the fetching client. Re-exported here
   so every existing import is unchanged. */
import { ApiError, AuthError } from "./apiErrors";

export { ApiError, AuthError };

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
  let resp: Response;
  try {
    resp = await fetch(path, { ...init, headers });
  } catch (err) {
    // No response at all: the message stays the browser's own, so what a
    // page shows is unchanged, and the status says nothing answered.
    throw new ApiError(err instanceof Error ? err.message : String(err), 0);
  }
  if (resp.status === 401) {
    if (_onAuthError) _onAuthError();
    throw new AuthError();
  }
  if (!resp.ok) {
    let detail: unknown = resp.statusText;
    try {
      detail = (await resp.json()).detail ?? detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(detailText(detail), resp.status);
  }
  return resp;
}

/** The server's own reason, as text (P4: a refusal shows the server's
 *  words). FastAPI sends a string for an HTTPException and a list of
 *  `{msg}` for a validation failure; the list used to reach the screen
 *  as "[object Object]". */
function detailText(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const msgs = detail
      .map((d) => (d && typeof d === "object" && "msg" in d ? String(d.msg) : ""))
      .filter(Boolean);
    if (msgs.length) return msgs.join("; ");
  }
  try {
    return JSON.stringify(detail);
  } catch {
    return String(detail);
  }
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

export async function putJson<T>(path: string, body: unknown): Promise<T> {
  const resp = await request(path, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  return resp.json();
}

export async function deleteJson(path: string): Promise<void> {
  await request(path, { method: "DELETE" });
}
