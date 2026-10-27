/** A34.3: a fake network for DOM tests.
 *
 *  `stubFetch` answers each request by the longest matching path prefix
 *  and records every URL asked for, so a test can assert both what the
 *  page rendered and what it requested -- including what it must never
 *  request.
 */

import { vi } from "vitest";

export interface StubReply {
  status?: number;
  body?: unknown;
}

export type StubRoutes = Record<string, StubReply | ((url: URL) => StubReply)>;

export function stubFetch(routes: StubRoutes) {
  const calls: string[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const url = new URL(raw, "http://console.test");
    calls.push(`${url.pathname}${url.search}`);
    const key = Object.keys(routes)
      .filter((prefix) => url.pathname === prefix || url.pathname.startsWith(prefix))
      .sort((a, b) => b.length - a.length)[0];
    if (key === undefined) {
      return new Response(JSON.stringify({ detail: `no stub for ${url.pathname}` }), {
        status: 599,
        headers: { "content-type": "application/json" },
      });
    }
    const entry = routes[key];
    const reply = typeof entry === "function" ? entry(url) : entry;
    return new Response(JSON.stringify(reply.body ?? {}), {
      status: reply.status ?? 200,
      headers: { "content-type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls, fetchMock };
}
