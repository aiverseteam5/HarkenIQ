export class AuthError extends Error {}

export function getToken(): string {
  return sessionStorage.getItem("sm_token") ?? "";
}

export function setToken(token: string): void {
  sessionStorage.setItem("sm_token", token);
}

async function request(path: string, init?: RequestInit): Promise<Response> {
  const headers = new Headers(init?.headers);
  const token = getToken();
  if (token) headers.set("authorization", `Bearer ${token}`);
  const resp = await fetch(path, { ...init, headers });
  if (resp.status === 401) throw new AuthError("unauthorized");
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
